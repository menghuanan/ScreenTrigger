import json
from pathlib import Path
import unittest

from backend.storage.errors import StorageError
from backend.types.anchor import AnchoredPoint


class AnchoredPointTests(unittest.TestCase):
    def test_shared_projection_vectors(self):
        path = Path(__file__).resolve().parents[2] / "schema/anchor-cases.json"
        for case in json.loads(path.read_text(encoding="utf8")):
            with self.subTest(case=case["id"]):
                try:
                    actual = list(AnchoredPoint.parse(case["anchor"]).resolve(case["rect"]))
                except StorageError as exc:
                    actual = exc.code
                self.assertEqual(actual, case["expected"])
