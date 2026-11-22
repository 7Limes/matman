"""Graphical interface for mattra. Run with `python gui.py`."""

import html
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QFontMetrics, QKeyEvent, QKeySequence, QPainter, QPalette
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFileSystemModel,
    QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget,
    QListWidgetItem, QMainWindow, QPlainTextEdit, QProgressBar, QPushButton, QSizePolicy,
    QSpinBox, QToolButton, QTreeView, QVBoxLayout, QWidget,
)

from main import ResizeOptions, convert_material
from material import ConversionCancelled, TextureScan, scan_textures
from util import set_warning_handler


PLATFORMS = [('Godot', 'godot'), ('Unity (URP)', 'unity')]

WARNING_COLOR = '#c08000'
ERROR_COLOR = '#d03030'

# Background colors of the result banner shown when a conversion finishes
RESULT_SUCCESS_COLOR = '#1565c0'
RESULT_CANCELLED_COLOR = '#b26a00'
RESULT_FAILED_COLOR = '#c62828'

# How long the Clear button waits for its confirming second click
CLEAR_CONFIRM_MS = 3000

# Texture badges shown on each row: (badge text, texture name, Material field)
TEXTURE_BADGES = [
    ('D', 'Diffuse', 'diffuse_map'),
    ('N', 'Normal', 'normal_map'),
    ('R', 'Roughness', 'roughness_map'),
    ('M', 'Metallic', 'metallic_map'),
    ('AO', 'Ambient occlusion', 'occlusion_map'),
    ('H', 'Height', 'displacement_map'),
]
BADGE_COLOR = '#1565c0'
BADGE_MISSING_COLOR = 'rgba(128, 128, 128, 110)'  # Faint on both light and dark themes

# Badge looks, chosen by each badge's 'badge' property. Set once on the list, since parsing a
# style sheet per badge makes adding many materials slow.
BADGE_STYLE_SHEET = f'''
QLabel[badge="missing"] {{ color: {BADGE_MISSING_COLOR}; border: 1px solid {BADGE_MISSING_COLOR}; border-radius: 3px; }}
QLabel[badge="found"] {{ background: {BADGE_COLOR}; color: white; border: 1px solid {BADGE_COLOR}; border-radius: 3px; }}
QLabel[badge="derived"] {{ color: {BADGE_COLOR}; border: 1px solid {BADGE_COLOR}; border-radius: 3px; }}
QLabel[scanError="true"] {{ color: {ERROR_COLOR}; }}
'''

# Threads used to find each material's textures in the background
SCAN_THREADS = 4


class ElidedLabel(QLabel):
    """
    A label in a muted color that shortens its text with '…' in the middle instead of growing
    the window.
    """

    def __init__(self, text: str):
        super().__init__(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setPen(self.palette().color(QPalette.ColorRole.PlaceholderText))
        elided = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideMiddle,
                                               self.width())
        painter.drawText(self.rect(), self.alignment(), elided)


class MaterialList(QListWidget):
    """The conversion list. Pressing Delete (or Backspace) asks to remove the selected rows."""

    delete_pressed = Signal()

    def keyPressEvent(self, event: QKeyEvent):
        if event.matches(QKeySequence.StandardKey.Delete) or event.key() == Qt.Key.Key_Backspace:
            self.delete_pressed.emit()
        else:
            super().keyPressEvent(event)


class TextureBadges(QWidget):
    """
    A row of badges showing which textures a material has. Found textures are filled, missing
    ones are faint outlines, and roughness taken from the metallic map's alpha is outlined.
    Hovering a badge shows which file it is. Until set_scan is called, every badge is faint.
    """

    def __init__(self):
        super().__init__()
        font = self.font()
        font.setPointSizeF(font.pointSizeF() * 0.85)
        font.setBold(True)
        metrics = QFontMetrics(font)
        # The same size for every badge keeps them in columns down the list
        width = metrics.horizontalAdvance('AO') + 10
        height = metrics.height() + 4

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        self.badges: dict[str, QLabel] = {}
        for text, name, field in TEXTURE_BADGES:
            badge = QLabel(text)
            badge.setFont(font)
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setFixedSize(width, height)
            self._set_state(badge, 'missing', f'{name}: looking…')
            layout.addWidget(badge)
            self.badges[field] = badge

    @staticmethod
    def _set_state(badge: QLabel, state: str, tooltip: str):
        """Set a badge's look ('missing', 'found' or 'derived'; see BADGE_STYLE_SHEET)."""
        if badge.property('badge') != state:
            badge.setProperty('badge', state)
            badge.style().unpolish(badge)  # Re-apply the style sheet for the new property
            badge.style().polish(badge)
        badge.setToolTip(tooltip)

    def set_scan(self, scan: TextureScan):
        for _, name, field in TEXTURE_BADGES:
            badge = self.badges[field]
            image = scan.textures[field]
            if image is not None:
                self._set_state(badge, 'found', f'{name}: {image.name}')
            elif field == 'roughness_map' and scan.smoothness_in_metallic:
                self._set_state(badge, 'derived', f'{name}: from the smoothness in the alpha '
                                f'channel of {scan.textures["metallic_map"].name}')
            else:
                self._set_state(badge, 'missing', f'{name}: not found')


