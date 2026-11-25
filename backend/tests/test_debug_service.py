"""Debugger integration with pinned Lua, real source hashes and detached frame values."""
import unittest

from backend.debug.lines import executable_lines
from backend.storage.errors import StorageError
from backend.tests import test_execution_runtime as fixtures


class DebugTests(unittest.TestCase):
    setUp = fixtures.ExecutionRuntime.setUp
    cleanup_roots = fixtures.ExecutionRuntime.cleanup_roots
    save = fixtures.ExecutionRuntime.save
    await_state = fixtures.ExecutionRuntime.await_state

    def breakpoint(self, script, line):
        digest = self.scripts.load_script(script)["script"]["source_sha256"]
        return self.controller.command("add_breakpoint", dict(script_id=script, source_hash=digest, line=line))["breakpoint"]

    def run_debug(self, script):
        return self.controller.execute(dict(script_id=script, display_id="d", debug=True))["execution_id"]

    def command(self, name, run, **rest):
        return self.controller.command(name, dict(execution_id=run, **rest))

    def test_compile_only_and_stale_breakpoint(self):
        script = self.save('-- comment\nlocal function nested(a)\n return a+1\nend\nreturn nested(2)')
        self.assertIn(3, executable_lines(self.scripts.load_script(script)["source"]))
        with self.assertRaises(StorageError):
            self.breakpoint(script, 1)
        point = self.breakpoint(script, 3)
        self.assertEqual(self.breakpoint(script, 3)["id"], point["id"])
        self.scripts.save_script("updated", "return debug == nil", [], script_id=script, expected_revision=1)
        params = dict(script_id=script, line=3, source_hash=point["source_hash"])
        with self.assertRaisesRegex(StorageError, "源码"):
            self.controller.command("add_breakpoint", params)
        self.assertFalse(self.controller.command("list_breakpoints", {"script_id":script})["items"][0]["enabled"])
        self.assertTrue(self.await_state(self.run_debug(script), "completed")["result"])

    def test_preline_cycles_limits_and_stopped_breakpoint(self):
        src = ('local t = {}\nt.self=t\nlocal long=string.rep("文",5000)\n'
               'local wide={}\nfor i=1,180 do wide[i]=i end\npress_key("a")\nreturn 3')
        script = self.save(src)
        self.breakpoint(script, 6)
        run = self.run_debug(script)
        self.assertEqual(self.await_state(run, "debug_paused")["current_line"],6)
        self.assertFalse(self.input.actions)
        frame = self.command("get_stack", run)["frames"][0]
        page = self.command("get_variables", run, frame_id=frame["frame_id"])
        variables = {v["name"]:v for v in page["variables"]}
        self.assertTrue(page["truncated"])
        self.assertEqual(variables["t"]["value"][0]["reference_id"], variables["t"]["reference_id"])
        self.assertEqual(variables["long"]["value"], "文" * 4096)
        with self.assertRaises(StorageError):
            self.command("get_variables", run, frame_id="old")
        self.command("stop_script", run)
        self.await_state(run, "cancelled")
        self.assertFalse(self.input.actions)

    def test_step_into_out_over_child_and_parent_variables(self):
        child = self.save('local x=5\nx=x+1\nreturn x')
        parent = self.save('local n=2\nlocal answer=call_script("%s")\nreturn answer+n' % child)
        self.breakpoint(parent, 2)
        for step in ("step_into","step_over"):
            run = self.run_debug(parent)
            self.await_state(run,"debug_paused")
            self.command(step, run)
            self.await_state(run,"debug_paused")
            stack = self.command("get_stack",run)["frames"]
            if step == "step_into":
                self.assertEqual([f["script_id"] for f in stack], [child,parent])
                variables = self.command("get_variables",run,frame_id=stack[-1]["frame_id"])
                self.assertIn(("n",2),[(v["name"],v["value"]) for v in variables["variables"]])
                self.command("step_out",run)
                self.await_state(run,"debug_paused")
                stack=self.command("get_stack",run)["frames"]
            self.assertEqual((stack[0]["script_id"],stack[0]["line"]), (parent,3))
            self.command("continue_execution",run)
            self.assertEqual(self.await_state(run,"completed")["result"],8)
