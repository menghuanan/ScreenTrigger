import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from backend.vision.cancel import NullCancelToken
from backend.vision.model_library import ModelLibrary
from backend.vision.providers.ocr_models import OcrModelCatalog
from backend.vision.types import RecognitionError


class ModelLibraryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        self.media = root / "media"
        models = []
        engines = []
        for model in ("shared-detector", "rec-a", "rec-b"):
            component = self.media / model
            component.mkdir(parents=True)
            payload = ("version=1 model=" + model).encode()
            (component / "inference.json").write_bytes(payload)
            models.append(dict(model_id=model, files=[dict(name="inference.json", bytes=len(payload),
                               sha256=hashlib.sha256(payload).hexdigest())]))
            if model.startswith("rec-"):
                engines.append(dict(detection_model="shared-detector", recognition_model=model))
        catalog = root / "trusted.json"
        catalog.write_text(json.dumps(dict(models=models, engines=engines)), encoding="utf8")
        self.library = ModelLibrary(root / "models", root / "factory", OcrModelCatalog.load(str(catalog)))

    def test_shared_object_survives_until_both_pipelines_are_removed(self):
        cancel = NullCancelToken()
        lib = self.library
        first = lib.prepare("rec-a", self.media, cancel)
        lib.commit("rec-a", first, 0)
        second = lib.prepare("rec-b", self.media, cancel)
        lib.commit("rec-b", second, 1)
        self.assertEqual(first["shared-detector"], second["shared-detector"])
        self.assertEqual(len(list(lib.objects.iterdir())), 3)
        lib.commit("rec-a", None, 2)
        lib.collect(lib.object_refs(lib.selection))
        lib.catalog.verify(lib.roots("rec-b", second)["shared-detector"], ("shared-detector",))
        self.assertFalse((lib.objects / first["rec-a"]).exists())
        lib.commit("rec-b", None, 3)
        lib.collect(lib.object_refs(lib.selection))
        self.assertEqual(list(lib.objects.iterdir()), [])

    def test_untrusted_same_size_version_cannot_change_committed_selection(self):
        cancel = NullCancelToken()
        lib = self.library
        first = lib.prepare("rec-a", self.media, cancel)
        lib.commit("rec-a", first, 0)
        original = lib.pointer.read_bytes()
        metadata = self.media / "rec-a" / "inference.json"
        metadata.write_bytes(metadata.read_bytes().replace(b"version=1", b"version=2"))
        with self.assertRaises(RecognitionError) as caught:
            lib.prepare("rec-a", self.media, cancel)
        self.assertEqual(caught.exception.code, "DATA_CORRUPT")
        self.assertEqual(lib.pointer.read_bytes(), original)
        lib.catalog.verify(lib.roots("rec-a", first)["rec-a"], ("rec-a",))
