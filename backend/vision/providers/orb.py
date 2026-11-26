"""Locate a single textured sample in the current frame using CPU OpenCV ORB."""
from collections import OrderedDict
from hashlib import sha256

from ...types.capabilities import ProviderManifest
from ...types.conditions import _validate_feature_params
from ..types import RecognitionError
from ._algo import BaseAlgorithmProvider, require_cv2, require_numpy, run_leaf_validator
from .base import make_candidate


MANIFEST = ProviderManifest(provider_id="builtin.orb", api_version=1, provider_version="1.0.0",
                            supported_condition_types=("feature_match",), model_ids=(),
                            supported_languages=(), thread_safe=False, offline=True)


class OrbProvider(BaseAlgorithmProvider):
    def __init__(self, pixels):
        super().__init__(MANIFEST, pixels)
        self._samples = OrderedDict()
        self.last_measurement = None

    def initialize(self, model_dir):
        super().initialize(model_dir)
        if not hasattr(require_cv2(), "ORB_create"):
            raise RecognitionError("ENGINE_UNAVAILABLE", "OpenCV 未包含 ORB")

    @staticmethod
    def _features(raw):
        cv, np = require_cv2(), require_numpy()
        bgr = np.frombuffer(raw.bgr, np.uint8).reshape(raw.height, raw.width, 3)
        gray = cv.cvtColor(bgr, cv.COLOR_BGR2GRAY)
        orb = cv.ORB_create(nfeatures=1500, edgeThreshold=12, patchSize=25, fastThreshold=12)
        points, descriptors = orb.detectAndCompute(gray, None)
        return np.float32([point.pt for point in points]).reshape(-1, 2), descriptors

    def _sample(self, raw):
        # Parameters and descriptor format are fixed for the lifetime of this provider.
        key = (sha256(raw.bgr).digest(), raw.width, raw.height)
        if key not in self._samples:
            self._samples[key] = self._features(raw)
            if len(self._samples) > 8:
                self._samples.popitem(last=False)
        self._samples.move_to_end(key)
        return self._samples[key]

    def recognize(self, frame, params, cancel, roi=None):
        cancel.throw_if_cancelled()
        self.last_measurement = None
        options = {k: v for k, v in params.items() if k != "template_image_id"}
        run_leaf_validator(_validate_feature_params, options)
        sample = self._read(params.get("template_image_id"))
        scene = self._crop(self._read(frame.image_ref.image_id), roi)
        if scene is None or min(sample.width, sample.height, scene.width, scene.height) < 25:
            return self._provider_result()
        cv, np = require_cv2(), require_numpy()
        sample_pts, sample_desc = self._sample(sample)
        scene_pts, scene_desc = self._features(scene)
        cancel.throw_if_cancelled()
        minimum = options.get("min_inliers", 8)
        if sample_desc is None or scene_desc is None or min(len(sample_desc), len(scene_desc)) < minimum:
            return self._provider_result()
        # Scene-to-sample matching preserves repeated instances of the same sample.
        pairs = cv.BFMatcher(cv.NORM_HAMMING).knnMatch(scene_desc, sample_desc, k=2)
        matches = [a for a, b in pairs if a.distance < .76 * b.distance]
        source = np.float32([sample_pts[m.trainIdx] for m in matches]).reshape(-1, 2)
        observed = np.float32([scene_pts[m.queryIdx] for m in matches]).reshape(-1, 2)
        threshold = options.get("max_reprojection_error", 3.0)
        corners = np.float32([[0, 0], [sample.width, 0], [sample.width, sample.height], [0, sample.height]])
        candidates = []
        while len(source) >= minimum:
            cancel.throw_if_cancelled()
            matrix, mask = cv.estimateAffinePartial2D(source, observed, method=cv.RANSAC,
                                                       ransacReprojThreshold=threshold, maxIters=1000)
            if matrix is None or mask is None or not np.isfinite(matrix).all():
                break
            inliers = mask.ravel().astype(bool)
            if inliers.sum() < minimum:
                break
            projected = cv.transform(corners[None], matrix)[0]
            fitted = cv.transform(source[inliers][None], matrix)[0]
            error = float(np.mean(np.linalg.norm(fitted - observed[inliers], axis=1)))
            distinct = len(np.unique(source[inliers], axis=0))
            # An isolated cluster of matching lettering is insufficient to locate an entire icon.
            hull_area = cv.contourArea(cv.convexHull(source[inliers]))
            low, high = projected.min(axis=0), projected.max(axis=0)
            x, y = np.rint(low).astype(int)
            right, bottom = np.rint(high).astype(int)
            in_bounds = 0 <= x < right <= scene.width and 0 <= y < bottom <= scene.height
            # Evaluate agreement among correspondences near this instance, excluding other copies.
            nearby = ((observed >= low) & (observed <= high)).all(axis=1)
            ratio = float(inliers.sum()) / max(int(nearby.sum()), int(inliers.sum()))
            if (in_bounds and distinct >= minimum and ratio >= options.get("min_inlier_ratio", .45)
                    and hull_area >= .05 * sample.width * sample.height and error <= threshold):
                score = ratio * (1 - 1 / (1 + distinct)) / (1 + error / threshold)
                box = (int(x + scene.offset_x), int(y + scene.offset_y), int(right - x), int(bottom - y))
                candidates.append(((-score, -distinct, error, box[1], box[0], box[2], box[3]), box))
            source, observed = source[~inliers], observed[~inliers]
        cancel.throw_if_cancelled()
        if not candidates:
            return self._provider_result()
        rank, box = min(candidates)
        self.last_measurement = {"inliers": -rank[1], "mean_reprojection_error": rank[2]}
        return self._provider_result([make_candidate(*box, -rank[0], space=frame.space)])

    def close(self):
        self._samples.clear()
        super().close()
