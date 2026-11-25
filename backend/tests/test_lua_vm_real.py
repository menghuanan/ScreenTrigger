"""Exercise the production Lupa boundary, including host callbacks and count hooks."""
import importlib.util
import threading
import unittest

from backend.script.cancel import CancelFlag, CheckpointCancelled, ExecutionGate
from backend.script.lua_vm import LuaVm
from backend.storage.errors import StorageError


@unittest.skipUnless(importlib.util.find_spec("lupa"), "requires project Lua 5.4 runtime")
class RealLuaTest(unittest.TestCase):
    def test_libraries_arguments_results_and_error(self):
        vm = LuaVm(ExecutionGate())
        try:
            vm.build()
            vm.registry.register("failure", lambda: self.raise_error())
            result = vm.run_source("""
                local ok, err = pcall(failure)
                assert(not ok and type(err) == 'table')
                assert(io == nil and os == nil and debug == nil and python == nil and load == nil)
                return {empty=json_array({}), null=json_null, value=args.value, id=SCRIPT_ID,
                        code=err.code, library=string.upper("ok")}
            """, "boundary", {"value": "中😀"}, script_id="script-1")
            self.assertEqual(result, {"empty": [], "null": None, "value": "中😀", "id": "script-1",
                                      "code": "INVALID_PARAMS", "library": "OK"})
            with self.assertRaises(StorageError) as error:
                vm.run_source("local t={}; t.self=t; return t", "cycle")
            self.assertEqual(error.exception.code, "SERIALIZATION_ERROR")
        finally:
            vm.close()

    @staticmethod
    def raise_error():
        raise StorageError("INVALID_PARAMS", "test")

    def test_count_hook_stops_busy_loop(self):
        cancel = CancelFlag()
        entered = threading.Event()
        stopped = threading.Event()
        errors = []

        def run():
            vm = LuaVm(ExecutionGate(cancel=cancel))
            try:
                vm.build()
                vm.registry.register("started", lambda: entered.set())
                vm.run_source("started(); while true do local x = 2 end", "busy")
            except CheckpointCancelled:
                stopped.set()
            except Exception as exc:
                errors.append(exc)
            finally:
                vm.close()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        try:
            self.assertTrue(entered.wait(2), str(errors))
        finally:
            cancel.cancel()
        worker.join(2)
        self.assertTrue(stopped.is_set(), str(errors))
        self.assertFalse(worker.is_alive())
