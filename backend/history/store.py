"""Bounded execution history. Only the writer thread touches log files during execution."""
import json
import os
import queue
import threading
import time
from collections import deque
from concurrent.futures import Future, TimeoutError
from pathlib import Path
from uuid import UUID

from ..storage.errors import StorageError
from ..storage.paths import DataRoot

TERMINAL = {"completed", "failed", "cancelled"}
LEVEL = {"debug": 0, "info": 1, "warning": 2, "error": 3}
SUMMARY_FIELDS = ("execution_id", "kind", "script_id", "status", "reason", "started_at", "updated_at",
                  "iteration", "effective_hz", "throttled")


def page_bounds(params):
    offset, limit = params.get("offset", 0), params.get("limit", 100)
    if type(offset) is not int or offset < 0:
        raise StorageError("INVALID_PARAMS", "起始位置必须为非负整数", field="offset")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise StorageError("INVALID_PARAMS", "每页 1 至 500 项", field="limit")
    return offset, limit


class HistoryStore:
    def __init__(self, directory, warn):
        self.root = DataRoot(str(directory))
        self.warn = warn
        self.lock = threading.Lock()
        self.queue = queue.Queue(maxsize=2048)
        self.closing = False
        self.summaries, self.pending, self.fallback = {}, {}, {}
        self.degraded = set()
        self.prune = set()
        self.state_retries = {}
        self.warned = False
        try:
            directory = self.path("")
            directory.mkdir(parents=True, exist_ok=True)
            for path in directory.glob("*.json"):
                try:
                    key = str(UUID(path.stem))
                    state = json.loads(self.path(key + ".json").read_bytes())
                    if key != state["execution_id"] or not all(k in state for k in SUMMARY_FIELDS):
                        raise ValueError("invalid summary")
                    if state["status"] not in TERMINAL:
                        state.update(status="failed", reason="engine_exit", error_code="ENGINE_UNAVAILABLE")
                        self.pending[key] = state
                    self.summaries[key] = state
                except (KeyError, ValueError, OSError, StorageError):
                    self.warning("部分历史摘要无法读取")
            self._trim()
        except (OSError, StorageError):
            self.warning("历史目录不可写，将保留内存摘要")
        self.writer = threading.Thread(target=self._run, name="history-writer", daemon=True)
        self.writer.start()

    def path(self, filename):
        return Path(self.root.resolve("history/" + filename if filename else "history"))

    def warning(self, message):
        if not self.warned:
            self.warned = True
            self.warn(message)

    def _trim(self):
        terminal = sorted((v for v in self.summaries.values() if v["status"] in TERMINAL),
                          key=lambda v: (v["updated_at"], v["execution_id"]), reverse=True)
        for item in terminal[100:]:
            key = item["execution_id"]
            self.summaries.pop(key, None)
            self.fallback.pop(key, None)
            self.pending.pop(key, None)
            self.degraded.discard(key)
            self.prune.add(key)

    def state(self, state, sample_no, root_script=None):
        row = {name: state[name] for name in SUMMARY_FIELDS}
        row.update(sample_no=sample_no, error_code=(state["error"] or {}).get("code"))
        if root_script is not None:
            row.update(script_id=root_script["id"], script_name=root_script["name"])
        # Script-supplied stop text may hold arguments. Persist only engine-defined reasons.
        if row["status"] == "cancelled" and row["reason"] not in ("user_requested", "hotkey", "engine_exit", "script_requested"):
            row["reason"] = "script_requested"
        with self.lock:
            if self.closing:
                return
            key = row["execution_id"]
            self.summaries[key] = self.pending[key] = row
            if row["status"] in TERMINAL:
                self._trim()
            try:
                self.queue.put_nowait(("wake", None, None))
            except queue.Full:
                pass

    def log(self, entry, settings):
        if LEVEL.get(entry["level"], 1) < LEVEL.get(settings.get("level"), 1):
            return
        key = entry["execution_id"]
        with self.lock:
            if self.closing:
                return
            self.fallback.setdefault(key, deque(maxlen=1000)).append(entry)
            try:
                self.queue.put_nowait(("log", (entry, settings), None))
                return
            except queue.Full:
                self.degraded.add(key)
        self.warning("日志写入队列已满，最近日志可从内存查看")

    def _files(self, key):
        directory = self.path("")
        files = []
        for entry in directory.glob(key + ".jsonl*"):
            suffix = entry.name[len(key + ".jsonl"):]
            if not suffix or suffix.startswith(".") and suffix[1:].isdigit():
                files.append((int(suffix[1:] or 0), self.path(entry.name)))
        return sorted(files, reverse=True)

    def _append(self, entry, settings):
        key = entry["execution_id"]
        path = self.path(key + ".jsonl")
        raw = (json.dumps(entry, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if path.exists() and path.stat().st_size + len(raw) > settings.get("rotate_bytes", 10485760):
            for number, segment in self._files(key):
                if number + 1 >= settings.get("file_count", 5):
                    segment.unlink()
                else:
                    os.replace(segment, self.path(key + f".jsonl.{number + 1}"))
        with path.open("ab") as handle:
            handle.write(raw)

    def _flush_states(self, force=False):
        now = time.monotonic()
        with self.lock:
            pending, self.pending = self.pending, {}
            prune, self.prune = self.prune, set()
            # Read the latest row: a retry must never reinstate an earlier execution state.
            for key, (due, _) in list(self.state_retries.items()):
                if key not in self.summaries:
                    del self.state_retries[key]
                elif force or due <= now:
                    pending[key] = self.summaries[key]
        for key, row in pending.items():
            try:
                temp = self.path(key + ".json.tmp")
                with temp.open("wb") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, self.path(key + ".json"))
                self.state_retries.pop(key, None)
            except (OSError, StorageError):
                delay = min(30, self.state_retries.get(key, (0, .125))[1] * 2)
                self.state_retries[key] = time.monotonic() + delay, delay
                self.warning("历史摘要未能写盘，仍可在本次会话中查看")
        for key in prune:
            try:
                self._clear(key)
                self.path(key + ".json").unlink(missing_ok=True)
            except (OSError, StorageError):
                self.warning("历史清理失败")

    def _clear(self, key):
        for _, path in self._files(key):
            path.unlink()
        with self.lock:
            self.fallback.pop(key, None)
            self.degraded.discard(key)

    @staticmethod
    def _page(items, total, offset):
        used = 0
        for index, row in enumerate(items):
            used += len(json.dumps(row, ensure_ascii=False).encode("utf-8")) + 1
            if used > 1024 * 1024:
                return dict(items=items[:index], total=total, truncated=True, next_offset=offset+index)
        return dict(items=items, total=total)

    def _read(self, key, offset, limit):
        if key in self.degraded:
            with self.lock:
                rows = list(self.fallback.get(key, ()))
            return self._page(rows[offset:offset + limit], len(rows), offset)
        items, total = [], 0
        for _, path in self._files(key):
            with path.open("rb") as handle:
                for line in handle:
                    if offset <= total < offset + limit:
                        try:
                            items.append(json.loads(line))
                        except (ValueError, UnicodeError) as exc:
                            raise StorageError("DATA_CORRUPT", "历史日志包含损坏的记录") from exc
                    total += 1
        return self._page(items, total, offset)

    def _run(self):
        while True:
            self._flush_states()
            due = min((when for when, _ in self.state_retries.values()), default=None)
            try:
                kind, payload, future = self.queue.get(timeout=None if due is None else max(0, due - time.monotonic()))
            except queue.Empty:
                continue
            except queue.ShutDown:
                self._flush_states(force=True)
                return
            try:
                if kind == "log":
                    self._append(*payload)
                elif kind == "read":
                    future.set_result(self._read(*payload))
                elif kind == "clear":
                    self._clear(payload)
                    future.set_result({"ok": True})
            except (OSError, StorageError, ValueError) as exc:
                if kind == "log":
                    self.degraded.add(payload[0]["execution_id"])
                self.warning("执行日志存储失败；最近日志已保留在内存中")
                if future:
                    future.set_exception(exc if isinstance(exc, StorageError) else StorageError("STORAGE_ERROR", "历史日志读写失败"))

    def command(self, name, params):
        allowed = {"offset", "limit"} if name == "list_execution_history" else {"execution_id"}
        if name == "get_execution_logs":
            allowed.update(("offset", "limit"))
        for field in params.keys() - allowed:
            raise StorageError("INVALID_PARAMS", "存在未声明的参数", field=field)
        offset, limit = page_bounds(params)
        with self.lock:
            if self.closing:
                raise StorageError("INVALID_STATE", "历史存储已关闭")
            if name == "list_execution_history":
                rows = sorted(self.summaries.values(), key=lambda v:(v["updated_at"], v["execution_id"]), reverse=True)
                return dict(items=rows[offset:offset + limit], total=len(rows))
            key = params.get("execution_id")
            if not isinstance(key, str) or key not in self.summaries:
                raise StorageError("INVALID_STATE", "执行历史不存在", field="execution_id")
            if name == "clear_execution_logs" and self.summaries[key]["status"] not in TERMINAL:
                raise StorageError("INVALID_STATE", "请先停止执行再清除日志")
        future = Future()
        try:
            entry = ("read", (key, offset, limit), future) if name == "get_execution_logs" else ("clear", key, future)
            self.queue.put(entry, timeout=.3)
            return future.result(timeout=3)
        except queue.ShutDown as exc:
            raise StorageError("INVALID_STATE", "历史存储已关闭") from exc
        except (queue.Full, TimeoutError) as exc:
            raise StorageError("BUSY", "日志存储繁忙，请重新查询", retryable=True) from exc

    def close(self):
        with self.lock:
            self.closing = True
            self.queue.shutdown()
        # The host bounds process shutdown. A normal EOF waits for accepted writes.
        self.writer.join()
