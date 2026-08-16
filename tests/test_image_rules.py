from __future__ import annotations

from english_leaderboard.image_processing import (
    ImagePolicy,
    ImageValidationError,
    analyze_image_bytes,
    phash_distance,
)
from english_leaderboard.models import SubmissionStatus
from english_leaderboard.rules import detect_completion, detect_platform

from conftest import make_png


def test_real_format_validation_and_hashes():
    payload = make_png(4)
    result = analyze_image_bytes(
        payload,
        policy=ImagePolicy(max_bytes=2_000_000, min_width=320, min_height=320, blur_threshold=5),
    )
    assert result.image_format == "PNG"
    assert result.mime_type == "image/png"
    assert len(result.sha256) == 64
    assert result.width == 480


def test_invalid_bytes_are_rejected():
    try:
        analyze_image_bytes(b"not really a png")
    except ImageValidationError as error:
        assert error.code == "invalid_image"
    else:  # pragma: no cover
        raise AssertionError("invalid bytes were accepted")


def test_perceptual_hash_flags_same_pixels_with_different_bytes():
    first = analyze_image_bytes(make_png(3, metadata="a"))
    second = analyze_image_bytes(make_png(3, metadata="b"))
    assert first.sha256 != second.sha256
    assert phash_distance(first.phash, second.phash) == 0


def test_combo_is_never_interpreted_as_units():
    text = "Lição concluída! Combo x51. Receber XP"
    platform, confidence, _ = detect_platform(text)
    complete, complete_confidence, phrases = detect_completion(text, platform)
    assert platform == "duolingo"
    assert confidence > 0.7
    assert complete is True
    assert complete_confidence > 0.7
    assert "licao concluida" in phrases


def test_beconfident_detection():
    platform, confidence, _ = detect_platform(
        "Atividade concluída. Calculando pontuação geral. Preparando seu feedback."
    )
    complete, _, _ = detect_completion("Atividade concluída", platform)
    assert platform == "beconfident"
    assert confidence > 0.8
    assert complete

