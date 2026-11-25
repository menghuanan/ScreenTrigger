"""Interrupt a real process between durable writes, then reopen and write again."""
import multiprocessing
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from backend.storage.backup import BackupService
from backend.storage.catalog import Catalog
from backend.targets.repository import Repository, CatalogChange


def interrupt_restore(directory, backup, method, when):
    repo = Repository(directory)
    service = BackupService(repo)
    original = getattr(Catalog, method)
    def fault(catalog, *args):
        if when == "before":
            os._exit(77)
        original(catalog, *args)
        os._exit(77)
    preview = service.preview(backup)
    with patch.object(Catalog, method, fault):
        service.restore(backup, preview["expected_current"], preview["expected_backup"])


class RestoreInterruptTest(unittest.TestCase):
    def test_restart_after_generation_manifest_or_pointer_interrupt(self):
        for method, when in (("write_generation", "after"), ("write_manifest", "after"),
                             ("activate", "before"), ("activate", "after")):
            with self.subTest(method=method, when=when), TemporaryDirectory() as tmp:
                repo = Repository(tmp)
                initial = repo.commit(CatalogChange(next_sample_no=7), None)
                service = BackupService(repo)
                backup = service.create()["backup_id"]
                old = repo.commit(CatalogChange(next_sample_no=12), initial.generation)
                worker = multiprocessing.get_context("spawn").Process(
                    target=interrupt_restore, args=(tmp, backup, method, when))
                worker.start()
                worker.join(10)
                if worker.is_alive():
                    worker.terminate()
                    worker.join()
                    self.fail("storage worker hung")
                self.assertEqual(worker.exitcode, 77)
                snapshot = Repository(tmp).read_snapshot()
                self.assertEqual(snapshot.next_sample_no, 12)
                self.assertEqual(snapshot.generation == old.generation,
                                 (method, when) != ("activate", "after"))
                self.assertFalse(Path(tmp, "catalog/writer.lock").exists())
                repo.commit(CatalogChange(next_sample_no=13), snapshot.generation)
