"""Monitor clocks and policy, plus the public controller with real Lua child VMs."""
import threading
import unittest
from types import SimpleNamespace

from backend.monitor.scheduler import DEFAULT_MONITOR, run_monitor
from backend.storage.errors import StorageError
from backend.tests import test_execution_runtime as fixtures
from backend.types.runtime import validate_monitor_config


class ClockRoot:
    def __init__(self, **settings):
        self.config = validate_monitor_config(dict(DEFAULT_MONITOR, target_id="target", display_id="d", **settings))
        self.frequency = self.config.frequency_hz
        self.time = self.iteration = 0
        self.calls, self.events = [], []
        self.id = "clock"
        self.controller = SimpleNamespace(emit=lambda name, payload: self.events.append((name, payload)))
        self.gate = SimpleNamespace(checkpoint=lambda: None)

    def active_time(self):
        return self.time

    def wait(self, duration):
        self.time += duration

    def call(self, script, args):
        self.calls.append((script, args))

    def log(self, *args, **details):
        pass


def detection(found):
    return dict(found=found, sample_no=7, matches=[{"bbox": {"x": -10}}] if found else [])


class MonitorClockTests(unittest.TestCase):
    def test_first_tick_and_no_backlog_at_four_rates(self):
        for hz in (1, 5, 10, 30):
            with self.subTest(hz=hz):
                root = ClockRoot(frequency_hz=hz)
                ticks = []
                def sample(_):
                    ticks.append(root.time)
                    root.time += 1.25 / hz
                    return detection(len(ticks) == 3)
                result, reason = run_monitor(root, sample)
                self.assertEqual(reason, "target_detected")
                self.assertEqual(result["match_count"], 1)
                self.assertEqual(root.calls, [])
                self.assertAlmostEqual(ticks[0], 0)
                self.assertAlmostEqual(ticks[1], 2 / hz)
                self.assertAlmostEqual(ticks[2], 4 / hz)
                self.assertAlmostEqual(root.effective_hz, hz / 2)
                self.assertTrue(root.throttled)

    def test_edges_every_match_and_timeout_only_failure(self):
        for trigger, hits in (("rising_edge", 2), ("every_match", 4)):
            with self.subTest(trigger=trigger):
                root = ClockRoot(on_match="run_script", action_script_id="a", repeat=True,
                                 trigger=trigger, frequency_hz=10, cooldown_ms=300, wait_timeout_ms=210,
                                 on_timeout="run_script", timeout_script_id="f")
                samples = iter((True, True, False, True, True))
                result, reason = run_monitor(root, lambda _: detection(next(samples, False)))
                self.assertEqual((result["match_count"], reason), (hits, "timeout_handled"))
                self.assertEqual([k for k, _ in root.calls], ["a"] * hits + ["f"])
                self.assertEqual(root.calls[-1][1]["failure"]["attempts"], 1)

    def test_retries_expire_before_next_tick(self):
        root = ClockRoot(wait_timeout_ms=50, on_timeout="retry", max_retries=2, retry_delay_ms=100)
        ticks = []
        with self.assertRaises(StorageError) as caught:
            run_monitor(root, lambda _: ticks.append(root.time) or detection(False))
        self.assertEqual(caught.exception.details["attempts"], 3)
        self.assertEqual(caught.exception.code, "WAIT_TIMEOUT")
        self.assertEqual(len(ticks), 3)
        self.assertAlmostEqual(root.time, .35)


class MonitorIntegration(unittest.TestCase):
    setUp = fixtures.ExecutionRuntime.setUp
    cleanup_roots = fixtures.ExecutionRuntime.cleanup_roots
    save = fixtures.ExecutionRuntime.save
    await_state = fixtures.ExecutionRuntime.await_state

    def setUp(self):
        fixtures.ExecutionRuntime.setUp(self)
        ref = self.store.put(fixtures.RawImage(12, 12, bytes(432)), "png")
        self.target = self.targets.create_target(dict(name="monitor", image_id=ref.image_id,
            anchor_condition_id="c", conditions={"op":"all","children":[{"id":"c","type":"template","params":{"threshold":.8}}]}))["id"]

    def start(self, **options):
        return self.controller.command("start_monitor", dict(display_id="d", target_id=self.target, **options))["execution_id"]

    def test_default_stop_and_child_shared_lease(self):
        self.vision.run_sync.return_value = detection(True)
        root = self.start(client_run_id="dedup")
        self.assertEqual(self.start(client_run_id="dedup"), root)
        state = self.await_state(root, "completed")
        self.assertEqual((state["kind"], state["script_id"], state["result"]["match_count"]), ("monitor", None, 1))
        self.assertFalse(self.input.actions)
        action = self.save('if args.match.sample_no==7 then press_key("a") end',
            [dict(name="match", type="object", required=True, default=None, description="")])
        second = self.start(on_match="run_script", action_script_id=action, action_args={"match":{"sample_no":0}})
        self.await_state(second, "completed")
        self.assertEqual([a[0] for a in self.input.actions], [second])
        self.assertCountEqual(self.input.releases, [root, second])

    def test_preflight_and_recognition_action_errors(self):
        self.vision.run_sync.side_effect = StorageError("MODEL_UNAVAILABLE", "缺模型")
        wrong = self.save("return 1")
        with self.assertRaises(StorageError):
            self.start(on_timeout="run_script", timeout_script_id=wrong)
        self.assertFalse(self.input.leases)
        failure = self.save('press_key("b")', [dict(name="failure", type="object", required=True, default=None, description="")])
        root = self.start(on_timeout="run_script", timeout_script_id=failure)
        self.assertEqual(self.await_state(root, "failed")["error"]["code"], "MODEL_UNAVAILABLE")
        self.vision.run_sync.side_effect = None
        self.vision.run_sync.return_value = detection(True)
        action = self.save('error("broken")', [dict(name="match", type="object", required=True, default=None, description="")])
        root = self.start(on_match="run_script", action_script_id=action, on_timeout="run_script", timeout_script_id=failure)
        err = self.await_state(root, "failed")["error"]
        self.assertEqual(err["details"]["call_stack"], [action])
        self.assertFalse(self.input.actions)

    def test_cancel_during_inference_discards_late_hit(self):
        entered, finish = threading.Event(), threading.Event()
        def infer(*args, **kwargs):
            entered.set()
            finish.wait(2)
            return detection(True)
        self.vision.run_sync.side_effect = infer
        root = self.start()
        self.assertTrue(entered.wait(2))
        self.controller.command("stop_script", dict(execution_id=root))
        finish.set()
        self.await_state(root, "cancelled")
        self.assertFalse(any(name == "target_detected" for name, _ in self.events))
        self.assertFalse(self.input.actions)

    def test_pause_discards_inflight_frame_and_root_deadline_wins(self):
        entered, finish = threading.Event(), threading.Event()
        count = 0
        def infer(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                finish.wait(2)
            return detection(count == 1)
        self.vision.run_sync.side_effect = infer
        root = self.start(total_timeout_ms=250, wait_timeout_ms=1000, frequency_hz=30)
        self.assertTrue(entered.wait(2))
        self.controller.command("pause_script", dict(execution_id=root))
        finish.set()
        self.controller.command("resume_script", dict(execution_id=root))
        self.assertEqual(self.await_state(root, "failed")["error"]["code"], "SCRIPT_TIMEOUT")
        self.assertGreater(count, 1)
        self.assertFalse(any(name == "target_detected" for name, _ in self.events))