class TextureScanner(QObject):
    """
    Finds materials' textures on background threads. Results are emitted from those threads;
    Qt delivers them to the window on the GUI thread.
    """

    scanned = Signal(str, object)  # Path, TextureScan
    failed = Signal(str, str)  # Path, error message

    def __init__(self):
        super().__init__()
        self._executor = ThreadPoolExecutor(max_workers=SCAN_THREADS)

    def scan(self, path: str):
        self._executor.submit(self._scan, path)

    def _scan(self, path: str):
        try:
            self.scanned.emit(path, scan_textures(path))
        except Exception as e:  # Shown on the row; the conversion will report the problem too
            self.failed.emit(path, str(e) or type(e).__name__)

    def shutdown(self):
        self._executor.shutdown(wait=False, cancel_futures=True)


class MaterialRow(QWidget):
    """
    A row in the conversion list: the material's name and path, which textures it has, and a
    remove button.
    """

    remove_requested = Signal(str)

    def __init__(self, path: str):
        super().__init__()
        self.path = path

        name = QLabel(Path(path).name)
        font = name.font()
        font.setBold(True)
        name.setFont(font)

        location = ElidedLabel(str(Path(path).parent))
        self.setToolTip(path)

        text = QVBoxLayout()
        text.setSpacing(0)
        text.addWidget(name)
        text.addWidget(location)

        self.remove_button = QToolButton()
        self.remove_button.setText('✕')
        self.remove_button.setToolTip('Remove from the list')
        self.remove_button.setAutoRaise(True)
        self.remove_button.clicked.connect(lambda: self.remove_requested.emit(self.path))

        self.badges = TextureBadges()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 3, 3, 3)
        layout.addLayout(text, 1)
        layout.addSpacing(6)
        layout.addWidget(self.badges)
        layout.addWidget(self.remove_button)

    def set_scan(self, scan: TextureScan):
        self.badges.set_scan(scan)

    def set_scan_error(self, error: str):
        """Replace the badges with a note that the material can't be read."""
        message = QLabel("Can't read")
        message.setProperty('scanError', True)
        message.setToolTip(error)
        message.setMinimumWidth(self.badges.sizeHint().width())
        self.layout().replaceWidget(self.badges, message)
        self.badges.deleteLater()


class ConversionWorker(QObject):
    """
    Converts materials on background threads. Signals are emitted from those threads; Qt
    delivers them to the window on the GUI thread.
    """

    material_done = Signal(str, str)  # Source path, exported material path
    material_failed = Signal(str, str)  # Source path, error message
    finished = Signal(bool)  # Whether the conversion was cancelled

    def __init__(self, files: list[str], platform: str, output_dir: str, threaded: bool,
                 resize: ResizeOptions | None):
        super().__init__()
        self.files = files
        self.platform = platform
        self.output_dir = output_dir
        self.threaded = threaded
        self.resize = resize
        self.cancel_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def cancel(self):
        self.cancel_event.set()

    def wait(self):
        if self._thread is not None:
            self._thread.join()

    def _convert(self, file: str):
        try:
            exported = convert_material(file, self.platform, self.output_dir, self.resize,
                                        self.cancel_event)
            self.material_done.emit(file, str(exported))
        except ConversionCancelled:
            pass
        except Exception as e:  # Report any failure instead of stopping the whole conversion
            self.material_failed.emit(file, str(e) or type(e).__name__)

    def _run(self):
        if self.threaded:
            # One thread per material, like the CLI's --threaded
            with ThreadPoolExecutor(max_workers=len(self.files)) as executor:
                list(executor.map(self._convert, self.files))
        else:
            for file in self.files:
                if self.cancel_event.is_set():
                    break
                self._convert(file)

        self.finished.emit(self.cancel_event.is_set())


