# -*- coding: utf-8 -*-
"""TASK-012 服务层单测（backend/scripts/service.py ``ScriptService``）。

覆盖设计 §5/§11/§15：save（create/update/no-op/冲突/只给其一）、load 往返与不可用资产、
delete（IN_USE / 源码保留）、list 排序分页、validate_script 委托与 ENGINE_UNAVAILABLE 传播、
``catalog_changed`` 事件（仅 commit 成功后发、no-op/失败不发）、**保存不执行**（可证伪）、
以及 §15.2 **装配级集成测试**（ScriptService 与 TargetService 共用同一 Repository 与同一
CatalogEmitter，防脑裂）。
"""

from __future__ import annotations

import io
import itertools
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from backend.image.store import SessionImageStore  # noqa: E402
from backend.main import (  # noqa: E402
    SidecarApp,
    SidecarArgs,
    StdioTransport,
    build_catalog_services,
)
from backend.script.validator import (  # noqa: E402
    Diagnostic,
    ScriptValidator,
    engine_unavailable,
)
from backend.scripts import ScriptService, ScriptSourceStore, domain  # noqa: E402
from backend.scripts.sources import ScriptSourceStore as _ScriptSourceStore  # noqa: E402
from backend.storage.errors import StorageError  # noqa: E402
from backend.targets import RecordingCatalogEmitter, Repository, TargetService  # noqa: E402


def _code(ctx) -> str:
    return getattr(ctx.exception, "code", None)


def _field(ctx):
    return getattr(ctx.exception, "details", {}).get("field")


def _param(name="retry", type_="number", required=False, default=3, description=""):
    return {"name": name, "type": type_, "required": required, "default": default,
            "description": description}


class _FakeValidator(ScriptValidator):
    """返回预设诊断的替身（不依赖 lupa）。"""

    def __init__(self, diagnostics):
        self._diagnostics = list(diagnostics)

    def compile(self, source, name="script"):
        return list(self._diagnostics)


class _SpyValidator(ScriptValidator):
    """记录调用次数（证伪「保存会编译」）。"""

    def __init__(self):
        self.calls = []

    def compile(self, source, name="script"):
        self.calls.append(source)
        return []


class _EngineUnavailableValidator(ScriptValidator):
    """模拟 lupa 缺失：抛 ENGINE_UNAVAILABLE。"""

    def compile(self, source, name="script"):
        raise engine_unavailable()


class _CountingSourceStore(_ScriptSourceStore):
    """记录 persist 调用次数的源码存储（证伪 no-op 落盘）。"""

    def __init__(self, data_root, **kw):
        super().__init__(data_root, **kw)
        self.persist_calls = 0

    def persist(self, script_id, source_bytes):
        self.persist_calls += 1
        return super().persist(script_id, source_bytes)


class _ServiceFixture(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="st-script-svc-")
        self.repo = Repository(self.dir)
        self.store = ScriptSourceStore(self.repo.data_root)
        self.emitter = RecordingCatalogEmitter()
        self._clock_counter = itertools.count(0)
        self._id_counter = itertools.count(1)
        self.svc = ScriptService(
            self.repo,
            self.store,
            validator=_FakeValidator([]),
            emitter=self.emitter,
            clock=self._clock,
            id_gen=self._id_gen,
        )

    def _clock(self) -> str:
        return "2026-09-21T09:%02d:00Z" % next(self._clock_counter)

    def _id_gen(self) -> str:
        return "s-%d" % next(self._id_counter)

    def _save(self, name="登录点击", source="return 1", parameters=None, **kw):
        if parameters is None:
            parameters = [_param()]
        return self.svc.save_script(name, source, parameters, **kw)


