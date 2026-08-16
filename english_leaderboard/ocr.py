"""Small, UI-independent adapter around the legacy RapidOCR runtime.

The application deliberately imports ``rapidocr_onnxruntime`` lazily so the
rest of the domain can be imported (and tested with a fake engine) without
loading ONNX models.  Model caching belongs to the caller, e.g. Streamlit's
``cache_resource`` around :func:`create_ocr_engine`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import math
from os import PathLike
from pathlib import Path
from typing import Any, Protocol, TypeAlias


Point: TypeAlias = tuple[float, float]
Box: TypeAlias = tuple[Point, ...]
ImageSource: TypeAlias = bytes | bytearray | memoryview | str | PathLike[str]


class OCREngine(Protocol):
    """Protocol implemented by RapidOCR and by lightweight test doubles."""

    def __call__(self, image: Any) -> Any: ...


class OCRUnavailableError(RuntimeError):
    """Raised when the optional local OCR dependency is not installed."""


class OCRExecutionError(RuntimeError):
    """Raised when an OCR engine cannot be invoked or parsed safely."""


@dataclass(frozen=True, slots=True)
class OCRLine:
    """One OCR text line and its optional confidence/bounding polygon."""

    text: str
    confidence: float | None = None
    box: Box | None = None


@dataclass(frozen=True, slots=True)
class OCRResult:
    """Normalized OCR output used by the rule engine."""

    text: str
    lines: tuple[OCRLine, ...]
    confidence: float | None = None
    elapsed_seconds: float | None = None

    @property
    def mean_confidence(self) -> float | None:
        """Compatibility/readability alias for the aggregate confidence."""

        return self.confidence

    @classmethod
    def empty(cls, *, elapsed_seconds: float | None = None) -> "OCRResult":
        return cls(text="", lines=(), confidence=None, elapsed_seconds=elapsed_seconds)


def create_ocr_engine(
    factory: Callable[..., OCREngine] | None = None,
    **engine_options: Any,
) -> OCREngine:
    """Create a local legacy RapidOCR engine.

    ``factory`` is intentionally injectable for tests and alternate packaged
    model configurations.  With no factory, only ``rapidocr_onnxruntime`` is
    imported; this function never downloads a model or calls an external API.
    """

    if factory is None:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on deployment extras
            raise OCRUnavailableError(
                "Local OCR is unavailable; install rapidocr-onnxruntime and its "
                "packaged ONNX models."
            ) from exc
        factory = RapidOCR

    try:
        return factory(**engine_options)
    except Exception as exc:
        raise OCRUnavailableError("Could not initialize the local OCR engine.") from exc


def extract_text(
    source: ImageSource | Any,
    *,
    engine: OCREngine | Any | None = None,
    engine_factory: Callable[..., OCREngine] | None = None,
    separator: str = "\n",
    **engine_options: Any,
) -> OCRResult:
    """Run OCR and normalize common legacy/new/fake return shapes.

    Supported sources include bytes, path strings and ``PathLike`` instances.
    Other decoded image objects are passed through unchanged to make the
    adapter convenient for OpenCV/Pillow callers.  Known result shapes include
    legacy ``(lines, elapsed)`` tuples, line dictionaries, ``[box, text,
    score]`` triples, and objects exposing ``txts``/``scores``/``boxes``.
    """

    if engine is not None and engine_factory is not None:
        raise ValueError("Pass either engine or engine_factory, not both.")
    if engine is not None and engine_options:
        raise ValueError("engine_options are only valid when creating an engine.")
    if not isinstance(separator, str):
        raise TypeError("separator must be a string.")

    normalized_source = _normalize_source(source)
    active_engine = engine or create_ocr_engine(engine_factory, **engine_options)

    try:
        raw = _invoke_engine(active_engine, normalized_source)
    except OCRExecutionError:
        raise
    except Exception as exc:
        raise OCRExecutionError("Local OCR processing failed.") from exc

    if isinstance(raw, OCRResult):
        return raw

    payload, elapsed = _unwrap_result(raw)
    lines = tuple(_collect_lines(payload))
    if not lines:
        return OCRResult.empty(elapsed_seconds=elapsed)

    text = separator.join(line.text for line in lines if line.text)
    scores = [line.confidence for line in lines if line.confidence is not None]
    confidence = sum(scores) / len(scores) if scores else None
    return OCRResult(
        text=text,
        lines=lines,
        confidence=confidence,
        elapsed_seconds=elapsed,
    )


def _normalize_source(source: Any) -> Any:
    if isinstance(source, memoryview):
        source = source.tobytes()
    elif isinstance(source, bytearray):
        source = bytes(source)
    elif isinstance(source, PathLike):
        source = str(Path(source))

    if isinstance(source, bytes) and not source:
        raise ValueError("OCR source bytes cannot be empty.")
    if isinstance(source, str) and not source.strip():
        raise ValueError("OCR source path cannot be empty.")
    if source is None:
        raise TypeError("OCR source cannot be None.")
    return source


def _invoke_engine(engine: Any, source: Any) -> Any:
    if callable(engine):
        return engine(source)
    for method_name in ("ocr", "run"):
        method = getattr(engine, method_name, None)
        if callable(method):
            return method(source)
    raise OCRExecutionError("OCR engine must be callable or expose ocr()/run().")


def _unwrap_result(raw: Any) -> tuple[Any, float | None]:
    """Separate payload from timing metadata without mistaking two lines for it."""

    elapsed = _read_elapsed(raw)
    if isinstance(raw, tuple) and len(raw) == 2 and not isinstance(raw[0], str) and _looks_like_timing(raw[1]):
        return raw[0], elapsed
    return raw, elapsed


def _read_elapsed(raw: Any) -> float | None:
    if isinstance(raw, tuple) and len(raw) == 2 and not isinstance(raw[0], str) and _looks_like_timing(raw[1]):
        return _coerce_elapsed(raw[1])
    for attribute in ("elapsed_seconds", "elapsed", "elapse"):
        value = getattr(raw, attribute, None)
        result = _coerce_elapsed(value)
        if result is not None:
            return result
    return None


def _looks_like_timing(value: Any) -> bool:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True
    if isinstance(value, Mapping):
        return any(key in value for key in ("total", "elapsed", "elapse", "time"))
    if _is_sequence(value) and 0 < len(value) <= 8:
        return all(_finite_float(item) is not None for item in value)
    return False


def _coerce_elapsed(value: Any) -> float | None:
    if isinstance(value, Mapping):
        for key in ("total", "elapsed", "elapse", "time"):
            if key in value:
                return _coerce_elapsed(value[key])
        return None
    if _is_sequence(value):
        parts = [_finite_float(item) for item in value]
        if parts and all(part is not None and part >= 0 for part in parts):
            return sum(part for part in parts if part is not None)
        return None
    number = _finite_float(value)
    return number if number is None or number >= 0 else None


def _collect_lines(payload: Any) -> list[OCRLine]:
    if payload is None:
        return []
    if isinstance(payload, OCRLine):
        return [payload] if payload.text else []
    if isinstance(payload, OCRResult):
        return list(payload.lines)

    aggregate = _aggregate_lines(payload)
    if aggregate is not None:
        return aggregate

    line = _line_from_item(payload)
    if line is not None:
        return [line] if line.text else []

    if _is_sequence(payload):
        lines: list[OCRLine] = []
        for item in payload:
            parsed = _line_from_item(item)
            if parsed is not None:
                if parsed.text:
                    lines.append(parsed)
            elif _is_sequence(item) or isinstance(item, Mapping):
                lines.extend(_collect_lines(item))
        return lines

    raise OCRExecutionError(f"Unsupported OCR result type: {type(payload).__name__}.")


def _aggregate_lines(payload: Any) -> list[OCRLine] | None:
    def pick(names: tuple[str, ...]) -> Any:
        if isinstance(payload, Mapping):
            for name in names:
                if name in payload:
                    return payload[name]
        else:
            for name in names:
                value = getattr(payload, name, None)
                if value is not None:
                    return value
        return None

    texts = pick(("txts", "texts", "rec_texts"))
    if texts is None or isinstance(texts, str) or not _is_sequence(texts):
        return None
    scores = pick(("scores", "confidences", "rec_scores"))
    boxes = pick(("boxes", "polygons", "dt_polys"))
    result: list[OCRLine] = []
    for index, text in enumerate(texts):
        normalized_text = _coerce_text(text)
        if not normalized_text:
            continue
        score = _sequence_item(scores, index)
        box = _sequence_item(boxes, index)
        result.append(
            OCRLine(
                text=normalized_text,
                confidence=_coerce_confidence(score),
                box=_coerce_box(box),
            )
        )
    return result


def _line_from_item(item: Any) -> OCRLine | None:
    if isinstance(item, OCRLine):
        return item
    if isinstance(item, str):
        text = item.strip()
        return OCRLine(text=text) if text else None

    if isinstance(item, Mapping):
        text = _first_value(item, ("text", "txt", "label"))
        if text is None:
            return None
        return OCRLine(
            text=_coerce_text(text),
            confidence=_coerce_confidence(
                _first_value(item, ("score", "confidence", "conf"))
            ),
            box=_coerce_box(_first_value(item, ("box", "bbox", "points", "polygon"))),
        )

    if _is_sequence(item):
        values = list(item)
        # Legacy RapidOCR: [box, text, score].
        if len(values) >= 2 and isinstance(values[1], str):
            return OCRLine(
                text=values[1].strip(),
                confidence=_coerce_confidence(values[2] if len(values) > 2 else None),
                box=_coerce_box(values[0]),
            )
        # Lightweight fakes and some adapters: [text, score, box?].
        if values and isinstance(values[0], str):
            return OCRLine(
                text=values[0].strip(),
                confidence=_coerce_confidence(values[1] if len(values) > 1 else None),
                box=_coerce_box(values[2] if len(values) > 2 else None),
            )
        return None

    text = getattr(item, "text", None) or getattr(item, "txt", None)
    if text is None:
        return None
    return OCRLine(
        text=_coerce_text(text),
        confidence=_coerce_confidence(
            getattr(item, "score", None) or getattr(item, "confidence", None)
        ),
        box=_coerce_box(getattr(item, "box", None) or getattr(item, "bbox", None)),
    )


def _first_value(mapping: Mapping[Any, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _sequence_item(value: Any, index: int) -> Any:
    if not _is_sequence(value):
        return None
    return value[index] if index < len(value) else None


def _coerce_text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _coerce_confidence(value: Any) -> float | None:
    score = _finite_float(value)
    if score is None:
        return None
    # RapidOCR scores are probabilities.  Keep normalization conservative for
    # adapters that expose a percentage while rejecting impossible negatives.
    if score < 0:
        return None
    if 1 < score <= 100:
        return score / 100
    return score if score <= 1 else None


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _coerce_box(value: Any) -> Box | None:
    if value is None:
        return None
    if hasattr(value, "tolist"):
        value = value.tolist()
    if not _is_sequence(value):
        return None

    points: list[Point] = []
    for candidate in value:
        if hasattr(candidate, "tolist"):
            candidate = candidate.tolist()
        if not _is_sequence(candidate) or len(candidate) < 2:
            return None
        x = _finite_float(candidate[0])
        y = _finite_float(candidate[1])
        if x is None or y is None:
            return None
        points.append((x, y))
    return tuple(points) if points else None


def _is_sequence(value: Any) -> bool:
    if isinstance(value, (str, bytes, bytearray, memoryview, Mapping)):
        return False
    return isinstance(value, Sequence) or (
        hasattr(value, "__len__") and hasattr(value, "__getitem__")
    )


__all__ = [
    "Box",
    "ImageSource",
    "OCREngine",
    "OCRExecutionError",
    "OCRLine",
    "OCRResult",
    "OCRUnavailableError",
    "create_ocr_engine",
    "extract_text",
]
