"""Integration regressions for real Lua VMs, catalog generations and concurrent root control."""
import tempfile
import json
import time
import unittest
from pathlib import Path
from unittest.mock import Mock
from backend import startup_timing

from backend.capture.ports import RawImage
from backend.capture.service import CaptureService
from backend.image.store import SessionImageStore
from backend.main import SidecarArgs, build_catalog_services
from backend.script.controller import ExecutionController, TERMINAL
from backend.storage.errors import StorageError
from backend.types.region import DisplayInfo, Rect, validate_region


class Input:
    def __init__(self):
        self.leases, self.actions, self.releases = {}, [], []
        self.faults = []

    def acquire_lease(self, execution_id, context):
        if context["region"] is not None:
            validate_region(context["region"])
        self.leases[execution_id] = context
        return "lease-" + execution_id

    def release_lease(self, execution_id):
        self.releases.append(execution_id)

    def dispatch_action(self, execution_id, lease, action_id, revision, action, *, remaining_ms=None):
        assert lease == "lease-" + execution_id
        self.actions.append((execution_id, action_id, revision, action))
        if self.faults:
            raise self.faults.pop(0)
        return True

    def query_pointer(self):
        return {"x": 120, "y": 85}


class Screen:
    def list_displays(self):
        return [DisplayInfo("d", Rect(100, 80, 40, 30), 1, True)]

    def capture_bgr(self, x, y, w, h, cancel):
        cancel.throw_if_cancelled()
        return RawImage(w, h, bytes([7, 11, 13]) * w * h)


