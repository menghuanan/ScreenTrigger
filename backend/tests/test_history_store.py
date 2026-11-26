"""Full Log API retention, rotation and recovery, including write failures during stop."""
import errno
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.history.store import HistoryStore
from backend.script.controller import stamp
from backend.storage.errors import StorageError
from backend.tests import test_execution_runtime as fixtures


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.warnings = []
        self.archive = HistoryStore(self.temp.name, self.warnings.append)
        self.addCleanup(lambda: self.archive.close())
        self.key = str(uuid4())
        self.state = dict(execution_id=self.key, kind="script", script_id="s", status="running",
                          reason=None, started_at=stamp(), updated_at=stamp(), iteration=1,
                          effective_hz=None, throttled=False, error=None, result={"private":"secret"})
        self.archive.state(self.state, None)

    def add(self, i, size=100000):
        self.archive.log(dict(timestamp=stamp(),execution_id=self.key,level="info",event="measure",
                              message=f"message-{i}",details={}),dict(rotate_bytes=size,file_count=3))

    def read(self, **page):
        return self.archive.command("get_execution_logs", dict(execution_id=self.key, **page))

    def test_rotation_pagination_clear_and_reopen(self):
        for i in range(12):
            self.add(i, 250)
        page=self.read(offset=1,limit=1)
        self.assertEqual((page["total"],len(page["items"])),(3,1))
        self.assertEqual(page["items"][0]["message"],"message-10")
        with self.assertRaises(StorageError):
            self.archive.command("clear_execution_logs",{"execution_id":self.key})
        self.archive.state(dict(self.state,status="completed"), 7)
        self.archive.close()
        self.archive=HistoryStore(self.temp.name,self.warnings.append)
        rows=self.archive.command("list_execution_history",{})["items"]
        self.assertEqual((len(rows),rows[0]["status"],rows[0]["sample_no"]),(1,"completed",7))
        self.assertNotIn("secret",json.dumps(rows))
        self.archive.command("clear_execution_logs",{"execution_id":self.key})
        self.assertEqual(self.read(),{"items":[],"total":0})
        with self.assertRaises(StorageError): self.read(offset=-1)
        with self.assertRaises(StorageError): self.archive.command("get_execution_logs",{"execution_id":"../config"})

    def test_previous_active_never_resumed_and_bounded_history(self):
        self.add(0); self.read()
        self.archive.close()
        self.archive=HistoryStore(self.temp.name,self.warnings.append)
        row=self.archive.command("list_execution_history",{})["items"][0]
        self.assertEqual((row["status"],row["reason"]),("failed","engine_exit"))
        for _ in range(105):
            self.archive.state(dict(self.state,execution_id=str(uuid4()),status="completed",updated_at=stamp()),None)
        self.assertEqual(self.archive.command("list_execution_history",{})["total"],100)

    def test_disk_full_falls_back_and_writer_never_blocks_stop(self):
        started, release = threading.Event(),threading.Event()
        def full(*_):
            started.set(); release.wait(2)
            raise OSError(errno.ENOSPC, "injected disk full")
        with patch.object(self.archive,"_append",full):
            self.add(1); self.assertTrue(started.wait(1))
            before=time.monotonic()
            self.archive.state(dict(self.state,status="cancelled",reason="user_requested"),None)
            self.assertLess(time.monotonic()-before,.1)
            release.set()
            self.assertEqual(self.read()["items"][0]["message"],"message-1")
        self.assertEqual(len(self.warnings),1)

    def test_close_drains_a_full_queue_and_terminal_state_before_reopen(self):
        self.read()
        archive = self.archive
        archive.queue.maxsize = 1
        started, release = threading.Event(), threading.Event()
        append = archive._append
        def slow_append(*args):
            started.set()
            if not release.wait(5):
                raise AssertionError("fixture did not release the writer")
            append(*args)
        with patch.object(archive, "_append", slow_append):
            self.add(1)
            self.assertTrue(started.wait(2))
            self.add(2)
            self.assertTrue(archive.queue.full())
            archive.state(dict(self.state, status="cancelled", reason="user_requested"), None)
            closers = [threading.Thread(target=archive.close) for _ in range(2)]
            for closer in closers:
                closer.start()
            try:
                # Exceed the previous close deadline while the writer owns the first log.
                closers[0].join(1.1)
                self.assertTrue(all(closer.is_alive() for closer in closers))
            finally:
                release.set()
                for closer in closers:
                    closer.join(3)
        self.assertFalse(any(closer.is_alive() for closer in closers))
        self.assertFalse(archive.writer.is_alive())
        archive.close()
        self.archive = HistoryStore(self.temp.name, self.warnings.append)
        state, = self.archive.command("list_execution_history", {})["items"]
        self.assertEqual((state["status"],state["reason"]), ("cancelled","user_requested"))
        self.assertEqual([r["message"] for r in self.read()["items"]], ["message-1","message-2"])

    def test_locked_summary_retries_the_latest_terminal_when_idle(self):
        self.read()
        path = self.archive.path(self.key + ".json")
        from os import replace
        locked = threading.Event()
        recovered = threading.Event()

        def transient_lock(source, destination):
            if destination == path and not locked.is_set():
                locked.set()
                raise PermissionError("fixture holds the summary open")
            replace(source, destination)
            if destination == path:
                recovered.set()

        with patch("backend.history.store.os.replace", side_effect=transient_lock):
            self.archive.state(dict(self.state, status="cancelled", reason="user_requested"), None)
            self.assertTrue(locked.wait(1))
            self.assertTrue(recovered.wait(3))
        self.assertEqual(json.loads(path.read_bytes())["status"], "cancelled")
        self.assertFalse(path.with_suffix(".json.tmp").exists())


