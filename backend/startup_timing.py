"""Local opt-in timing for a single startup snapshot, on the Windows QPC clock."""
import json
import os
import stat
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path


_stream = None
_session = None
_lock = threading.Lock()
current_operation = ContextVar("startup_recognition_operation", default=None)


def enabled():
    return _stream is not None


@contextmanager
def session(data_dir, session_id):
    global _stream, _session
    if not data_dir:
        yield
        return
    folder = Path(data_dir) / "diagnostics"
    marker = folder / "startup-timing.enabled"
    try:
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size or any(
            p.is_symlink() or getattr(p.lstat(), "st_file_attributes", 0) & 0x400
            for p in (folder.parent, folder, marker)
        ):
            raise ValueError("Invalid local timing marker")
        journal = (folder / "startup-timing.jsonl").open("x", encoding="utf8", buffering=1)
    except (OSError, ValueError):
        yield
        return
    _session, _stream = session_id, journal
    try:
        yield
    finally:
        with _lock:
            _stream = _session = None
            try:
                journal.close()
            except OSError:
                pass


def mark(event, *, qpc_ns=None, **fields):
    if _stream is None:
        return
    when = time.perf_counter_ns() if qpc_ns is None else qpc_ns
    with _lock:
        if _stream is not None:
            row = dict(event=event, qpc_ns=when, pid=os.getpid(), session_id=_session, **fields)
            try:
                _stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            except OSError:
                pass


def operation(event, **fields):
    context = current_operation.get()
    if context is not None:
        mark(event, **context, **fields)
