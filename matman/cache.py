"""A size-limited disk cache, used for online material thumbnails."""

import hashlib
import os
import threading
import time
from pathlib import Path


class DiskCache:
    """
    Stores bytes in files named by a hash of their key. When the total size goes over
    `max_bytes`, the least recently used entries are deleted until it is back under
    `trim_ratio` of the limit, so trimming doesn't happen on every write.

    Safe to use from several threads, and from several app instances sharing the directory:
    writes are atomic, and an entry deleted by someone else is simply a miss.
    """

    _STALE_TEMP_SECONDS = 3600  # Temp files older than this were left by a crash

    def __init__(self, directory: Path, max_bytes: int, suffix: str='', trim_ratio: float=0.8):
        self.directory = directory
        self.max_bytes = max_bytes
        self.suffix = suffix
        self.trim_ratio = trim_ratio
        self._lock = threading.Lock()
        self._total: int | None = None  # Measured on first write

    def _path(self, key: str) -> Path:
        return self.directory / (hashlib.sha1(key.encode()).hexdigest() + self.suffix)

    def get(self, key: str) -> bytes | None:
        """The cached bytes for `key`, or None. Marks the entry as recently used."""
        path = self._path(key)
        try:
            data = path.read_bytes()
        except OSError:
            return None
        try:
            os.utime(path)  # The modification time records when it was last used
        except OSError:
            pass
        return data

    def put(self, key: str, data: bytes):
        """Store `data` for `key`, then trim the cache if it has grown too big."""
        path = self._path(key)
        temp = path.with_name(f'{path.name}.{os.getpid()}.{threading.get_ident()}.tmp')
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            try:
                previous = path.stat().st_size
            except OSError:
                previous = 0
            temp.write_bytes(data)
            os.replace(temp, path)
        except OSError:
            temp.unlink(missing_ok=True)
            return  # Caching is best effort

        with self._lock:
            if self._total is None:
                self._total = self._measure()
            else:
                self._total += len(data) - previous
            if self._total > self.max_bytes:
                self._trim()

    def remove(self, key: str):
        """Delete the entry for `key`, e.g. because its data turned out to be corrupt."""
        self._path(key).unlink(missing_ok=True)

    def size(self) -> int:
        """Total bytes currently stored."""
        with self._lock:
            return self._measure()

    def _entries(self) -> list[os.DirEntry]:
        try:
            return [e for e in os.scandir(self.directory) if e.is_file()]
        except OSError:
            return []

    def _measure(self) -> int:
        total = 0
        for entry in self._entries():
            try:
                total += entry.stat().st_size
            except OSError:
                pass
        return total

    def _trim(self):
        """Delete least recently used entries (and stale temp files) until under the target size."""
        now = time.time()
        entries = []
        for entry in self._entries():
            try:
                stat = entry.stat()
            except OSError:
                continue
            if entry.name.endswith('.tmp'):
                if now - stat.st_mtime > self._STALE_TEMP_SECONDS:
                    Path(entry.path).unlink(missing_ok=True)
                continue
            entries.append((stat.st_mtime, stat.st_size, entry.path))

        total = sum(size for _, size, _ in entries)
        target = self.max_bytes * self.trim_ratio
        for _, size, path in sorted(entries):  # Oldest first
            if total <= target:
                break
            try:
                Path(path).unlink()
                total -= size
            except FileNotFoundError:
                total -= size  # Already deleted by another instance
            except OSError:
                pass
        self._total = total