class AuditPrivacy(unittest.TestCase):
    setUp = fixtures.ExecutionRuntime.setUp
    cleanup_roots = fixtures.ExecutionRuntime.cleanup_roots
    save = fixtures.ExecutionRuntime.save
    await_state = fixtures.ExecutionRuntime.await_state

    def test_new_execution_uses_saved_input_defaults_and_explicit_overrides(self):
        self.controller.settings = lambda: {"input": {"move_duration_ms": 119, "type_interval_ms": 73}}
        script = self.save('move_to(120,85); type_text("one"); type_text("two",0)')
        run = self.controller.execute(dict(script_id=script, display_id="d"))["execution_id"]
        self.await_state(run, "completed")
        actions = [event[3] for event in self.input.actions]
        self.assertEqual((actions[0]["duration_ms"], actions[1]["interval_ms"], actions[2]["interval_ms"]), (119,73,0))

    def test_no_input_or_args_persisted_and_normal_logs_never_pushed(self):
        archive=HistoryStore(self.boot.data_dir, lambda message: self.fail(message))
        self.controller.archive=archive
        self.addCleanup(archive.close)
        script=self.save('type_text(args.value,0); return args.value',
                         [dict(name="value",type="string",required=True,default=None,description="")])
        run=self.controller.execute(dict(script_id=script,display_id="d",args=dict(value="UNIQUE-TYPED-TEXT")))["execution_id"]
        self.await_state(run,"completed")
        logs=self.controller.command("get_execution_logs",dict(execution_id=run))
        actions=[row for row in logs["items"] if row["event"]=="input_action"]
        self.assertEqual(len(actions),1)
        self.assertEqual(actions[0]["details"]["action_id"],self.input.actions[0][1])
        self.assertTrue(actions[0]["details"]["dispatched"])
        self.assertFalse(any(name=="log_message" for name,_ in self.events))
        raw=b"".join(p.read_bytes() for p in (Path(self.boot.data_dir)/"history").glob("*"))
        self.assertNotIn(b"UNIQUE-TYPED-TEXT",raw)
