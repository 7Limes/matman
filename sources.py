"""
Online material libraries: ambientCG and Poly Haven. Both publish their materials as CC0.

Everything here is blocking network code without Qt, so it can be used from any thread.
"""

import http.client
import json
import shutil
import socket
import threading
import urllib.parse
import urllib.request
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


# Poly Haven's API terms require requests to name the app in their User-Agent
USER_AGENT = 'mattra/0.1'

RESOLUTIONS = ('1K', '2K', '4K', '8K')
FORMATS = ('JPG', 'PNG')

_CHUNK_SIZE = 256 * 1024

# Called with (bytes downloaded so far, total bytes or 0 if unknown)
ProgressCallback = Callable[[int, int], None]


class DownloadCancelled(Exception):
    pass


class CancelToken:
    """
    Cancels network requests, including reads already blocked waiting for data: cancelling
    shuts down the sockets of requests in progress, so they fail immediately instead of
    waiting for their timeout (which would also keep the app's process alive after closing).
    """

    def __init__(self):
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._sockets: set[socket.socket] = set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def cancel(self):
        self._event.set()
        with self._lock:
            sockets = list(self._sockets)
        for sock in sockets:
            _shutdown(sock)

    @contextmanager
    def watch(self, response):
        """Register `response`'s connection while it's being read, so cancel can interrupt it."""
        sock = _response_socket(response)
        if sock is None:  # Can't interrupt it, but the cancel flag is still checked between reads
            yield
            return
        with self._lock:
            self._sockets.add(sock)
        if self.is_set():
            _shutdown(sock)
        try:
            yield
        finally:
            with self._lock:
                self._sockets.discard(sock)


def _response_socket(response) -> socket.socket | None:
    # urlopen gives no public access to the socket, so reach through the response's file object
    try:
        return response.fp.raw._sock
    except AttributeError:
        return None


def _shutdown(sock: socket.socket):
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass  # Already closed


# What a read fails with when its connection is shut down or cut off
_READ_ERRORS = (OSError, http.client.HTTPException)


@dataclass(frozen=True)
class RemoteMaterial:
    source: str  # Key into SOURCES
    id: str
    name: str
    thumbnail_url: str


def fetch(url: str, timeout: float=30, cancel: CancelToken | None=None) -> bytes:
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled()
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if cancel is None:
                return response.read()
            with cancel.watch(response):
                return response.read()
    except _READ_ERRORS:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        raise


def fetch_json(url: str, cancel: CancelToken | None=None):
    return json.loads(fetch(url, cancel=cancel))


def _download_file(url: str, dest: Path, progress: ProgressCallback | None,
                   cancel: CancelToken | None, done_before: int=0, total: int=0) -> int:
    """
    Stream `url` into `dest`, reporting progress offset by `done_before` (for multi-file
    downloads). Returns the number of bytes written. Raises if the server sends less than it
    said it would, so a cut-off download is never mistaken for a complete one.
    """
    if cancel is not None and cancel.is_set():
        raise DownloadCancelled()
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    written = 0
    try:
        with urllib.request.urlopen(request, timeout=60) as response, open(dest, 'wb') as f:
            expected = int(response.headers.get('Content-Length') or 0)
            if not total:
                total = expected
            with cancel.watch(response) if cancel is not None else _no_watch():
                while chunk := response.read(_CHUNK_SIZE):
                    if cancel is not None and cancel.is_set():
                        raise DownloadCancelled()
                    f.write(chunk)
                    written += len(chunk)
                    if progress is not None:
                        progress(done_before + written, total)
    except _READ_ERRORS:
        if cancel is not None and cancel.is_set():
            raise DownloadCancelled()
        raise

    if cancel is not None and cancel.is_set():
        raise DownloadCancelled()  # Shut down at a chunk boundary, so the read just ended early
    if expected and written < expected:
        raise RuntimeError(f'The download was cut off ({written} of {expected} bytes)')
    return written


@contextmanager
def _no_watch():
    yield


def _pick(available: dict[str, object], resolution: str, fmt: str) -> str | None:
    """
    Choose the closest available '<resolution>-<format>' key: the wanted resolution or the
    nearest lower one, in the wanted format first, then the other format.
    """
    index = RESOLUTIONS.index(resolution)
    lower_first = list(RESOLUTIONS[index::-1]) + list(RESOLUTIONS[index + 1:])
    for f in (fmt, *[f for f in FORMATS if f != fmt]):
        for res in lower_first:
            if f'{res}-{f}' in available:
                return f'{res}-{f}'
    return None