class MainWindow(QMainWindow):
    # Warnings can come from conversion threads, so they go through a signal to reach the GUI thread
    warning_received = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle('mattra')
        self.resize(640, 720)

        self.worker: ConversionWorker | None = None

        self.scanner = TextureScanner()
        self.scanner.scanned.connect(self._on_scanned)
        self.scanner.failed.connect(self._on_scan_failed)
        self.start_time = 0.0
        self.succeeded = 0
        self.failed = 0

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.addWidget(self._build_materials_section(), 1)
        layout.addWidget(self._build_settings_section())
        layout.addWidget(self._build_resize_section())
        layout.addLayout(self._build_conversion_section())
        layout.addWidget(self._build_result_banner())
        layout.addWidget(self._build_log_section())
        self.setCentralWidget(central)

        # Must be a method of this window, not a lambda, so Qt runs it on the GUI thread
        self.warning_received.connect(self._on_warning)
        set_warning_handler(self.warning_received.emit)

        self._update_list_status()
        self._update_convert_enabled()

    # ----- Layout

    def _build_materials_section(self) -> QGroupBox:
        group = QGroupBox('Materials')

        self.add_zips_button = QPushButton('Add zip files…')
        self.add_zips_button.clicked.connect(self._choose_zips)
        self.add_folders_button = QPushButton('Add folders…')
        self.add_folders_button.clicked.connect(self._choose_folders)

        self.list_status = QLabel()

        # Clearing takes two clicks: the first arms the button, the second clears
        self.clear_button = QPushButton('Clear')
        self.clear_button.setToolTip('Remove every material from the list')
        self.clear_button.clicked.connect(self._on_clear_clicked)
        self.clear_button.setMinimumWidth(
            QPushButton('Click again to clear').sizeHint().width())  # Don't resize when armed
        self.clear_armed = False
        self.clear_timer = QTimer(self)
        self.clear_timer.setSingleShot(True)
        self.clear_timer.setInterval(CLEAR_CONFIRM_MS)
        self.clear_timer.timeout.connect(self._disarm_clear)

        header = QHBoxLayout()
        header.addWidget(self.add_zips_button)
        header.addWidget(self.add_folders_button)
        header.addWidget(self.list_status, 1)
        header.addWidget(self.clear_button)

        self.material_list = MaterialList()
        self.material_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.material_list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.material_list.delete_pressed.connect(self._remove_selected)
        self.material_list.setStyleSheet(BADGE_STYLE_SHEET)

        layout = QVBoxLayout(group)
        layout.addLayout(header)
        layout.addWidget(self.material_list)
        return group

    def _build_settings_section(self) -> QGroupBox:
        group = QGroupBox('Output')

        self.platform_combo = QComboBox()
        for label, value in PLATFORMS:
            self.platform_combo.addItem(label, value)

        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText('Required')
        self.output_edit.textChanged.connect(self._update_convert_enabled)
        browse = QPushButton('Browse…')
        browse.clicked.connect(self._choose_output_dir)
        output_row = QHBoxLayout()
        output_row.addWidget(self.output_edit, 1)
        output_row.addWidget(browse)
        self.output_browse_button = browse

        self.threaded_check = QCheckBox('Convert each material in a separate thread')
        self.threaded_check.setChecked(True)

        form = QFormLayout(group)
        form.addRow('Platform:', self.platform_combo)
        form.addRow('Output folder:', output_row)
        form.addRow('', self.threaded_check)
        return group

    def _build_resize_section(self) -> QGroupBox:
        self.resize_group = QGroupBox('Resize textures')
        self.resize_group.setCheckable(True)
        self.resize_group.setChecked(False)

        self.size_spin = QSpinBox()
        self.size_spin.setRange(1, 16384)
        self.size_spin.setValue(2048)
        self.size_spin.setSingleStep(256)
        self.size_spin.setSuffix(' px')
        self.size_spin.setToolTip("Each texture's longer side is resized to this, keeping its aspect ratio")

        self.no_upscale_check = QCheckBox("Don't enlarge textures smaller than this")
        self.no_downscale_check = QCheckBox("Don't shrink textures larger than this")
        # Both together would resize nothing, so checking one disables the other
        self.no_upscale_check.toggled.connect(lambda on: self.no_downscale_check.setEnabled(not on))
        self.no_downscale_check.toggled.connect(lambda on: self.no_upscale_check.setEnabled(not on))

        form = QFormLayout(self.resize_group)
        form.addRow('Size:', self.size_spin)
        form.addRow('', self.no_upscale_check)
        form.addRow('', self.no_downscale_check)
        return self.resize_group

    def _build_conversion_section(self) -> QHBoxLayout:
        self.progress_bar = QProgressBar()
        self.progress_bar.setFormat('%v / %m materials')
        self.progress_bar.setValue(0)

        self.convert_button = QPushButton('Convert')
        self.convert_button.setDefault(True)
        self.convert_button.clicked.connect(self._start_conversion)

        self.cancel_button = QPushButton('Cancel')
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_conversion)

        layout = QHBoxLayout()
        layout.addWidget(self.progress_bar, 1)
        layout.addWidget(self.convert_button)
        layout.addWidget(self.cancel_button)
        return layout

    def _build_result_banner(self) -> QLabel:
        self.result_banner = QLabel()
        self.result_banner.setWordWrap(True)
        self.result_banner.hide()  # Shown when a conversion finishes
        return self.result_banner

    def _show_result(self, message: str, color: str):
        self.result_banner.setText(message)
        self.result_banner.setStyleSheet(
            f'QLabel {{ background: {color}; color: white; font-weight: bold; font-size: 11pt;'
            ' padding: 8px; border-radius: 4px; }')
        self.result_banner.show()

    def _build_log_section(self) -> QGroupBox:
        group = QGroupBox('Log')
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setFixedHeight(130)
        layout = QVBoxLayout(group)
        layout.addWidget(self.log_view)
        return group

    # ----- Conversion list

    def _choose_zips(self):
        files, _ = QFileDialog.getOpenFileNames(self, 'Add zip files', '', 'Zip files (*.zip)')
        self.add_paths(files)

    def _choose_folders(self):
        # Native folder dialogs only allow choosing one folder, so use Qt's own dialog with
        # multi-select enabled on its file views
        dialog = QFileDialog(self, 'Add folders')
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        for view in dialog.findChildren(QListView) + dialog.findChildren(QTreeView):
            if isinstance(view.model(), QFileSystemModel):
                view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)

        if dialog.exec():
            self.add_paths(dialog.selectedFiles())

    def add_paths(self, paths: list[str]):
        """Add zip files or folders to the conversion list, skipping any already in it."""
        existing = {self._item_path(i) for i in range(self.material_list.count())}
        skipped = 0
        for path in paths:
            path = str(Path(path).resolve())
            if path in existing:
                skipped += 1
                continue
            existing.add(path)

            row = MaterialRow(path)
            row.remove_requested.connect(self.remove_path)
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setSizeHint(row.sizeHint())
            self.material_list.addItem(item)
            self.material_list.setItemWidget(item, row)
            self.scanner.scan(path)

        self._update_list_status()
        self._update_convert_enabled()
        if skipped:
            self.statusBar().showMessage(
                f'Skipped {skipped} {"item" if skipped == 1 else "items"} already in the list', 5000)

    def _row(self, path: str) -> MaterialRow | None:
        for i in range(self.material_list.count()):
            if self._item_path(i) == path:
                return self.material_list.itemWidget(self.material_list.item(i))
        return None  # Removed from the list while it was being scanned

    @Slot(str, object)
    def _on_scanned(self, path: str, scan: TextureScan):
        if row := self._row(path):
            row.set_scan(scan)

    @Slot(str, str)
    def _on_scan_failed(self, path: str, error: str):
        if row := self._row(path):
            row.set_scan_error(error)

    @Slot(str)
    def remove_path(self, path: str):
        for i in range(self.material_list.count()):
            if self._item_path(i) == path:
                self.material_list.takeItem(i)
                break
        self._update_list_status()
        self._update_convert_enabled()

    def _remove_selected(self):
        if self.worker is not None:
            return  # The list is locked while converting
        for item in self.material_list.selectedItems():
            self.material_list.takeItem(self.material_list.row(item))
        self._update_list_status()
        self._update_convert_enabled()

    def _on_clear_clicked(self):
        if not self.clear_armed:
            self.clear_armed = True
            self.clear_button.setText('Click again to clear')
            self.clear_timer.start()
            return

        self.material_list.clear()
        self._disarm_clear()
        self._update_list_status()
        self._update_convert_enabled()

    def _disarm_clear(self):
        self.clear_armed = False
        self.clear_timer.stop()
        self.clear_button.setText('Clear')

    def _item_path(self, index: int) -> str:
        return self.material_list.item(index).data(Qt.ItemDataRole.UserRole)

    def _paths(self) -> list[str]:
        return [self._item_path(i) for i in range(self.material_list.count())]

    def _update_list_status(self):
        count = self.material_list.count()
        if self.worker is None:
            self.progress_bar.setRange(0, max(count, 1))
            self.progress_bar.setValue(0)
        self.clear_button.setEnabled(count > 0 and self.worker is None)
        if count == 0:
            self._disarm_clear()
        self.list_status.setText(
            'No materials added' if count == 0 else f'{count} {"material" if count == 1 else "materials"}')

    # ----- Output

    def _choose_output_dir(self):
        directory = QFileDialog.getExistingDirectory(self, 'Choose output folder', self.output_edit.text())
        if directory:
            self.output_edit.setText(directory)

    # ----- Conversion

    def _update_convert_enabled(self):
        running = self.worker is not None
        has_materials = self.material_list.count() > 0
        has_output = bool(self.output_edit.text().strip())
        self.convert_button.setEnabled(not running and has_materials and has_output)

        if not has_materials:
            self.convert_button.setToolTip('Add materials to convert')
        elif not has_output:
            self.convert_button.setToolTip('Choose an output folder')
        else:
            self.convert_button.setToolTip('')

    def _set_inputs_enabled(self, enabled: bool):
        self._disarm_clear()
        self.clear_button.setEnabled(enabled and self.material_list.count() > 0)
        for widget in (self.add_zips_button, self.add_folders_button, self.platform_combo, self.output_edit,
                       self.output_browse_button, self.threaded_check, self.resize_group):
            widget.setEnabled(enabled)
        for i in range(self.material_list.count()):
            row = self.material_list.itemWidget(self.material_list.item(i))
            row.remove_button.setEnabled(enabled)

    def _start_conversion(self):
        files = self._paths()
        resize = None
        if self.resize_group.isChecked():
            resize = ResizeOptions(self.size_spin.value(), not self.no_upscale_check.isChecked(),
                                   not self.no_downscale_check.isChecked())

        self.worker = ConversionWorker(files, self.platform_combo.currentData(),
                                       self.output_edit.text().strip(),
                                       self.threaded_check.isChecked(), resize)
        self.worker.material_done.connect(self._on_material_done)
        self.worker.material_failed.connect(self._on_material_failed)
        self.worker.finished.connect(self._on_finished)

        self.succeeded = self.failed = 0
        self.progress_bar.setRange(0, len(files))
        self.progress_bar.setValue(0)
        self.log_view.clear()
        self.result_banner.hide()
        self._set_inputs_enabled(False)
        self._update_convert_enabled()
        self.cancel_button.setEnabled(True)
        self.cancel_button.setText('Cancel')
        self.statusBar().showMessage('Converting…')

        self.start_time = time.perf_counter()
        self.worker.start()

    def _cancel_conversion(self):
        if self.worker is not None:
            self.worker.cancel()
            self.cancel_button.setEnabled(False)
            self.cancel_button.setText('Cancelling…')
            self.statusBar().showMessage('Cancelling… (unfinished materials are removed)')

    @Slot(str)
    def _on_warning(self, message: str):
        self._log(f'Warning: {message}', WARNING_COLOR)

    @Slot(str, str)
    def _on_material_done(self, source: str, exported: str):
        self.succeeded += 1
        self.progress_bar.setValue(self.succeeded + self.failed)
        self._log(f'Exported {exported}')

    @Slot(str, str)
    def _on_material_failed(self, source: str, error: str):
        self.failed += 1
        self.progress_bar.setValue(self.succeeded + self.failed)
        self._log(f'Failed to convert {Path(source).name}: {error}', ERROR_COLOR)

    @Slot(bool)
    def _on_finished(self, cancelled: bool):
        self.worker.wait()
        self.worker = None

        elapsed = time.perf_counter() - self.start_time
        total = self.progress_bar.maximum()
        if cancelled:
            message = f'Cancelled after converting {self.succeeded} of {total} materials'
        else:
            message = f'Converted {self.succeeded} of {total} materials in {elapsed:.2f}s'
        if self.failed:
            message += f' ({self.failed} failed, see the log)'

        if self.failed:
            color = RESULT_FAILED_COLOR
        elif cancelled:
            color = RESULT_CANCELLED_COLOR
        else:
            color = RESULT_SUCCESS_COLOR
        self._show_result(message, color)
        self.statusBar().clearMessage()

        self.cancel_button.setEnabled(False)
        self.cancel_button.setText('Cancel')
        self._set_inputs_enabled(True)
        self._update_convert_enabled()

    def _log(self, message: str, color: str | None=None):
        text = html.escape(message)
        self.log_view.appendHtml(f'<span style="color:{color}">{text}</span>' if color else text)

    def closeEvent(self, event):
        # Stop a running conversion so it doesn't leave partial output or temporary files behind
        if self.worker is not None:
            self.worker.cancel()
            self.worker.wait()
        self.scanner.shutdown()
        set_warning_handler(None)
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName('mattra')
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
