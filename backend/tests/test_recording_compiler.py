"""Contract tests for converting an in-memory recording to editable Lua."""
import unittest

from backend.scripts.recording import compile_recording
from backend.storage.errors import StorageError


def click(x=4, scope="window"):
    return {"kind": "click", "button": "left", "point": {
        "version": 1, "scope": scope, "strategy": "relative", "reference_width": 10,
        "reference_height": 20, "x": x, "y": 8}}


def row(seq, start, end, action):
    return {"seq": seq, "start_ms": start, "end_ms": end, "action": action}


def compile_rows(*rows, interrupted=()):
    return compile_recording({"scope": "window", "actions": list(rows), "interrupted": list(interrupted)})


class RecordingCompilerTests(unittest.TestCase):
    def test_motion_and_incomplete_items_preserve_elapsed_time_and_locations(self):
        rows = [row(0, 125, 170, click()), row(1, 172, 215, click(scope="region")),
                row(2, 225, 225, {"kind": "scroll", "point": click()["point"], "delta": -60})]
        result = compile_rows(*rows, interrupted=[{"after": 2, "reason": "key_focus_lost"}])
        self.assertEqual(result["action_count"], 2)
        self.assertEqual(result["omitted"], [{"after": 1, "reason": "anchor.scope"},
                                             {"after": 2, "reason": "key_focus_lost"}])
        waits = [s for s in result["source"].splitlines() if s.startswith("wait")]
        self.assertEqual(waits, ["wait(0.125)", "wait(0.055)"])
        self.assertIn(',"left",1,45)', result["source"])
        self.assertIn(',-60)', result["source"])

    def test_only_complete_key_groups_compile(self):
        press = {"offset_ms": 0, "key": "a", "down": True}
        release = dict(press, offset_ms=9, down=False)
        scan = {"scan_code": 28, "extended": True, "down": True, "offset_ms": 0}
        for edges in ([press, dict(press, offset_ms=4), release], [scan, dict(scan, offset_ms=9, down=False)]):
            result = compile_rows(row(0, 0, 9, {"kind": "key_sequence", "events": edges}))
            self.assertFalse(result["omitted"])
            self.assertTrue(result["source"].startswith('wait(0.000)\nrecorded_keys("window",'))
        for edges in ([press, press], [release, press], [press, dict(release, key="a') os.exit()")],
                      [press, dict(release, down=0)], [scan, dict(scan, extended=False, down=False, offset_ms=9)]):
            with self.subTest(edges=edges):
                result = compile_rows(row(0, 0, 9, {"kind": "key_sequence", "events": edges}))
                self.assertEqual((result["action_count"], result["source"]), (0, ""))
                self.assertEqual(len(result["omitted"]), 1)

    def test_metadata_never_enters_lua(self):
        malformed = click()
        malformed["point"]["binding_id"] = "transient binding"
        result = compile_rows(row(0, 0, 0, malformed), interrupted=[{"after": 0, "reason": "external text"}])
        self.assertEqual(result["source"], "")
        self.assertEqual([v["reason"] for v in result["omitted"]], ["external text", "anchor"])

    def test_rejects_corrupt_timeline(self):
        for rows in ([row(False, 0, 1, click())], [row(1, 0, 1, click())],
                     [row(0, 1, 3, click()), row(1, 2, 4, click())], [row(0, 0, 1800001, click())]):
            with self.subTest(rows=rows), self.assertRaises(StorageError) as error:
                compile_rows(*rows)
            self.assertEqual(error.exception.code, "INVALID_PARAMS")
