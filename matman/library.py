"""A window for browsing and downloading materials from ambientCG and Poly Haven."""

import html
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import (
    QBuffer, QByteArray, QIODevice, QObject, QSize, QStandardPaths, Qt, QTimer, Signal, Slot,
)
from PySide6.QtGui import QColor, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from .cache import DiskCache
from .sources import (
    FORMATS, RESOLUTIONS, SOURCES, CancelToken, DownloadCancelled, RemoteMaterial, fetch,
)


PAGE_SIZE = 60
THUMBNAIL_SIZE = 128
THUMBNAIL_THREADS = 6
SEARCH_DELAY_MS = 400  # Wait for typing to pause before searching
LOAD_MORE_MARGIN = 300  # Load the next page when scrolled this close to the bottom (pixels)

ERROR_COLOR = '#d03030'

# Thumbnails are cached on disk as small WebPs (about 5 KB each, so every thumbnail from both
# sites is around 15 MB). Least recently used ones are deleted beyond this limit.
THUMBNAIL_CACHE_BYTES = 30 * 1024 * 1024
THUMBNAIL_CACHE_QUALITY = 85


def thumbnail_cache_dir() -> Path | None:
    """The user's cache folder for thumbnails, e.g. ~/.cache/matman/thumbnails on Linux."""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericCacheLocation)
    return Path(base) / 'matman' / 'thumbnails' if base else None


def _encode_webp(image: QImage) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'WEBP', THUMBNAIL_CACHE_QUALITY)
    return bytes(data)


class LibraryWorker(QObject):
    """
    Runs searches, thumbnail fetches and downloads on background threads. Signals are emitted
    from those threads; Qt delivers them to the window on the GUI thread.
    """

    page_loaded = Signal(int, object, int)  # Request id, list[RemoteMaterial], total results
    search_failed = Signal(int, str)  # Request id, error message
    thumbnail_loaded = Signal(str, QImage)  # Thumbnail URL, image
    download_progress = Signal(object, int, int)  # RemoteMaterial, bytes done, bytes total
    download_finished = Signal(object, str)  # RemoteMaterial, downloaded file
    download_failed = Signal(object, str)  # RemoteMaterial, error message
    download_cancelled = Signal(object)  # RemoteMaterial

    def __init__(self):
        super().__init__()
        self._search_pool = ThreadPoolExecutor(max_workers=1)
        self._thumbnail_pool = ThreadPoolExecutor(max_workers=THUMBNAIL_THREADS)
        self._download_pool = ThreadPoolExecutor(max_workers=1)  # One at a time, to be gentle
        self._cancel = CancelToken()  # Cancels downloads; replaced after use so later ones run
        self._closing = CancelToken()  # Cancels everything when the app closes
        self.wanted_thumbnails: set[str] = set()  # Fetches for anything else are skipped
        cache_dir = thumbnail_cache_dir()
        self.thumbnail_cache = DiskCache(cache_dir, THUMBNAIL_CACHE_BYTES, '.webp') if cache_dir else None

    def search(self, request_id: int, source: str, query: str, offset: int):
        self._search_pool.submit(self._search, request_id, source, query, offset)

    def _search(self, request_id: int, source: str, query: str, offset: int):
        try:
            materials, total = SOURCES[source].search(query, offset, PAGE_SIZE, self._closing)
            self.page_loaded.emit(request_id, materials, total)
        except DownloadCancelled:
            pass  # The app is closing
        except Exception as e:
            self.search_failed.emit(request_id, str(e) or type(e).__name__)

    def load_thumbnail(self, url: str):
        self._thumbnail_pool.submit(self._load_thumbnail, url)

    def _load_thumbnail(self, url: str):
        if url not in self.wanted_thumbnails:
            return  # The results it was for have been replaced

        if self.thumbnail_cache is not None and (cached := self.thumbnail_cache.get(url)):
            image = QImage.fromData(cached)
            if not image.isNull():
                self.thumbnail_loaded.emit(url, image)
                return
            self.thumbnail_cache.remove(url)  # Corrupt, so fetch it again

        try:
            image = QImage.fromData(fetch(url, cancel=self._closing))
        except Exception:
            return  # The placeholder stays
        if image.isNull():
            return
        image = image.scaled(THUMBNAIL_SIZE, THUMBNAIL_SIZE, Qt.AspectRatioMode.KeepAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)
        if self.thumbnail_cache is not None:
            self.thumbnail_cache.put(url, _encode_webp(image))
        self.thumbnail_loaded.emit(url, image)

    def download(self, material: RemoteMaterial, resolution: str, fmt: str, dest_dir: Path):
        self._download_pool.submit(self._download, material, resolution, fmt, dest_dir, self._cancel)

    def _download(self, material: RemoteMaterial, resolution: str, fmt: str, dest_dir: Path,
                  cancel: CancelToken):
        if cancel.is_set():
            self.download_cancelled.emit(material)
            return
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
            path = SOURCES[material.source].download(
                material, resolution, fmt, dest_dir,
                lambda done, total: self.download_progress.emit(material, done, total), cancel)
            self.download_finished.emit(material, str(path))
        except DownloadCancelled:
            self.download_cancelled.emit(material)
        except Exception as e:
            self.download_failed.emit(material, str(e) or type(e).__name__)

    def cancel_downloads(self):
        """Stop the current download and drop queued ones."""
        self._cancel.cancel()
        self._cancel = CancelToken()

    def shutdown(self):
        """
        Stop all network activity, including requests blocked waiting for data, so the app's
        process can exit straight away. Downloads in progress delete their partial files.
        """
        self._closing.cancel()
        self._cancel.cancel()
        for pool in (self._search_pool, self._thumbnail_pool, self._download_pool):
            pool.shutdown(wait=False, cancel_futures=True)


