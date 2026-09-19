"""Local text-region detection (Milestone 6, sections 3/4/5).

The pipeline only depends on the :class:`TextDetector` interface, never on a
specific OCR vendor:

* :class:`RapidOcrDetector` - PP-OCR (ONNX, CPU) boxes + text + confidence
* :class:`OpenCvTextDetector` - dependency-free fallback: morphological
  "text-like region" proposals (geometry only, no recognition)
* :class:`NullTextDetector` - nothing available: the analyzer reports
  ``unavailable`` and the pipeline falls back to the VLM verdict

Bounding boxes are the important part (section 5); recognition is a bonus.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Sequence

from core.subtitle_models import TextRegion

LOGGER = logging.getLogger(__name__)


class TextDetectorError(RuntimeError):
    """Any detector failure (missing model, unreadable image, engine crash)."""


class TextDetector(ABC):
    """Detect text regions in one image."""

    name: str = "unknown"
    #: whether recognized text is available (promotion keyword evidence)
    recognizes_text: bool = False

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """``(usable, explanation)`` - never raises."""

    @abstractmethod
    def detect(
        self, image_path: Path | str, *, recognize: bool | None = None
    ) -> list[TextRegion]:
        """Return normalized text regions for one image.

        ``recognize=False`` asks for boxes only (much cheaper); detectors that
        cannot recognize ignore the flag.
        """


class NullTextDetector(TextDetector):
    """Placeholder used when no engine is installed (section 39)."""

    name = "none"

    def __init__(self, reason: str = "no local text detector is available") -> None:
        self.reason = reason

    def available(self) -> tuple[bool, str]:
        return False, self.reason

    def detect(
        self, image_path: Path | str, *, recognize: bool | None = None
    ) -> list[TextRegion]:  # pragma: no cover
        return []


class RapidOcrDetector(TextDetector):
    """PP-OCR (RapidOCR, ONNX runtime) - CPU only, Chinese + English."""

    name = "rapidocr"
    recognizes_text = True

    def __init__(
        self,
        *,
        engine_factory: Callable[[], Any] | None = None,
        min_confidence: float = 0.30,
        max_side: int = 960,
    ) -> None:
        self._engine_factory = engine_factory
        self._engine: Any = None
        self._load_error = ""
        self.min_confidence = min_confidence
        self.max_side = max_side

    # -- engine lifecycle --------------------------------------------------
    def _load(self) -> Any:
        if self._engine is not None:
            return self._engine
        if self._load_error:
            raise TextDetectorError(self._load_error)
        try:
            if self._engine_factory is not None:
                self._engine = self._engine_factory()
            else:  # pragma: no cover - requires the optional dependency
                from rapidocr_onnxruntime import RapidOCR

                self._engine = RapidOCR()
        except Exception as exc:  # pragma: no cover - depends on the environment
            self._load_error = f"rapidocr is unavailable: {exc}"
            raise TextDetectorError(self._load_error) from exc
        return self._engine

    def available(self) -> tuple[bool, str]:
        try:
            self._load()
        except TextDetectorError as exc:
            return False, str(exc)
        return True, "rapidocr (PP-OCR onnx, CPU)"

    # -- detection ---------------------------------------------------------
    def detect(
        self, image_path: Path | str, *, recognize: bool | None = None
    ) -> list[TextRegion]:
        path = Path(image_path)
        if not path.exists():
            raise TextDetectorError(f"image not found: {path}")
        engine = self._load()
        use_reading = self.recognizes_text if recognize is None else bool(recognize)
        try:
            payload = engine(
                str(path), use_det=True, use_cls=False, use_rec=use_reading
            )
        except Exception as exc:  # pragma: no cover - engine specific
            raise TextDetectorError(f"rapidocr failed on {path.name}: {exc}") from exc
        boxes, _elapsed = _unpack_rapidocr(payload)
        width, height = _image_size(path)
        if width <= 0 or height <= 0:
            raise TextDetectorError(f"could not read image size: {path.name}")
        regions: list[TextRegion] = []
        for box, text, score in boxes:
            try:
                xs = [float(point[0]) for point in box]
                ys = [float(point[1]) for point in box]
            except (TypeError, ValueError, IndexError):  # pragma: no cover - defensive
                continue
            if not xs or not ys:
                continue
            confidence = float(score or 0.0)
            if confidence < self.min_confidence:
                continue
            regions.append(
                TextRegion(
                    x1=min(max(min(xs) / width, 0.0), 1.0),
                    y1=min(max(min(ys) / height, 0.0), 1.0),
                    x2=min(max(max(xs) / width, 0.0), 1.0),
                    y2=min(max(max(ys) / height, 0.0), 1.0),
                    confidence=round(confidence, 4),
                    text=str(text or "") if use_reading else "",
                    ocr_confidence=round(confidence, 4) if use_reading else None,
                    engine=self.name,
                )
            )
        return regions


class OpenCvTextDetector(TextDetector):
    """Morphological text-region proposals (no recognition, no extra deps).

    Classic pipeline: gradient magnitude -> threshold -> horizontal closing
    (characters merge into lines) -> contour filtering by size/aspect.  It
    answers "where is text-like detail and how big is it", which is exactly
    what the subtitle rules need; it cannot read the text.
    """

    name = "opencv"
    recognizes_text = False

    def __init__(
        self,
        *,
        cv2_module: Any = None,
        min_area_ratio: float = 0.0004,
        max_area_ratio: float = 0.6,
        min_confidence: float = 0.35,
    ) -> None:
        self._cv2 = cv2_module
        self.min_area_ratio = min_area_ratio
        self.max_area_ratio = max_area_ratio
        self.min_confidence = min_confidence

    def _module(self) -> Any:
        if self._cv2 is not None:
            return self._cv2
        try:  # pragma: no cover - depends on the environment
            import cv2

            self._cv2 = cv2
        except Exception as exc:  # pragma: no cover
            raise TextDetectorError(f"opencv is unavailable: {exc}") from exc
        return self._cv2

    def available(self) -> tuple[bool, str]:
        try:
            self._module()
        except TextDetectorError as exc:
            return False, str(exc)
        return True, "opencv morphological text-region proposals (no recognition)"

    def detect(
        self, image_path: Path | str, *, recognize: bool | None = None
    ) -> list[TextRegion]:
        cv2 = self._module()
        path = Path(image_path)
        if not path.exists():
            raise TextDetectorError(f"image not found: {path}")
        image = cv2.imread(str(path))
        if image is None:  # pragma: no cover - unreadable file
            raise TextDetectorError(f"could not read image: {path.name}")
        height, width = image.shape[:2]
        if width <= 0 or height <= 0:
            return []
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        # gradient magnitude emphasises stroke edges
        grad_x = cv2.Sobel(gray, cv2.CV_16S, 1, 0, ksize=3)
        grad_y = cv2.Sobel(gray, cv2.CV_16S, 0, 1, ksize=3)
        magnitude = cv2.convertScaleAbs(cv2.addWeighted(cv2.convertScaleAbs(grad_x), 0.5, cv2.convertScaleAbs(grad_y), 0.5, 0))
        _, binary = cv2.threshold(magnitude, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (17, 3))
        merged = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)
        contours, _ = cv2.findContours(merged, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        regions: list[TextRegion] = []
        frame_area = float(width * height)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            if h <= 2 or w <= 2:
                continue
            area_ratio = (w * h) / frame_area
            if area_ratio < self.min_area_ratio or area_ratio > self.max_area_ratio:
                continue
            aspect = w / float(h)
            if aspect < 1.0:  # vertical blobs are usually background detail
                continue
            fill = float(cv2.contourArea(contour)) / float(w * h) if w * h else 0.0
            confidence = round(min(0.95, 0.45 + 0.4 * fill) * min(1.0, aspect / 3.0), 4)
            if confidence < self.min_confidence:
                continue
            regions.append(
                TextRegion(
                    x1=x / width,
                    y1=y / height,
                    x2=(x + w) / width,
                    y2=(y + h) / height,
                    confidence=confidence,
                    text="",
                    engine=self.name,
                )
            )
        return regions


def _image_size(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image

        with Image.open(path) as image:
            return image.size
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.debug("could not read image size for %s: %s", path, exc)
        return 0, 0


def _unpack_rapidocr(payload: Any) -> tuple[list[tuple[Any, str, float]], Any]:
    """Normalize the RapidOCR return value across versions.

    ``use_rec=True``  -> ``[[box, text, score], ...]``
    ``use_rec=False`` -> ``[[[x, y], ...], ...]`` (bare boxes, no text/score)

    Getting this wrong is silent and expensive (it produced a false
    "no frame could be analysed" for every preview), so both shapes are parsed
    explicitly and a detection-only box gets a neutral confidence.
    """

    result: Any = payload
    elapsed: Any = None
    if isinstance(payload, tuple) and len(payload) == 2:
        result, elapsed = payload
    if result is None:
        return [], elapsed
    boxes = getattr(result, "boxes", None)
    if boxes is not None and not isinstance(result, list):  # pragma: no cover
        texts = getattr(result, "txts", []) or []
        scores = getattr(result, "scores", []) or []
        items = []
        for index, box in enumerate(boxes):
            text = texts[index] if index < len(texts) else ""
            score = scores[index] if index < len(scores) else 0.0
            items.append((box, text, score))
        return items, elapsed
    items = []
    for entry in result:
        if not isinstance(entry, (list, tuple)) or not entry:
            continue
        if _is_point_sequence(entry):
            # bare box: the engine only ran detection
            items.append((entry, "", 0.6))
            continue
        first = entry[0]
        looks_like_box = _is_point_sequence(first)
        if looks_like_box and len(entry) >= 3 and not _is_point_sequence(entry[1]):
            # [box, text, score]
            try:
                score = float(entry[2])
            except (TypeError, ValueError):  # pragma: no cover - defensive
                score = 0.0
            items.append((first, str(entry[1]), score))
            continue
        if looks_like_box and len(entry) == 2 and not _is_point_sequence(entry[1]):
            # [box, score] (recognition disabled on some builds)
            try:
                score = float(entry[1])
            except (TypeError, ValueError):  # pragma: no cover - defensive
                score = 0.6
            items.append((first, "", score))
            continue
    return items, elapsed


def _is_point_sequence(value: Any) -> bool:
    """True when ``value`` looks like ``[[x, y], [x, y], ...]``."""

    if not isinstance(value, (list, tuple)) or len(value) < 4:
        return False
    for point in value:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            return False
        if not all(isinstance(coordinate, (int, float)) for coordinate in point[:2]):
            return False
    return True


def build_detector(engine: str = "auto", **options: Any) -> TextDetector:
    """Pick a detector from config (``auto`` prefers OCR, then OpenCV)."""

    wanted = (engine or "auto").strip().lower()
    if wanted in ("none", "off", "disabled"):
        return NullTextDetector("subtitle_analysis.engine = none")
    if wanted == "rapidocr":
        return RapidOcrDetector(**options)
    if wanted == "opencv":
        return OpenCvTextDetector(**options)
    # auto: OCR first (it also recognizes text), then geometry-only proposals
    rapid = RapidOcrDetector(**options)
    usable, note = rapid.available()
    if usable:
        return rapid
    LOGGER.info("rapidocr unavailable (%s); falling back to opencv proposals", note)
    opencv_detector = OpenCvTextDetector(**options)
    usable, note2 = opencv_detector.available()
    if usable:
        return opencv_detector
    LOGGER.warning("no local text detector available: %s / %s", note, note2)
    return NullTextDetector(f"{note}; {note2}")


def detector_status(detector: TextDetector) -> dict[str, Any]:
    usable, note = detector.available()
    return {"engine": detector.name, "available": usable, "note": note}


def annotate_regions(image_path: Path, regions: Sequence[TextRegion], dest: Path) -> Path | None:
    """Debug helper: draw boxes on one frame (never touches production media)."""

    try:  # pragma: no cover - debug only
        from PIL import Image, ImageDraw

        with Image.open(image_path) as image:
            canvas = image.convert("RGB")
            draw = ImageDraw.Draw(canvas)
            width, height = canvas.size
            for region in regions:
                draw.rectangle(
                    [
                        region.x1 * width,
                        region.y1 * height,
                        region.x2 * width,
                        region.y2 * height,
                    ],
                    outline=(255, 0, 0),
                    width=2,
                )
            dest.parent.mkdir(parents=True, exist_ok=True)
            canvas.save(dest)
            return dest
    except Exception as exc:  # pragma: no cover - debug only
        LOGGER.debug("could not write the debug annotation: %s", exc)
        return None
