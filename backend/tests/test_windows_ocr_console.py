"""Protect the foreground when Paddle starts console utilities during its first inference."""
import ctypes
import json
import multiprocessing
import os
import subprocess
import sys
import unittest


def _observe_console(connection):
    # The dependency starts ordinary console subprocesses without custom flags.
    probe = """
import ctypes, json, os
from ctypes import wintypes
members = (wintypes.DWORD * 32)()
count = ctypes.windll.kernel32.GetConsoleProcessList(members, len(members))
ctypes.windll.kernel32.GetConsoleWindow.restype = wintypes.HWND
window = ctypes.windll.kernel32.GetConsoleWindow()
print(json.dumps([os.getpid(), list(members[:count]), bool(ctypes.windll.user32.IsWindowVisible(wintypes.HWND(window)))]))
"""
    try:
        result = subprocess.check_output([sys.executable, "-I", "-S", "-c", probe], timeout=10)
        connection.send((os.getpid(), json.loads(result)))
    finally:
        connection.close()


@unittest.skipUnless(sys.platform == "win32", "Windows console inheritance")
class OcrConsoleTest(unittest.TestCase):
    def test_console_child_shares_workers_invisible_console(self):
        from backend.worker_process import OcrWorkerProcess
        context = multiprocessing.get_context("spawn")
        reader, writer = context.Pipe(duplex=False)
        worker = OcrWorkerProcess(target=_observe_console, args=(writer,))
        try:
            worker.start()
            writer.close()
            self.assertTrue(reader.poll(15), "OCR worker did not return a console observation")
            pid, (child, members, visible) = reader.recv()
            self.assertEqual(pid, worker.pid)
            self.assertIn(child, members)
            self.assertIn(pid, members)
            self.assertFalse(visible)
            worker.join(5)
            self.assertEqual(worker.exitcode, 0)
        finally:
            writer.close()
            reader.close()
            if worker.pid is not None:
                if worker.is_alive():
                    worker.terminate()
                worker.join(5)
                worker.close()
