"""Local, deterministic validation and analysis for uploaded evidence images.

The module deliberately has no Streamlit or database dependency.  It validates
the *decoded* image format (rather than trusting a filename), computes hashes
over the original bytes, and exposes the measurements needed by the rule
engine.  Persisting the returned bytes is optional and always uses the UUID
storage name generated here.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from hashlib import sha256
from io import BytesIO
import os
from pathlib import Path
from typing import Any, BinaryIO, Mapping
from uuid import uuid4
import warnings

import cv2
import imagehash
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError


FORMAT_MIME: Mapping[str, str] = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}
FORMAT_EXTENSION: Mapping[str, str] = {
    "JPEG": ".jpg",
    "PNG": ".png",
    "WEBP": ".webp",
}


class ImageValidationError(ValueError):
    """A safe, machine-readable upload validation failure."""

    def __init__(self, code: str, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


@dataclass(frozen=True, slots=True)
class ImagePolicy:
    """Limits applied before an image can enter the processing pipeline."""

    max_bytes: int = 10 * 1024 * 1024
    min_width: int = 200
    min_height: int = 200
    max_width: int = 12_000
    max_height: int = 12_000
    max_pixels: int = 40_000_000
    allowed_formats: frozenset[str] = field(
        default_factory=lambda: frozenset(FORMAT_MIME)
    )
    allow_animated: bool = False
    blur_threshold: float = 70.0
    # App screenshots can have large flat-color backgrounds (notably the
    # purple BeConfident UI); OCR confidence remains the stronger text signal.
    contrast_threshold: float = 16.0
    dark_threshold: float = 25.0
    bright_threshold: float = 245.0


@dataclass(frozen=True, slots=True)
class LegibilityMetrics:
    laplacian_variance: float
    grayscale_mean: float
    grayscale_stddev: float
    score: float
    is_blurry: bool
    is_low_contrast: bool
    is_too_dark: bool
    is_too_bright: bool

    @property
    def needs_review(self) -> bool:
        return any(
            (
                self.is_blurry,
                self.is_low_contrast,
                self.is_too_dark,
                self.is_too_bright,
            )
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "laplacian_variance": self.laplacian_variance,
            "grayscale_mean": self.grayscale_mean,
            "grayscale_stddev": self.grayscale_stddev,
            "score": self.score,
            "is_blurry": self.is_blurry,
            "is_low_contrast": self.is_low_contrast,
            "is_too_dark": self.is_too_dark,
            "is_too_bright": self.is_too_bright,
        }


@dataclass(frozen=True, slots=True)
class ColorSignals:
    """Advisory palette measurements; color alone is never proof of completion."""

    dominant_rgb: tuple[int, int, int]
    green_ratio: float
    purple_ratio: float
    yellow_ratio: float
    white_ratio: float
    dark_ratio: float

    def get(self, key: str, default: Any = None) -> Any:
        """Provide mapping-style access for rule engines and JSON adapters."""

        return getattr(self, key, default)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dominant_rgb": list(self.dominant_rgb),
            "green_ratio": self.green_ratio,
            "purple_ratio": self.purple_ratio,
            "yellow_ratio": self.yellow_ratio,
            "white_ratio": self.white_ratio,
            "dark_ratio": self.dark_ratio,
        }


@dataclass(frozen=True, slots=True)
class AnalyzedImage:
    """Validated image metadata plus the original, unmodified upload bytes."""

    storage_name: str
    mime_type: str
    image_format: str
    extension: str
    width: int
    height: int
    byte_size: int
    sha256: str
    phash: str
    legibility: LegibilityMetrics
    color_signals: ColorSignals
    original_bytes: bytes = field(repr=False, compare=False)

    @property
    def dimensions(self) -> tuple[int, int]:
        return self.width, self.height

    @property
    def valid(self) -> bool:
        return True

    @property
    def legible(self) -> bool:
        return not self.legibility.needs_review

    @property
    def size_bytes(self) -> int:
        return self.byte_size

    @property
    def content_type(self) -> str:
        return self.mime_type

    @property
    def format(self) -> str:
        return self.image_format

    @property
    def laplacian_variance(self) -> float:
        return self.legibility.laplacian_variance

    @property
    def data(self) -> bytes:
        return self.original_bytes

    def as_dict(self, *, include_bytes: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "storage_name": self.storage_name,
            "mime_type": self.mime_type,
            "image_format": self.image_format,
            "extension": self.extension,
            "width": self.width,
            "height": self.height,
            "byte_size": self.byte_size,
            "sha256": self.sha256,
            "phash": self.phash,
            "legibility": self.legibility.as_dict(),
            "color_signals": self.color_signals.as_dict(),
        }
        if include_bytes:
            value["original_bytes"] = self.original_bytes
        return value


def _coerce_bytes(source: bytes | bytearray | memoryview | BinaryIO) -> bytes:
    if isinstance(source, bytes):
        return source
    if isinstance(source, (bytearray, memoryview)):
        return bytes(source)
    read = getattr(source, "read", None)
    if not callable(read):
        raise TypeError("image source must be bytes-like or a binary file object")
    position: int | None = None
    try:
        position = source.tell()
    except (AttributeError, OSError):
        pass
    payload = read()
    if position is not None:
        try:
            source.seek(position)
        except (AttributeError, OSError):
            pass
    if not isinstance(payload, (bytes, bytearray, memoryview)):
        raise TypeError("binary file object returned non-bytes data")
    return bytes(payload)


def sha256_hex(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def generate_storage_name(image_format: str) -> str:
    normalized = image_format.upper()
    try:
        extension = FORMAT_EXTENSION[normalized]
    except KeyError as exc:
        raise ImageValidationError(
            "unsupported_format", f"unsupported image format: {image_format}"
        ) from exc
    return f"{uuid4()}{extension}"


def _rgb_image(image: Image.Image) -> Image.Image:
    """Flatten transparency onto white and return a detached RGB image."""

    oriented = ImageOps.exif_transpose(image)
    if oriented.mode in {"RGBA", "LA"} or "transparency" in oriented.info:
        rgba = oriented.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, rgba).convert("RGB")
    return oriented.convert("RGB")


def decode_image(payload: bytes) -> tuple[Image.Image, str, int]:
    """Decode and fully load an image, returning RGB pixels, real format, frames."""

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as probe:
                image_format = (probe.format or "").upper()
                frames = int(getattr(probe, "n_frames", 1))
                probe.verify()
            with Image.open(BytesIO(payload)) as reopened:
                reopened.load()
                rgb = _rgb_image(reopened)
                rgb.load()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageValidationError("invalid_image", "file is not a decodable image") from exc
    return rgb, image_format, frames


def measure_legibility(
    image: Image.Image | np.ndarray,
    *,
    policy: ImagePolicy | None = None,
) -> LegibilityMetrics:
    """Measure focus, contrast and exposure with OpenCV.

    The score is advisory.  Poor legibility is expected to cause review, never
    an automatic fraud rejection.
    """

    policy = policy or ImagePolicy()
    if isinstance(image, Image.Image):
        array = np.asarray(image.convert("RGB"))
        gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
    else:
        array = np.asarray(image)
        if array.ndim == 2:
            gray = array.astype(np.uint8, copy=False)
        elif array.ndim == 3 and array.shape[2] == 4:
            gray = cv2.cvtColor(array, cv2.COLOR_BGRA2GRAY)
        elif array.ndim == 3 and array.shape[2] == 3:
            gray = cv2.cvtColor(array, cv2.COLOR_BGR2GRAY)
        else:
            raise ValueError("expected a grayscale, BGR, BGRA, or PIL image")

    laplacian = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    mean, stddev = cv2.meanStdDev(gray)
    gray_mean = float(mean[0, 0])
    gray_std = float(stddev[0, 0])
    sharpness_score = min(1.0, laplacian / max(policy.blur_threshold, 1.0))
    contrast_score = min(1.0, gray_std / max(policy.contrast_threshold, 1.0))
    exposure_score = max(0.0, 1.0 - abs(gray_mean - 127.5) / 127.5)
    score = round(0.50 * sharpness_score + 0.30 * contrast_score + 0.20 * exposure_score, 4)
    return LegibilityMetrics(
        laplacian_variance=round(laplacian, 4),
        grayscale_mean=round(gray_mean, 4),
        grayscale_stddev=round(gray_std, 4),
        score=score,
        is_blurry=laplacian < policy.blur_threshold,
        is_low_contrast=gray_std < policy.contrast_threshold,
        is_too_dark=gray_mean < policy.dark_threshold,
        is_too_bright=gray_mean > policy.bright_threshold,
    )


def measure_color_signals(image: Image.Image | np.ndarray) -> ColorSignals:
    """Measure broad UI palette signals in HSV space.

    The ranges intentionally overlap real-world JPEG variation.  They are
    useful as supporting evidence for Duolingo/BeConfident classification but
    must not be used as a completion rule on their own.
    """

    if isinstance(image, Image.Image):
        rgb = np.asarray(image.convert("RGB"))
    else:
        array = np.asarray(image)
        if array.ndim != 3 or array.shape[2] not in (3, 4):
            raise ValueError("expected an RGB/BGR-like color image")
        if array.shape[2] == 4:
            array = cv2.cvtColor(array, cv2.COLOR_BGRA2BGR)
        rgb = cv2.cvtColor(array, cv2.COLOR_BGR2RGB)

    # Downsampling bounds CPU/memory use without materially changing ratios.
    height, width = rgb.shape[:2]
    scale = min(1.0, 512.0 / max(height, width))
    if scale < 1.0:
        rgb = cv2.resize(
            rgb,
            (max(1, round(width * scale)), max(1, round(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    hue, saturation, value = cv2.split(hsv)
    vivid = saturation >= 55

    def ratio(mask: np.ndarray) -> float:
        return round(float(np.count_nonzero(mask)) / float(mask.size), 4)

    # OpenCV hue is [0, 179].  These broad ranges cover app theme variants.
    green = vivid & (hue >= 35) & (hue <= 95) & (value >= 35)
    purple = vivid & (hue >= 120) & (hue <= 165) & (value >= 30)
    yellow = vivid & (hue >= 18) & (hue <= 38) & (value >= 80)
    white = (saturation <= 35) & (value >= 210)
    dark = value <= 45
    median = np.median(rgb.reshape(-1, 3), axis=0)
    return ColorSignals(
        dominant_rgb=tuple(int(round(channel)) for channel in median),
        green_ratio=ratio(green),
        purple_ratio=ratio(purple),
        yellow_ratio=ratio(yellow),
        white_ratio=ratio(white),
        dark_ratio=ratio(dark),
    )


def validate_image(
    source: bytes | bytearray | memoryview | BinaryIO,
    *,
    policy: ImagePolicy | None = None,
) -> AnalyzedImage:
    """Validate an upload and calculate immutable image metadata."""

    policy = policy or ImagePolicy()
    payload = _coerce_bytes(source)
    byte_size = len(payload)
    if byte_size == 0:
        raise ImageValidationError("empty_file", "image file is empty")
    if byte_size > policy.max_bytes:
        raise ImageValidationError(
            "file_too_large",
            f"image exceeds the {policy.max_bytes}-byte limit",
            details={"byte_size": byte_size, "max_bytes": policy.max_bytes},
        )

    # Read only headers before decoding pixels, so compressed bombs and
    # unsupported formats are rejected before memory-heavy decompression.
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload)) as probe:
                image_format = (probe.format or "").upper()
                frames = int(getattr(probe, "n_frames", 1))
                width, height = probe.size
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ImageValidationError("invalid_image", "file is not a decodable image") from exc
    if image_format not in policy.allowed_formats or image_format not in FORMAT_MIME:
        raise ImageValidationError(
            "unsupported_format",
            "real image format must be JPEG, PNG, or WEBP",
            details={"detected_format": image_format},
        )
    if frames > 1 and not policy.allow_animated:
        raise ImageValidationError(
            "animated_image_not_allowed",
            "animated images are not accepted",
            details={"frames": frames},
        )

    if width < policy.min_width or height < policy.min_height:
        raise ImageValidationError(
            "dimensions_too_small",
            "image dimensions are below the configured minimum",
            details={"width": width, "height": height},
        )
    if width > policy.max_width or height > policy.max_height:
        raise ImageValidationError(
            "dimensions_too_large",
            "image dimensions exceed the configured maximum",
            details={"width": width, "height": height},
        )
    pixels = width * height
    if pixels > policy.max_pixels:
        raise ImageValidationError(
            "too_many_pixels",
            "decoded image contains too many pixels",
            details={"pixels": pixels, "max_pixels": policy.max_pixels},
        )

    image, decoded_format, decoded_frames = decode_image(payload)
    if decoded_format != image_format or decoded_frames != frames:
        raise ImageValidationError("image_changed_during_decode", "image metadata was inconsistent")

    return AnalyzedImage(
        storage_name=generate_storage_name(image_format),
        mime_type=FORMAT_MIME[image_format],
        image_format=image_format,
        extension=FORMAT_EXTENSION[image_format],
        width=width,
        height=height,
        byte_size=byte_size,
        sha256=sha256_hex(payload),
        phash=str(imagehash.phash(image)),
        legibility=measure_legibility(image, policy=policy),
        color_signals=measure_color_signals(image),
        original_bytes=payload,
    )


def analyze_image_bytes(
    payload: bytes | bytearray | memoryview,
    *,
    policy: ImagePolicy | None = None,
    max_bytes: int | None = None,
    max_size_bytes: int | None = None,
    min_width: int | None = None,
    min_height: int | None = None,
    max_width: int | None = None,
    max_height: int | None = None,
    max_pixels: int | None = None,
) -> AnalyzedImage:
    """Public bytes API with optional one-off limit overrides."""

    effective = policy or ImagePolicy()
    if max_bytes is not None and max_size_bytes is not None and max_bytes != max_size_bytes:
        raise ValueError("max_bytes and max_size_bytes disagree")
    overrides = {
        "max_bytes": max_bytes if max_bytes is not None else max_size_bytes,
        "min_width": min_width,
        "min_height": min_height,
        "max_width": max_width,
        "max_height": max_height,
        "max_pixels": max_pixels,
    }
    selected = {key: value for key, value in overrides.items() if value is not None}
    if selected:
        effective = replace(effective, **selected)
    return validate_image(payload, policy=effective)


def prepare_ocr_variants(source: AnalyzedImage | bytes) -> dict[str, np.ndarray]:
    """Return RGB/BGR-compatible variants useful to a local OCR engine."""

    payload = source.original_bytes if isinstance(source, AnalyzedImage) else source
    image, _, _ = decode_image(payload)
    rgb = np.asarray(image)
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(gray)
    threshold = cv2.adaptiveThreshold(
        clahe,
        255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY,
        31,
        11,
    )
    return {"original": bgr, "contrast": clahe, "threshold": threshold}


def phash_distance(left: str, right: str) -> int:
    """Return the Hamming distance between two ImageHash pHash strings."""

    try:
        return int(imagehash.hex_to_hash(left) - imagehash.hex_to_hash(right))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid perceptual hash") from exc


def is_perceptually_similar(left: str, right: str, *, max_distance: int = 8) -> bool:
    return phash_distance(left, right) <= max_distance


def persist_image(image: AnalyzedImage, upload_directory: str | os.PathLike[str]) -> Path:
    """Persist validated original bytes under the generated UUID name.

    Bytes are fsynced to a same-directory temporary file, then linked atomically
    to the UUID destination without overwriting an existing file.
    """

    root = Path(upload_directory)
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    destination = root / image.storage_name
    temporary = root / f".{image.storage_name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(image.original_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        # link(2) publishes the completed file atomically and refuses overwrite.
        os.link(temporary, destination)
        try:
            directory_fd = os.open(root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some filesystems do not support directory fsync; file fsync above
            # still guarantees that readers never observe a partial payload.
            pass
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    temporary.unlink(missing_ok=True)
    return destination


# Explicit aliases retained for service-layer readability.
ProcessedImage = AnalyzedImage
process_image = validate_image
validate_image_upload = validate_image
store_image = persist_image


__all__ = [
    "FORMAT_EXTENSION",
    "FORMAT_MIME",
    "AnalyzedImage",
    "ColorSignals",
    "ImagePolicy",
    "ImageValidationError",
    "LegibilityMetrics",
    "ProcessedImage",
    "analyze_image_bytes",
    "decode_image",
    "generate_storage_name",
    "is_perceptually_similar",
    "measure_legibility",
    "measure_color_signals",
    "phash_distance",
    "prepare_ocr_variants",
    "persist_image",
    "process_image",
    "sha256_hex",
    "store_image",
    "validate_image",
    "validate_image_upload",
]