class SaveScriptTest(_ServiceFixture):
    def test_local_hotkey_lifecycle(self):
        first = self._save()
        second = self._save()
        bound = self.svc.set_hotkey(first["id"], 1, "F12")
        self.assertEqual(self.svc.set_hotkey(first["id"], 2, "F12"), bound)
        for key, revision, code in (("F13", 1, "INVALID_PARAMS"), ("F12", 1, "HOTKEY_CONFLICT"),
                                    ("A", 2, "CONFLICT")):
            with self.assertRaises(StorageError) as error:
                self.svc.set_hotkey(second["id"], revision, key)
            self.assertEqual(error.exception.code, code)
        edited = self._save(name="修改", script_id=first["id"], expected_revision=2)
        self.assertEqual(edited["hotkey"], "F12")
        self.assertNotIn("hotkey", self.svc.set_hotkey(first["id"], 3, None))
        self.assertEqual(self.svc.set_hotkey(second["id"], 1, "F12")["revision"], 2)

    def test_create_revision_event_and_source_persisted(self):
        script = self._save()
        self.assertEqual(script["revision"], 1)
        self.assertEqual(script["id"], "s-1")
        self.assertEqual(
            script["source_path"], "scripts/s-1/%s.lua" % script["source_sha256"]
        )
        self.assertTrue(os.path.isfile(os.path.join(self.dir, script["source_path"])))
        self.assertEqual(len(self.emitter.events), 1)
        self.assertEqual(self.emitter.events[0]["revision"], 1)
        self.assertTrue(self.emitter.events[0]["generation"].startswith("g-"))

    def test_update_bumps_revision_keeps_created_at_and_keeps_old_source(self):
        first = self._save()
        self.emitter.events.clear()
        second = self._save(
            source="return 2", script_id=first["id"], expected_revision=first["revision"]
        )
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["id"], first["id"])
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertNotEqual(second["updated_at"], first["updated_at"])
        self.assertNotEqual(second["source_sha256"], first["source_sha256"])
        # 旧 hash 文件保留（只保留不回收）
        self.assertTrue(os.path.isfile(os.path.join(self.dir, first["source_path"])))
        self.assertTrue(os.path.isfile(os.path.join(self.dir, second["source_path"])))
        self.assertEqual(len(self.emitter.events), 1)
        self.assertEqual(self.emitter.events[0]["revision"], 2)

    def test_noop_save_no_commit_no_event_no_persist(self):
        store = _CountingSourceStore(self.repo.data_root)
        svc = ScriptService(
            self.repo, store, validator=_FakeValidator([]), emitter=self.emitter,
            clock=self._clock, id_gen=self._id_gen,
        )
        first = svc.save_script("登录点击", "return 1", [_param()])
        self.emitter.events.clear()
        store.persist_calls = 0
        again = svc.save_script(
            "登录点击", "return 1", [_param()],
            script_id=first["id"], expected_revision=first["revision"],
        )
        self.assertEqual(again, first)
        self.assertEqual(store.persist_calls, 0)
        self.assertEqual(self.emitter.events, [])
        self.assertEqual(svc.load_script(first["id"])["script"]["revision"], 1)

    def test_save_does_not_compile_or_execute(self):
        spy = _SpyValidator()
        svc = ScriptService(self.repo, self.store, validator=spy, emitter=self.emitter,
                            clock=self._clock, id_gen=self._id_gen)
        # 语法非法（不能编译）但元数据合法的源码：保存成功 → 证明保存不编译
        script = svc.save_script("坏脚本", "local x =", [_param()])
        self.assertEqual(script["revision"], 1)
        self.assertEqual(spy.calls, [])

    def test_conflict_wrong_revision_keeps_snapshot_no_event(self):
        first = self._save()
        self.emitter.events.clear()
        with self.assertRaises(StorageError) as ctx:
            self._save(source="return 2", script_id=first["id"], expected_revision=99)
        self.assertEqual(_code(ctx), "CONFLICT")
        self.assertEqual(_field(ctx), "expected_revision")
        self.assertEqual(self.emitter.events, [])
        still = self.svc.load_script(first["id"])["script"]
        self.assertEqual(still["revision"], 1)

    def test_only_one_of_id_revision_reports_missing_partner(self):
        with self.assertRaises(StorageError) as ctx1:
            self.svc.save_script("n", "return 1", [_param()], script_id="s-1")
        self.assertEqual(_code(ctx1), "INVALID_PARAMS")
        self.assertEqual(_field(ctx1), "expected_revision")
        with self.assertRaises(StorageError) as ctx2:
            self.svc.save_script("n", "return 1", [_param()], expected_revision=1)
        self.assertEqual(_code(ctx2), "INVALID_PARAMS")
        self.assertEqual(_field(ctx2), "script_id")

    def test_update_missing_script_not_found(self):
        with self.assertRaises(StorageError) as ctx:
            self.svc.save_script("n", "return 1", [_param()],
                                 script_id="nope", expected_revision=1)
        self.assertEqual(_code(ctx), "SCRIPT_NOT_FOUND")
        self.assertEqual(_field(ctx), "script_id")

    def test_source_over_budget_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            self._save(source="a" * (domain.SOURCE_MAX_BYTES + 1))
        self.assertEqual(_code(ctx), "RESOURCE_LIMIT")
        self.assertEqual(_field(ctx), "source")

    def test_invalid_name_rejected(self):
        with self.assertRaises(StorageError) as ctx:
            self._save(name="   ")
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(_field(ctx), "name")

    def test_expected_revision_type_defense(self):
        # F-2 防御：直接调用服务时非 int 的 expected_revision 不崩（INVALID_PARAMS）
        first = self._save()
        for bad in ("abc", None, 1.5, True, [1]):
            with self.subTest(value=bad):
                with self.assertRaises(StorageError) as ctx:
                    self.svc.save_script(
                        "n", "return 2", [_param()],
                        script_id=first["id"], expected_revision=bad,
                    )
                self.assertEqual(_code(ctx), "INVALID_PARAMS")
                self.assertEqual(_field(ctx), "expected_revision")