class AmbientCG:
    name = 'ambientCG'
    url = 'https://ambientcg.com'
    _API = 'https://ambientcg.com/api/v3/assets'

    @classmethod
    def search(cls, query: str, offset: int, limit: int,
               cancel: CancelToken | None=None) -> tuple[list[RemoteMaterial], int]:
        """Search materials, most popular first. Returns (page of materials, total results)."""
        params = {'type': 'material', 'sort': 'popular', 'limit': limit, 'offset': offset,
                  'include': 'title,thumbnails'}
        if query.strip():
            params['q'] = query.strip()
        data = fetch_json(f'{cls._API}?{urllib.parse.urlencode(params)}', cancel)

        materials = [
            RemoteMaterial('ambientcg', asset['id'], asset.get('title') or asset['id'],
                           asset['thumbnails'].get('256-PNG', ''))
            for asset in data['assets']
        ]
        return materials, data['totalResults']

    @classmethod
    def download(cls, material: RemoteMaterial, resolution: str, fmt: str, dest_dir: Path,
                 progress: ProgressCallback | None=None,
                 cancel: CancelToken | None=None) -> Path:
        """Download the material's zip into `dest_dir`, returning its path."""
        data = fetch_json(f'{cls._API}?{urllib.parse.urlencode({"id": material.id, "include": "downloads"})}',
                          cancel)
        downloads = {d['attributes']: d for d in data['assets'][0]['downloads']
                     if d.get('extension') == 'zip'}
        choice = _pick(downloads, resolution, fmt)
        if choice is None:
            raise RuntimeError(f'{material.name} has no zip download')

        dest = dest_dir / f'{material.id}_{choice}.zip'
        part = dest.with_name(dest.name + '.part')
        try:
            _download_file(downloads[choice]['url'], part, progress, cancel,
                           total=downloads[choice].get('size', 0))
            part.replace(dest)
        finally:
            part.unlink(missing_ok=True)
        return dest


class PolyHaven:
    name = 'Poly Haven'
    url = 'https://polyhaven.com'
    _API = 'https://api.polyhaven.com'

    # Poly Haven map name -> file name part, for the maps mattra can use
    _MAPS = ('Diffuse', 'nor_gl', 'Rough', 'Metal', 'AO', 'Displacement')

    _assets: list[tuple[RemoteMaterial, str]] | None = None  # (material, searchable text)
    _assets_lock = threading.Lock()

    @classmethod
    def _all(cls, cancel: CancelToken | None=None) -> list[tuple[RemoteMaterial, str]]:
        """Every texture, most downloaded first. Fetched once; the API has no search."""
        with cls._assets_lock:
            if cls._assets is None:
                data = fetch_json(f'{cls._API}/assets?t=textures', cancel)
                ordered = sorted(data.items(), key=lambda item: -item[1].get('download_count', 0))
                cls._assets = [
                    (RemoteMaterial('polyhaven', asset_id, info.get('name', asset_id),
                                    info.get('thumbnail_url', '')),
                     ' '.join([asset_id, info.get('name', ''), *info.get('tags', []),
                               *info.get('categories', [])]).lower())
                    for asset_id, info in ordered
                ]
            return cls._assets

    @classmethod
    def search(cls, query: str, offset: int, limit: int,
               cancel: CancelToken | None=None) -> tuple[list[RemoteMaterial], int]:
        """Search materials, most downloaded first. Returns (page of materials, total results)."""
        words = query.lower().split()
        matches = [material for material, text in cls._all(cancel) if all(w in text for w in words)]
        return matches[offset:offset + limit], len(matches)

    @classmethod
    def download(cls, material: RemoteMaterial, resolution: str, fmt: str, dest_dir: Path,
                 progress: ProgressCallback | None=None,
                 cancel: CancelToken | None=None) -> Path:
        """
        Download the material's maps and pack them into a zip in `dest_dir` (Poly Haven serves
        each map separately), returning its path.
        """
        files = fetch_json(f'{cls._API}/files/{material.id}', cancel)

        picks = []  # (url, file size)
        chosen_res = None
        for map_name in cls._MAPS:
            by_res = files.get(map_name)
            if not by_res:
                continue
            # Same choice rules as ambientCG, over this map's '<res>-<format>' options
            options = {f'{res.upper()}-{f.upper()}': entry for res, formats in by_res.items()
                       for f, entry in formats.items() if f.upper() in FORMATS}
            choice = _pick(options, resolution, fmt)
            if choice is None:
                continue
            picks.append((options[choice]['url'], options[choice].get('size', 0)))
            chosen_res = chosen_res or choice.split('-')[0]
        if not picks:
            raise RuntimeError(f'{material.name} has no downloadable maps')

        dest = dest_dir / f'{material.id}_{chosen_res.lower()}.zip'
        staging = dest_dir / f'.{dest.stem}.part'
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            total = sum(size for _, size in picks)
            done = 0
            for url, size in picks:
                done += _download_file(url, staging / url.rsplit('/', 1)[1], progress, cancel,
                                       done_before=done, total=total)

            part = dest.with_name(dest.name + '.part')
            with zipfile.ZipFile(part, 'w', zipfile.ZIP_DEFLATED) as zf:
                for f in sorted(staging.iterdir()):
                    zf.write(f, f'textures/{f.name}')
            part.replace(dest)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
            dest.with_name(dest.name + '.part').unlink(missing_ok=True)
        return dest


SOURCES = {'ambientcg': AmbientCG, 'polyhaven': PolyHaven}