def _downloaded_file(material: RemoteMaterial, folder: Path) -> Path | None:
    """A previously downloaded zip of this material in `folder`, at any resolution."""
    return next(folder.glob(f'{material.id}_*.zip'), None) if folder.is_dir() else None


class LibraryWindow(QWidget):
    """Browse ambientCG and Poly Haven, and download materials into a folder."""

    # A material finished downloading (its zip's path), and should be added to the conversion list
    material_downloaded = Signal(str)

    def __init__(self, parent: QWidget | None=None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setWindowTitle('matman - Online materials')
        self.resize(900, 700)

        self.worker = LibraryWorker()
        self.worker.page_loaded.connect(self._on_page_loaded)
        self.worker.search_failed.connect(self._on_search_failed)
        self.worker.thumbnail_loaded.connect(self._on_thumbnail_loaded)
        self.worker.download_progress.connect(self._on_download_progress)
        self.worker.download_finished.connect(self._on_download_finished)
        self.worker.download_failed.connect(self._on_download_failed)
        self.worker.download_cancelled.connect(self._on_download_cancelled)

        # Search state
        self.request_id = 0
        self.loaded = 0  # Results shown so far
        self.total = 0
        self.loading = False
        self.items_by_thumbnail: dict[str, QListWidgetItem] = {}

        # Download state
        self.queued: list[RemoteMaterial] = []  # Waiting or in progress, in order
        self.batch_size = 0
        self.batch_done = 0
        self.batch_errors: list[str] = []

        self.placeholder_icon = self._make_placeholder_icon()

        layout = QVBoxLayout(self)
        layout.addLayout(self._build_search_row())
        layout.addWidget(self._build_grid(), 1)
        self.credit_label = QLabel()
        self.credit_label.setOpenExternalLinks(True)
        layout.addWidget(self.credit_label)
        layout.addLayout(self._build_download_options())
        layout.addLayout(self._build_download_row())

        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(SEARCH_DELAY_MS)
        self.search_timer.timeout.connect(self.new_search)

        self.new_search()

    # ----- Layout

    def _build_search_row(self) -> QHBoxLayout:
        self.source_combo = QComboBox()
        for key, source in SOURCES.items():
            self.source_combo.addItem(source.name, key)
        self.source_combo.currentIndexChanged.connect(self.new_search)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText('Search materials…')
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(lambda: self.search_timer.start())
        self.search_edit.returnPressed.connect(self.new_search)

        self.results_label = QLabel()

        row = QHBoxLayout()
        row.addWidget(self.source_combo)
        row.addWidget(self.search_edit, 1)
        row.addWidget(self.results_label)
        return row

    def _build_grid(self) -> QListWidget:
        self.grid = QListWidget()
        self.grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid.setIconSize(QSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE))
        self.grid.setGridSize(QSize(THUMBNAIL_SIZE + 28, THUMBNAIL_SIZE + 44))
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid.setMovement(QListWidget.Movement.Static)
        self.grid.setUniformItemSizes(True)
        self.grid.setWordWrap(True)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.grid.verticalScrollBar().valueChanged.connect(self._maybe_load_more)
        self.grid.itemDoubleClicked.connect(lambda item: self._download_items([item]))
        self.grid.itemSelectionChanged.connect(self._update_download_button)
        return self.grid

    def _build_download_options(self) -> QHBoxLayout:
        self.resolution_combo = QComboBox()
        self.resolution_combo.addItems(RESOLUTIONS)
        self.resolution_combo.setCurrentText('2K')
        self.resolution_combo.setToolTip('The closest available resolution is used if this one is missing')

        self.format_combo = QComboBox()
        self.format_combo.addItems(FORMATS)

        default_folder = Path(QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation)) / 'matman'
        self.folder_edit = QLineEdit(str(default_folder))
        self.folder_edit.editingFinished.connect(self._refresh_downloaded_marks)
        browse = QPushButton('Browse…')
        browse.clicked.connect(self._choose_folder)

        self.add_to_list_check = QCheckBox('Add to conversion list')
        self.add_to_list_check.setChecked(True)

        row = QHBoxLayout()
        row.addWidget(QLabel('Resolution:'))
        row.addWidget(self.resolution_combo)
        row.addWidget(QLabel('Format:'))
        row.addWidget(self.format_combo)
        row.addSpacing(12)
        row.addWidget(QLabel('Save to:'))
        row.addWidget(self.folder_edit, 1)
        row.addWidget(browse)
        row.addWidget(self.add_to_list_check)
        return row

    def _build_download_row(self) -> QHBoxLayout:
        self.download_status = QLabel('Double-click a material, or select several and press Download')
        self.download_bar = QProgressBar()
        self.download_bar.setTextVisible(False)
        self.download_bar.setMaximumWidth(200)
        self.download_bar.hide()

        self.download_button = QPushButton('Download')
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(lambda: self._download_items(self.grid.selectedItems()))

        self.cancel_button = QPushButton('Cancel downloads')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_downloads)

        row = QHBoxLayout()
        row.addWidget(self.download_status, 1)
        row.addWidget(self.download_bar)
        row.addWidget(self.download_button)
        row.addWidget(self.cancel_button)
        return row

    def _make_placeholder_icon(self) -> QIcon:
        pixmap = QPixmap(THUMBNAIL_SIZE, THUMBNAIL_SIZE)
        pixmap.fill(QColor(128, 128, 128, 60))
        return QIcon(pixmap)

    # ----- Searching

    def _source(self) -> str:
        return self.source_combo.currentData()

    @Slot()
    def new_search(self):
        """Replace the results with the first page for the current source and search text."""
        self.search_timer.stop()
        self.request_id += 1
        self.loaded = self.total = 0
        self.grid.clear()
        self.items_by_thumbnail.clear()
        self.worker.wanted_thumbnails = set()

        source = SOURCES[self._source()]
        self.credit_label.setText(
            f'Powered by <a href="{source.url}">{html.escape(source.name)}</a>. '
            'Materials are CC0: free to use for any purpose.')

        self._load_page()

    def _load_page(self):
        self.loading = True
        self.results_label.setText('Loading…')
        self.results_label.setStyleSheet('')
        self.worker.search(self.request_id, self._source(), self.search_edit.text(), self.loaded)

    def _maybe_load_more(self):
        bar = self.grid.verticalScrollBar()
        if not self.loading and self.loaded < self.total and bar.value() >= bar.maximum() - LOAD_MORE_MARGIN:
            self._load_page()

    @Slot(int, object, int)
    def _on_page_loaded(self, request_id: int, materials: list[RemoteMaterial], total: int):
        if request_id != self.request_id:
            return  # A newer search replaced this one
        self.loading = False
        self.total = total
        self.loaded += len(materials)

        folder = self._folder()
        wanted = set(self.worker.wanted_thumbnails)
        for material in materials:
            item = QListWidgetItem(self.placeholder_icon, material.name)
            item.setData(Qt.ItemDataRole.UserRole, material)
            item.setToolTip(f'{material.name}\n{SOURCES[material.source].name}: {material.id}')
            self.grid.addItem(item)
            self._mark_downloaded(item, _downloaded_file(material, folder))
            if material.thumbnail_url:
                self.items_by_thumbnail[material.thumbnail_url] = item
                wanted.add(material.thumbnail_url)
        self.worker.wanted_thumbnails = wanted
        for material in materials:
            if material.thumbnail_url:
                self.worker.load_thumbnail(material.thumbnail_url)

        self.results_label.setText('No results' if total == 0 else f'{self.loaded} of {total}')
        # Keep loading if the results don't fill the grid yet, so it can scroll. The grid lays
        # out its items lazily, so do it now or the scroll range would still be empty.
        self.grid.doItemsLayout()
        self._maybe_load_more()

    @Slot(int, str)
    def _on_search_failed(self, request_id: int, error: str):
        if request_id != self.request_id:
            return
        self.loading = False
        self.results_label.setText("Couldn't load results")
        self.results_label.setStyleSheet(f'color: {ERROR_COLOR}')
        self.results_label.setToolTip(error)

    @Slot(str, QImage)
    def _on_thumbnail_loaded(self, url: str, image: QImage):
        if item := self.items_by_thumbnail.get(url):
            item.setIcon(QIcon(QPixmap.fromImage(image)))

    # ----- Downloading

    def _folder(self) -> Path:
        return Path(self.folder_edit.text().strip()).expanduser()

    def _choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, 'Save downloads to', self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)
            self._refresh_downloaded_marks()

    def _mark_downloaded(self, item: QListWidgetItem, path: Path | None):
        material: RemoteMaterial = item.data(Qt.ItemDataRole.UserRole)
        item.setText(f'✓ {material.name}' if path else material.name)

    def _refresh_downloaded_marks(self):
        folder = self._folder()
        for i in range(self.grid.count()):
            item = self.grid.item(i)
            self._mark_downloaded(item, _downloaded_file(item.data(Qt.ItemDataRole.UserRole), folder))

    def _update_download_button(self):
        """Enable Download only if a selected material isn't already downloading or queued."""
        selected = [item.data(Qt.ItemDataRole.UserRole) for item in self.grid.selectedItems()]
        downloadable = [material for material in selected if material not in self.queued]
        self.download_button.setEnabled(bool(downloadable))
        if selected and not downloadable:
            self.download_button.setToolTip('Already downloading')
        else:
            self.download_button.setToolTip('')

    def _download_items(self, items: list[QListWidgetItem]):
        folder = self._folder()
        if not str(folder).strip():
            return
        if not self.queued:  # Nothing running, so this starts a new batch
            self.batch_size = self.batch_done = 0
            self.batch_errors = []
            self.download_status.setToolTip('')

        for item in items:
            material: RemoteMaterial = item.data(Qt.ItemDataRole.UserRole)
            if material in self.queued:
                continue
            self.queued.append(material)
            self.batch_size += 1
            self.worker.download(material, self.resolution_combo.currentText(),
                                 self.format_combo.currentText(), folder)

        self._update_download_button()
        if self.queued:
            self.cancel_button.setEnabled(True)
            self.download_bar.show()
            self._show_download_status()

    def _show_download_status(self, done: int=0, total: int=0):
        if not self.queued:
            return
        current = self.queued[0]
        position = self.batch_done + len(self.batch_errors) + 1
        sizes = f' - {done / 1e6:.1f} / {total / 1e6:.1f} MB' if total else ''
        self.download_status.setText(
            f'Downloading {current.name} ({position} of {self.batch_size}){sizes}')
        self.download_bar.setRange(0, 1000)
        self.download_bar.setValue(int(done / total * 1000) if total else 0)

    @Slot(object, int, int)
    def _on_download_progress(self, material: RemoteMaterial, done: int, total: int):
        if self.queued and self.queued[0] == material:
            self._show_download_status(done, total)

    def _finish_one(self, material: RemoteMaterial):
        if material in self.queued:
            self.queued.remove(material)
        self._update_download_button()
        if self.queued:
            self._show_download_status()
            return

        self.cancel_button.setEnabled(False)
        self.download_bar.hide()
        message = f'Downloaded {self.batch_done} of {self.batch_size}'
        if self.batch_errors:
            message += f' ({len(self.batch_errors)} failed, hover for details)'
            self.download_status.setToolTip('\n'.join(self.batch_errors))
        self.download_status.setText(message)

    @Slot(object, str)
    def _on_download_finished(self, material: RemoteMaterial, path: str):
        self.batch_done += 1
        for i in range(self.grid.count()):
            item = self.grid.item(i)
            if item.data(Qt.ItemDataRole.UserRole) == material:
                self._mark_downloaded(item, Path(path))
        if self.add_to_list_check.isChecked():
            self.material_downloaded.emit(path)
        self._finish_one(material)

    @Slot(object, str)
    def _on_download_failed(self, material: RemoteMaterial, error: str):
        self.batch_errors.append(f'{material.name}: {error}')
        self._finish_one(material)

    @Slot(object)
    def _on_download_cancelled(self, material: RemoteMaterial):
        if material in self.queued:
            self.queued.remove(material)
        self._update_download_button()
        if not self.queued:
            self.cancel_button.setEnabled(False)
            self.download_bar.hide()
            self.download_status.setText(f'Cancelled - downloaded {self.batch_done} of {self.batch_size}')

    def _cancel_downloads(self):
        self.worker.cancel_downloads()
        self.cancel_button.setEnabled(False)
        self.download_status.setText('Cancelling…')

    def shutdown(self):
        """Stop all network activity. Call when the app closes."""
        self.worker.shutdown()
