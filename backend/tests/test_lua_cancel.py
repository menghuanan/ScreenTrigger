# -*- coding: utf-8 -*-
"""TASK-020 取消/暂停/期限闸门单测（backend/script/cancel.py）。

受管 Python（无 lupa）即可**真实执行**（注入假时钟与假 sleep）——覆盖 TEST-008 中属本任务的
「可取消 wait 不 sleep 整段 / 取消即停」子场景。
"""

from __future__ import annotations

import os
import sys
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.script.cancel import (  # noqa: E402
    CancelFlag,
    CheckpointCancelled,
    ExecutionGate,
    NullCancelToken,
    PauseFlag,
    cancellable_wait,
)


class _FakeClock:
    """可注入单调时钟。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now_ns = round(start * 1_000_000_000)

    def monotonic(self) -> float:
        return self.now_ns / 1_000_000_000

    def advance(self, seconds: float) -> None:
        self.now_ns += round(seconds * 1_000_000_000)


class _FakeSleep:
    """假 sleep：记录每次时长并推进时钟。"""

    def __init__(self, clock: _FakeClock) -> None:
        self.clock = clock
        self.calls: list = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


class CancelTokenTest(unittest.TestCase):
    def test_null_cancel_token_never_cancels(self):
        token = NullCancelToken()
        self.assertFalse(token.is_cancelled())
        self.assertIsNone(token.throw_if_cancelled())

    def test_cancel_flag_idempotent_and_throws(self):
        flag = CancelFlag()
        self.assertFalse(flag.is_cancelled())
        flag.throw_if_cancelled()  # 未取消：不抛
        flag.cancel()
        flag.cancel()  # 幂等
        self.assertTrue(flag.is_cancelled())
        with self.assertRaises(CheckpointCancelled):
            flag.throw_if_cancelled()


class PauseFlagTest(unittest.TestCase):
    def test_pause_resume_cycle(self):
        pause = PauseFlag()
        self.assertFalse(pause.is_paused())
        pause.pause()
        self.assertTrue(pause.is_paused())
        pause.resume()
        self.assertFalse(pause.is_paused())


class ExecutionGateTest(unittest.TestCase):
    def test_deadline_and_checkpoint(self):
        clock = _FakeClock()
        gate = ExecutionGate(deadline_ms=1000, monotonic=clock.monotonic)
        self.assertFalse(gate.is_cancelled())
        self.assertGreater(gate.remaining_ms(), 0)
        clock.advance(0.5)
        self.assertFalse(gate.is_cancelled())  # 500ms < 1000ms
        gate.checkpoint()  # 未超期：不抛
        clock.advance(0.5)
        self.assertTrue(gate.deadline_exceeded())
        self.assertTrue(gate.is_cancelled())
        self.assertEqual(gate.remaining_ms(), 0)
        with self.assertRaises(CheckpointCancelled):
            gate.checkpoint()

    def test_no_deadline_never_expires(self):
        clock = _FakeClock()
        gate = ExecutionGate(monotonic=clock.monotonic)
        self.assertIsNone(gate.remaining_ms())
        clock.advance(10 ** 6)
        self.assertFalse(gate.deadline_exceeded())
        self.assertFalse(gate.is_cancelled())

    def test_cancel_flag_drives_gate(self):
        flag = CancelFlag()
        gate = ExecutionGate(cancel=flag)
        gate.checkpoint()
        flag.cancel()
        self.assertTrue(gate.is_cancelled())
        with self.assertRaises(CheckpointCancelled):
            gate.checkpoint()

    def test_negative_deadline_rejected(self):
        with self.assertRaises(ValueError):
            ExecutionGate(deadline_ms=-1)


class CancellableWaitTest(unittest.TestCase):
    def test_waits_full_duration_in_slices(self):
        clock = _FakeClock()
        sleep = _FakeSleep(clock)
        gate = ExecutionGate(monotonic=clock.monotonic)
        result = cancellable_wait(
            0.2, gate, slice_ms=50, monotonic=clock.monotonic, sleep=sleep
        )
        self.assertTrue(result)
        # 0.2s / 50ms = 4 片；每片 ≤ 50ms（不 sleep 整段）
        self.assertEqual(len(sleep.calls), 4)
        self.assertLessEqual(max(sleep.calls), 0.05)

    def test_cancelled_returns_false_without_full_sleep(self):
        clock = _FakeClock()
        sleep = _FakeSleep(clock)
        flag = CancelFlag()
        gate = ExecutionGate(cancel=flag, monotonic=clock.monotonic)

        original = sleep.__call__

        def cancel_after_first(seconds: float) -> None:
            original(seconds)
            flag.cancel()  # 首片后取消

        result = cancellable_wait(
            5.0, gate, slice_ms=50, monotonic=clock.monotonic, sleep=cancel_after_first
        )
        self.assertFalse(result)
        # 只睡了一片即返回，绝不 sleep 5s 整段
        self.assertEqual(len(sleep.calls), 1)
        self.assertLess(sum(sleep.calls), 5.0)

    def test_already_cancelled_returns_immediately(self):
        clock = _FakeClock()
        sleep = _FakeSleep(clock)
        flag = CancelFlag()
        flag.cancel()
        gate = ExecutionGate(cancel=flag, monotonic=clock.monotonic)
        result = cancellable_wait(
            5.0, gate, slice_ms=50, monotonic=clock.monotonic, sleep=sleep
        )
        self.assertFalse(result)
        self.assertEqual(sleep.calls, [])

    def test_zero_seconds_finishes_immediately(self):
        clock = _FakeClock()
        sleep = _FakeSleep(clock)
        gate = ExecutionGate(monotonic=clock.monotonic)
        self.assertTrue(
            cancellable_wait(0.0, gate, monotonic=clock.monotonic, sleep=sleep)
        )
        self.assertEqual(sleep.calls, [])


if __name__ == "__main__":
    unittest.main()