class LoadDeleteListTest(_ServiceFixture):
    def test_load_roundtrip(self):
        first = self._save(source="-- 注释\nreturn 42")
        got = self.svc.load_script(first["id"])
        self.assertEqual(got["script"]["id"], first["id"])
        self.assertEqual(got["source"], "-- 注释\nreturn 42")

    def test_load_missing_script_not_found(self):
        with self.assertRaises(StorageError) as ctx:
            self.svc.load_script("nope")
        self.assertEqual(_code(ctx), "SCRIPT_NOT_FOUND")
        self.assertEqual(_field(ctx), "script_id")

    def test_load_missing_asset_data_corrupt(self):
        first = self._save()
        os.remove(os.path.join(self.dir, first["source_path"]))
        with self.assertRaises(StorageError) as ctx:
            self.svc.load_script(first["id"])
        self.assertEqual(_code(ctx), "DATA_CORRUPT")

    def test_delete_removes_metadata_keeps_source(self):
        first = self._save()
        self.emitter.events.clear()
        self.svc.delete_script(first["id"], first["revision"])
        self.assertTrue(os.path.isfile(os.path.join(self.dir, first["source_path"])))
        with self.assertRaises(StorageError) as ctx:
            self.svc.load_script(first["id"])
        self.assertEqual(_code(ctx), "SCRIPT_NOT_FOUND")
        self.assertEqual(len(self.emitter.events), 1)
        self.assertEqual(self.emitter.events[0]["revision"], first["revision"])

    def test_delete_in_use(self):
        repo = Repository(self.dir, reference_port=lambda eid: eid == "s-1")
        svc = ScriptService(
            repo, ScriptSourceStore(repo.data_root), validator=_FakeValidator([]),
            emitter=self.emitter, clock=self._clock, id_gen=self._id_gen,
        )
        first = svc.save_script("n", "return 1", [_param()])
        with self.assertRaises(StorageError) as ctx:
            svc.delete_script(first["id"], first["revision"])
        self.assertEqual(_code(ctx), "IN_USE")
        self.assertEqual(_field(ctx), "script_id")

    def test_delete_conflict_wrong_revision(self):
        first = self._save()
        with self.assertRaises(StorageError) as ctx:
            self.svc.delete_script(first["id"], 99)
        self.assertEqual(_code(ctx), "CONFLICT")

    def test_list_sort_and_page(self):
        ids = [self._save(name="脚本%d" % i)["id"] for i in range(3)]
        result = self.svc.list_scripts()
        self.assertEqual(result["total"], 3)
        self.assertEqual([s["id"] for s in result["items"]], ids)
        page = self.svc.list_scripts(offset=1, limit=1)
        self.assertEqual([s["id"] for s in page["items"]], [ids[1]])
        self.assertEqual(page["total"], 3)