class ExecutionRuntime(unittest.TestCase):
    def setUp(self):
        try:
            import lupa.lua54
        except ImportError:
            self.skipTest("requires Lua 5.4")
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.store = SessionImageStore("runtime")
        self.boot = SidecarArgs("runtime", data_dir=tmp.name)
        _, self.targets, self.scripts = build_catalog_services(self.boot, self.store)
        self.input, self.events = Input(), []
        self.capture = CaptureService(Screen(), Screen(), None, self.store)
        self.vision = Mock()
        self.controller = ExecutionController(self.scripts, self.capture, self.vision, self.input,
            dict, lambda n, p: self.events.append((n, p)), lambda hz, ms: None)
        self.scripts.repository.attach_reference_port(self.controller.referenced)
        self.addCleanup(self.cleanup_roots)

    def cleanup_roots(self):
        self.controller.stop_all()
        with self.controller.changed:
            self.assertTrue(self.controller.changed.wait_for(lambda: not self.controller.roots, timeout=3))

    def save(self, source, params=None):
        return self.scripts.save_script(str(time.monotonic_ns()), source, params or [])["id"]

    def start(self, script, **params):
        return self.controller.execute(dict(script_id=script, display_id="d", **params))["execution_id"]

    def await_state(self, root_id, status=None):
        with self.controller.changed:
            def wait():
                state = self.controller.command("get_script_state", dict(execution_id=root_id))["state"]
                return state if (state["status"] in TERMINAL or state["status"] == status) else None
            state = self.controller.changed.wait_for(wait, timeout=5)
            self.assertTrue(state, root_id)
            if status:
                self.assertEqual(state["status"], status, state)
            return state

    def test_opted_in_first_statement_records_the_real_session_and_scalar_result(self):
        folder = Path(self.boot.data_dir) / "diagnostics"
        folder.mkdir()
        log = folder / "startup-timing.jsonl"
        with startup_timing.session(self.boot.data_dir, "runtime"):
            self.assertFalse(startup_timing.enabled())
        self.assertFalse(log.exists())
        (folder / "startup-timing.enabled").touch()
        script = self.save("log('ST-040/first'); return 40")
        with startup_timing.session(self.boot.data_dir, "runtime"):
            execution = self.controller.command("execute_script", dict(script_id=script, display_id="d"))["execution_id"]
            self.assertEqual(self.await_state(execution, "completed")["result"], 40)
        raw = log.read_bytes()
        rows = [json.loads(line) for line in raw.splitlines()]
        self.assertEqual([r["event"] for r in rows], ["execute_accepted", "lua_first_statement", "script_finished"])
        self.assertEqual([r["qpc_ns"] for r in rows], sorted(r["qpc_ns"] for r in rows))
        self.assertTrue(all((r["session_id"], r["script_id"], r["execution_id"]) == ("runtime", script, execution) for r in rows))
        self.assertEqual((rows[-1]["status"], rows[-1]["result"]), ("completed", 40))
        with startup_timing.session(self.boot.data_dir, "runtime"):
            self.assertFalse(startup_timing.enabled())
        self.assertEqual(log.read_bytes(), raw)

    def test_ocr_timing_joins_lua_request_and_return_to_the_submitted_work(self):
        folder = Path(self.boot.data_dir) / "diagnostics"
        folder.mkdir()
        (folder / "startup-timing.enabled").touch()
        self.vision.run_sync.return_value = {"blocks": [{"text":"开始", "polygon":[{"x":0,"y":0}]}]}
        script = self.save("local b=ocr(nil,nil,'test'); local t=b[1].text; log('ST-040/ocr:'..t); return t")
        with startup_timing.session(self.boot.data_dir, "runtime"):
            execution = self.controller.command("execute_script", dict(script_id=script, display_id="d"))["execution_id"]
            self.assertEqual(self.await_state(execution, "completed")["result"], "开始")
        rows = [json.loads(line) for line in (folder / "startup-timing.jsonl").read_bytes().splitlines()]
        samples = [row for row in rows if row.get("operation_id")]
        self.assertEqual([r["event"] for r in samples], ["lua_ocr_request", "ocr_submitted", "ocr_result", "lua_ocr_return"])
        self.assertEqual(len({r["operation_id"] for r in samples}), 1)
        self.assertTrue(all(r["execution_id"] == execution for r in samples))
        self.assertEqual(samples[-1]["actual_text"], "开始")
        self.assertEqual([r["qpc_ns"] for r in samples], sorted(r["qpc_ns"] for r in samples))

    def test_explicit_region_reaches_the_lease_with_only_its_own_anchor(self):
        region = dict(space="screen_physical", display_id="d", x=105, y=85, width=20, height=12)
        script = self.save("return 42")
        for allow_exit in (False, True):
            with self.subTest(allow_exit=allow_exit):
                execution = self.start(script, region=region, allow_exit=allow_exit)
                result = self.await_state(execution, "completed")
                self.assertEqual(result["result"], 42)
                self.assertEqual(self.input.leases[execution]["region"], region)
                self.assertEqual(self.input.leases[execution]["allow_exit"], allow_exit)

    def test_modes_calls_copy_logs_and_one_lease(self):
        child = self.save('args.a[1]=9; print("child", args.a[1]); return json_array({args.a[1],json_null})',
                          [dict(name="a", type="array", required=True, default=None, description="")])
        parent = self.save('local a={1}; local v=call_script("%s", {a=a}); return {child=v,original=a[1]}' % child)
        execution = self.start(parent, mode="count", iterations=3, client_run_id="dedup")
        self.assertEqual(self.start(parent, mode="count", iterations=3, client_run_id="dedup"), execution)
        state = self.await_state(execution, "completed")
        self.assertEqual((state["result"], state["iteration"]), ({"child": [9, None], "original": 1}, 3))
        self.assertEqual(self.input.releases, [execution])
        logs = self.controller.command("get_execution_logs", dict(execution_id=execution))
        self.assertEqual([l["message"] for l in logs["items"] if l["event"] == "script_log"], ["child\t9"] * 3)

    def test_child_vm_preserves_json_containers_in_arguments_and_returns(self):
        child = self.save(
            'local incoming=args.payload; incoming.items[1]="child"; incoming.drained[1]=nil; '
            'return {incoming=incoming, made={empty=json_array({}), obj={}, '
            'items={2,json_null}, named={flag=true}, null=json_null}}',
            [dict(name="payload", type="object", required=True, default=None, description="")],
        )
        parent = self.save(
            'local payload={empty=json_array({}), obj={}, items={"parent",json_null}, '
            'drained={1}, named={flag=true}, null=json_null}; '
            'return {parent=payload, child=call_script("%s",{payload=payload})}' % child
        )
        result = self.await_state(self.start(parent), "completed")["result"]
        original = dict(empty=[], obj={}, items=["parent", None], drained=[1], named={"flag": True}, null=None)
        self.assertEqual(result, dict(
            parent=original,
            child=dict(
                incoming=dict(original, items=["child", None], drained=[]),
                made=dict(empty=[], obj={}, items=[2, None], named={"flag": True}, null=None),
            ),
        ))

    def test_target_and_group_commits_preserve_scripts_across_restart(self):
        script = self.save("return 42")
        ref = self.store.put(RawImage(12, 12, bytes(432)), "png")
        condition = {"op":"all", "children":[{"id":"c1","type":"template","params":{"threshold":.85}}]}
        target = self.targets.create_target(dict(name="sample", image_id=ref.image_id,
                                            anchor_condition_id="c1", conditions=condition))
        group = self.targets.create_group("fixtures", None)
        self.targets.update_target(target["id"], {"name": "renamed", "group_id": group["id"]}, expected_revision=1)
        _, targets, scripts = build_catalog_services(self.boot, self.store)
        self.assertEqual(scripts.load_script(script)["source"], "return 42")
        self.assertEqual(targets.get_target(target["id"])["group_id"], group["id"])
        scripts.delete_script(script, 1)
        _, _, final = build_catalog_services(self.boot, self.store)
        self.assertEqual(final.list_scripts()["total"], 0)

    def test_three_roots_pause_selected_stop_all_and_fourth_busy(self):
        script = self.save('local x=0; while true do x=x+1 end')
        ids = [self.start(script, mode="forever") for _ in range(3)]
        for root in ids:
            self.await_state(root, "running")
        with self.assertRaisesRegex(StorageError, "三个"):
            self.start(script)
        self.controller.command("pause_script", {"execution_id": ids[1]})
        self.assertEqual([self.await_state(r, status)["status"] for r, status in zip(ids, ("running", "paused", "running"))],
                         ["running", "paused", "running"])
        self.controller.command("resume_script", {"execution_id": ids[1]})
        self.controller.stop_all()
        for root in ids:
            self.await_state(root, "cancelled")
        self.assertCountEqual(ids, self.input.releases)
        self.assertEqual(len([1 for n, p in self.events if n=="script_state_changed" and p["status"]=="cancelled"]), 3)

    def test_active_time_pauses_and_root_deadline(self):
        script = self.save("while true do end")
        root = self.start(script, total_timeout_ms=180)
        self.await_state(root, "running")
        self.controller.command("pause_script", {"execution_id": root})
        time.sleep(.23)
        self.await_state(root, "paused")
        self.controller.command("resume_script", {"execution_id": root})
        state = self.await_state(root, "failed")
        self.assertEqual(state["error"]["code"], "SCRIPT_TIMEOUT")

    def test_snapshot_and_depth_and_child_error(self):
        child = self.save("return 1")
        parent = self.save('wait(.12); return call_script("%s")' % child)
        root = self.start(parent)
        self.await_state(root, "running")
        self.scripts.save_script("updated", "return 2", [], script_id=child, expected_revision=1)
        self.assertEqual(self.await_state(root, "completed")["result"], 1)
        self.assertEqual(self.await_state(self.start(child), "completed")["result"], 2)
        recurse = self.save("return call_script(SCRIPT_ID)")
        err = self.await_state(self.start(recurse), "failed")["error"]
        self.assertEqual(err["code"], "CALL_DEPTH_EXCEEDED")
        self.assertEqual(len(err["details"]["call_stack"]), 5)
        catch = self.save('local ok,e=pcall(call_script,"missing"); return {ok=ok, code=e.code}')
        self.assertEqual(self.await_state(self.start(catch), "completed")["result"],
                         {"ok": False, "code": "SCRIPT_NOT_FOUND"})

    def test_image_ownership_coordinates_pointer_and_cleanup(self):
        script = self.save('local i=capture_screen(); local x,y=get_mouse_position(); local r=get_pixel_color(x,y); '
                           'local w,h=get_screen_size(); release_image(i); release_image(i); '
                           'local ok,e=pcall(get_pixel_color,99,80); return {color=r, size={w,h}, err=e.code}')
        state = self.await_state(self.start(script), "completed")
        self.assertEqual(state["result"], dict(color={"r":13,"g":11,"b":7}, size=[40,30], err="INVALID_REGION"))
        self.assertEqual(self.store.bytes_used, 0)

    def test_target_fresh_retry_and_partial_error_cannot_be_swallowed(self):
        frames = []
        def infer(command, params, cancel, catalog):
            frames.append(params["image_id"])
            return dict(found=True, matches=[dict(center=dict(x=124,y=91), geometry_revision=len(frames))])
        self.vision.run_sync.side_effect = infer
        self.input.faults = [StorageError("STALE_FRAME", "moved")]
        result = self.await_state(self.start(self.save('return click_target("t")')), "completed")
        self.assertTrue(result["result"])
        self.assertEqual(len(set(frames)), 2)
        self.assertEqual([a[2] for a in self.input.actions], [1, 2])
        self.input.faults = [StorageError("INPUT_FAILED", "partial", details={"partially_dispatched":True})]
        root = self.start(self.save('pcall(press_key,"a"); press_key("b"); return true'))
        state = self.await_state(root, "failed")
        self.assertEqual(state["error"]["code"], "INPUT_FAILED")
        self.assertEqual(len([a for a in self.input.actions if a[0]==root]), 1)

    def test_waits_same_frame_timeout_nil_and_disappear(self):
        calls = []
        def infer(command, params, cancel, catalog):
            name = params["target_id"]
            calls.append((params["image_id"], name))
            return dict(found=name != "missing", matches=[] if name == "missing" else [{"sample_no":7}])
        self.vision.run_sync.side_effect = infer
        src = ('local a,m=wait_for_all_targets({"a","b"}); local id,hit=wait_for_any_target({"missing","b"}); '
               'local f,v=wait_for_target("missing",.01,.005); return {a,m.a.sample_no,id,hit.sample_no,f,v==nil,'
               'wait_until_target_disappear("missing",.01)}')
        self.assertEqual(self.await_state(self.start(self.save(src)), "completed")["result"], [True,7,"b",7,False,True,True])
        self.assertEqual(calls[0][0], calls[1][0])
        self.assertEqual(calls[2][0], calls[3][0])
