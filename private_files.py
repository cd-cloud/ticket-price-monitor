from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None


PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def ensure_private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        try:
            path.chmod(PRIVATE_DIR_MODE)
        except OSError:
            pass
    return path


def make_file_private(path: Path) -> Path:
    if os.name != "nt":
        try:
            path.chmod(PRIVATE_FILE_MODE)
        except OSError:
            pass
    return path


@contextmanager
def interprocess_lock(path: Path):
    """Serialize a small file-based critical section across local processes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, PRIVATE_FILE_MODE)
    windows_locked = False
    try:
        make_file_private(path)
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - exercised on Windows
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
            windows_locked = True
        yield
    finally:
        if fcntl is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except OSError:
                pass
        elif windows_locked:  # pragma: no cover - exercised on Windows
            try:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(descriptor)


def debug_capture_enabled() -> bool:
    return os.getenv("FLIGHT_TRACKER_CAPTURE_DEBUG_PAGES", "").strip().lower() in {
        "1", "true", "yes", "on"
    }


def debug_capture_directory(runtime_dir: Path | None, name: str) -> Path | None:
    if runtime_dir is None or not debug_capture_enabled():
        return None
    return ensure_private_directory(Path(runtime_dir) / name)


def atomic_write_bytes(path: Path, data: bytes, *, mode: int = PRIVATE_FILE_MODE) -> Path:
    ensure_private_directory(path.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        if os.name != "nt":
            try:
                os.fchmod(descriptor, mode)
            except OSError:
                pass
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            try:
                path.chmod(mode)
            except OSError:
                pass
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, text.encode(encoding))