class ValidateScriptServiceTest(_ServiceFixture):
    def test_delegates_to_validator_and_invariant(self):
        diag = Diagnostic(line=2, column=None, message="unexpected symbol near <eof>")
        svc = ScriptService(self.repo, self.store, validator=_FakeValidator([diag]))
        result = svc.validate_script("local x =")
        self.assertFalse(result["valid"])
        self.assertEqual(
            result["diagnostics"],
            [{"line": 2, "column": None, "message": "unexpected symbol near <eof>"}],
        )
        svc2 = ScriptService(self.repo, self.store, validator=_FakeValidator([]))
        self.assertEqual(svc2.validate_script("return 1"), {"valid": True, "diagnostics": []})

    def test_invalid_source_rejected_before_engine(self):
        spy = _SpyValidator()
        svc = ScriptService(self.repo, self.store, validator=spy)
        with self.assertRaises(StorageError) as ctx:
            svc.validate_script(123)
        self.assertEqual(_code(ctx), "INVALID_PARAMS")
        self.assertEqual(_field(ctx), "source")
        self.assertEqual(spy.calls, [])

    def test_engine_unavailable_propagates_with_shape(self):
        svc = ScriptService(self.repo, self.store, validator=_EngineUnavailableValidator())
        with self.assertRaises(StorageError) as ctx:
            svc.validate_script("return 1")
        self.assertEqual(_code(ctx), "ENGINE_UNAVAILABLE")
        self.assertFalse(ctx.exception.retryable)
        self.assertEqual(ctx.exception.details, {})


class AssemblySharingTest(unittest.TestCase):
    """§15.2 装配级集成测试：真实 ``build_catalog_services`` + ``SidecarApp`` 装配。

    单测**替身**会把「两边不同实例」的偏差抹平；只有真实装配路径才暴露。故此处断言
    ``ScriptService`` 与 ``TargetService`` 共用**同一** ``Repository`` 实例，且经
    ``SidecarApp`` 装配后共用**同一** ``CatalogEmitter``（防 generation/快照脑裂）。
    """

    def test_services_share_repository_and_emitter(self):
        data_dir = tempfile.mkdtemp(prefix="st-script-asm-")
        image_store = SessionImageStore(session_id="s-asm")
        args = SidecarArgs(session_id="s-asm", data_dir=data_dir)
        repository, target_service, script_service = build_catalog_services(args, image_store)
        # 同一 Repository 实例（防脑裂）
        self.assertIs(script_service.repository, target_service.repository)
        self.assertIs(script_service.repository, repository)
        # 经真实 SidecarApp 装配：共用同一 emitter（同一 transport/health/session）
        out, err, stdin = io.StringIO(), io.StringIO(), io.StringIO("")
        transport = StdioTransport(stdout=out, stdin=stdin, stderr=err)
        app = SidecarApp(
            args=args,
            transport=transport,
            stderr=err,
            lua_probe=lambda: "Lua 5.4",
            target_service=target_service,
            script_service=script_service,
        )
        self.assertIs(script_service.emitter, target_service.emitter)
        # 5 条脚本命令已注册（可被 Dispatch）
        for command in ("list_scripts", "load_script", "save_script",
                        "delete_script", "validate_script"):
            self.assertIn(command, app.dispatcher._handlers)


if __name__ == "__main__":
    unittest.main()
