"""TEST-012. These assertions exercise the on-disk JSON repository."""
import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.scripts.sources import ScriptSourceStore
from backend.storage import generation
from backend.storage.backup import BackupService
from backend.storage.catalog import Catalog
from backend.storage.errors import StorageError
from backend.targets import CatalogChange, Repository
from backend.tests.test_targets_repository import _asset, _group, _target, _TS


class CatalogBackupTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = Repository(str(self.root))
        self.addCleanup(self.repo.close)
        self.service = BackupService(self.repo)
        asset, image = _asset(b"immutable original")
        self.image = image
        self.target = _target("t1", 7, image, group_id="g1")
        self.group = _group("g1", "分组")
        self.source = b"return 42"
        path, sha = ScriptSourceStore(self.repo.data_root).persist("s1", self.source)
        script = dict(id="s1", revision=1, name="脚本", source_path=path, source_sha256=sha,
                      created_at=_TS, updated_at=_TS, parameters=[])
        self.repo.commit(CatalogChange(targets=(self.target,), groups=(self.group,),
                         assets=(asset,), script_metadata={"schema_version": 2, "scripts": [script]},
                         next_sample_no=12), None)
        self.current = self.root / "catalog/current.json"
        self.backup = self.service.create()["backup_id"]

    def restore(self, preview=None):
        p = preview or self.service.preview(self.backup)
        return self.service.restore(self.backup, p["expected_current"], p["expected_backup"])

    def test_portable_restore_keeps_assets_numbers_conditions_and_protects_new_data(self):
        old = self.repo.read_snapshot()
        self.repo.commit(CatalogChange(next_sample_no=24), old.generation)
        result = self.restore()
        self.assertTrue((self.root / "backups" / result["protection"]).is_dir())
        resumed = Repository(str(self.root)).read_snapshot()
        self.assertEqual(resumed.targets, (self.target,))
        self.assertEqual(resumed.groups, (self.group,))
        self.assertEqual(resumed.next_sample_no, 24)
        self.assertEqual(resumed.script_metadata, old.script_metadata)
        self.assertEqual((self.root / self.image["path"]).read_bytes(), b"immutable original")
        with TemporaryDirectory() as tmp:
            import shutil
            shutil.copytree(self.root / "backups" / self.backup, Path(tmp, "backups", self.backup))
            service = BackupService(Repository(tmp))
            p = service.preview(self.backup)
            service.restore(self.backup, p["expected_current"], p["expected_backup"])
            script = old.script_metadata["scripts"][0]
            self.assertEqual(Path(tmp, script["source_path"]).read_bytes(), self.source)
            self.assertEqual(Repository(tmp).read_snapshot().targets, old.targets)

    def test_concurrent_write_rejected_and_raw_pointer_unchanged(self):
        p = self.service.preview(self.backup)
        self.repo.commit(CatalogChange(), self.repo.read_snapshot().generation)
        current = self.current.read_bytes()
        with self.assertRaises(StorageError) as caught:
            self.restore(p)
        self.assertEqual(caught.exception.code, "CONFLICT")
        self.assertEqual(self.current.read_bytes(), current)

    def test_damaged_current_preserved_and_future_version_refused(self):
        raw = self.current.read_bytes()
        self.current.write_bytes(b"{broken")
        result = self.restore()
        self.assertEqual((self.root / "backups" / result["protection"]).read_bytes(), b"{broken")
        self.current.write_bytes(raw)
        gen = self.repo.reload().generation
        manifest = self.root / "catalog/generations" / gen / "manifest.json"
        doc = generation.decode_json(manifest.read_bytes())
        doc["schema_version"] = 3
        manifest.write_bytes(generation.encode_json(doc))
        self.current.write_bytes(generation.encode_json(dict(generation=gen, manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())))
        previous = self.current.read_bytes()
        with self.assertRaises(StorageError) as caught:
            self.service.preview(self.backup)
        self.assertEqual(caught.exception.code, "SCHEMA_UNSUPPORTED")
        self.assertEqual(self.current.read_bytes(), previous)

    def test_damaged_pointer_does_not_reuse_numbers_allocated_after_backup(self):
        self.repo.commit(CatalogChange(next_sample_no=48), self.repo.read_snapshot().generation)
        self.current.write_bytes(b"damaged pointer")
        self.restore()
        self.assertEqual(self.repo.reload().next_sample_no, 48)

    def test_missing_asset_and_disk_failure_do_not_replace_pointer(self):
        raw = self.current.read_bytes()
        p = self.service.preview(self.backup)
        with patch.object(Catalog, "activate", side_effect=OSError("disk full")):
            with self.assertRaises(StorageError):
                self.restore(p)
        self.assertEqual(self.current.read_bytes(), raw)
        (self.root / "backups" / self.backup / self.image["path"]).unlink()
        with self.assertRaises(StorageError):
            self.restore(p)
        self.assertEqual(self.current.read_bytes(), raw)

    def test_preview_rejects_bad_counts_and_paths_before_writing(self):
        for name in ("../catalog", "C:/temp", ""):
            with self.subTest(name=name), self.assertRaises(StorageError):
                self.service.preview(name)
        with patch("backend.storage.backup.MAX_TOTAL_BYTES", 12):
            with self.assertRaises(StorageError) as caught:
                self.service.preview(self.backup)
            self.assertEqual(caught.exception.code, "RESOURCE_LIMIT")
