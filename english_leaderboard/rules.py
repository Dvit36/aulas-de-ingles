from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterable, Sequence

from .models import CheckOutcome, SubmissionStatus


@dataclass(frozen=True)
class RuleResult:
    name: str
    outcome: CheckOutcome
    required: bool
    score: float
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AnalysisDecision:
    status: SubmissionStatus
    confidence: float
    recognized_units: int
    detected_platform: str | None
    ocr_text: str
    checks: tuple[RuleResult, ...]
    reason: str


DUOLINGO_MARKERS = (
    "duolingo",
    "receber xp",
    "receberxp",
    "total de xp",
    "totaldexp",
    "total dexp",
    "licao concluida",
    "licao perfeita",
    "aventura concluida",
    "combo",
    "super agil",
    "relampago",
)
DUOLINGO_COMPLETIONS = (
    "licao concluida",
    "licao perfeita",
    "aventura concluida",
    "sem erros",
    "nasce uma estrela",
    "nota alta",
    "quem e voce",
)
BECONFIDENT_MARKERS = (
    "beconfident",
    "atividade concluida",
    "conversa concluida",
    "voce conseguiu",
    "calculando pontuacao geral",
    "preparando seu feedback",
    "encontrando pontos para melhorar",
)
BECONFIDENT_COMPLETIONS = (
    "atividade concluida",
    "conversa concluida",
    "voce conseguiu",
)
PORTUGUESE_MARKERS = {
    "a", "as", "ao", "com", "como", "da", "das", "de", "do", "dos",
    "e", "em", "eu", "foi", "mais", "na", "nas", "no", "nos", "o",
    "os", "para", "por", "que", "se", "sobre", "uma", "um", "video",
}
ENGLISH_MARKERS = {
    "a", "about", "and", "are", "for", "from", "i", "in", "is", "it",
    "lesson", "of", "on", "that", "the", "this", "to", "was", "with",
}


def normalize_text(text: str | None) -> str:
    decomposed = unicodedata.normalize("NFKD", text or "")
    without_accents = "".join(char for char in decomposed if not unicodedata.combining(char))
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9:/._ -]+", " ", without_accents.lower())).strip()


def _marker_score(text: str, markers: Sequence[str]) -> tuple[int, list[str]]:
    found = [marker for marker in markers if marker in text]
    return len(found), found


def detect_platform(
    text: str,
    *,
    color_signals: dict[str, float] | None = None,
) -> tuple[str | None, float, dict[str, Any]]:
    normalized = normalize_text(text)
    duo_count, duo_found = _marker_score(normalized, DUOLINGO_MARKERS)
    be_count, be_found = _marker_score(normalized, BECONFIDENT_MARKERS)
    colors = color_signals or {}
    serializable_colors = (
        colors.as_dict() if hasattr(colors, "as_dict") else dict(colors)
    )
    # Color is supporting evidence only and cannot create a platform by itself.
    purple_support = min(float(colors.get("purple_ratio", 0.0)) * 1.5, 0.18)
    green_support = min(float(colors.get("green_ratio", 0.0)), 0.12)
    duo_score = min(0.98, 0.48 + 0.14 * duo_count + green_support) if duo_count else 0.0
    be_score = min(0.98, 0.52 + 0.14 * be_count + purple_support) if be_count else 0.0

    platform: str | None = None
    confidence = 0.0
    if duo_score > be_score:
        platform, confidence = "duolingo", duo_score
    elif be_score > duo_score:
        platform, confidence = "beconfident", be_score
    elif duo_score and be_score:
        platform, confidence = "ambiguous", min(duo_score, be_score)

    return platform, round(confidence, 4), {
        "duolingo_markers": duo_found,
        "beconfident_markers": be_found,
        "color_support_only": serializable_colors,
    }


def detect_completion(text: str, platform: str | None) -> tuple[bool, float, list[str]]:
    normalized = normalize_text(text)
    if platform == "duolingo":
        phrases = DUOLINGO_COMPLETIONS
    elif platform == "beconfident":
        phrases = BECONFIDENT_COMPLETIONS
    else:
        phrases = DUOLINGO_COMPLETIONS + BECONFIDENT_COMPLETIONS
    found = [phrase for phrase in phrases if phrase in normalized]
    # The number following "combo" is intentionally never parsed as units.
    confidence = min(0.98, 0.76 + 0.1 * len(found)) if found else 0.0
    return bool(found), round(confidence, 4), found


def likely_portuguese(text: str) -> tuple[bool, float]:
    normalized = normalize_text(text)
    words = re.findall(r"\b[a-z]+\b", normalized)
    if not words:
        return False, 0.0
    pt = sum(word in PORTUGUESE_MARKERS for word in words)
    en = sum(word in ENGLISH_MARKERS for word in words)
    accented = sum(char in (text or "").lower() for char in "áàâãéêíóôõúç")
    score = min(1.0, (pt + min(accented, 4) * 0.75) / max(3.0, min(len(words), 12) * 0.25))
    return pt + accented >= max(2, en), round(score, 4)


def maximum_text_similarity(text: str, previous: Iterable[str]) -> float:
    normalized = normalize_text(text)
    if not normalized:
        return 0.0
    return max(
        (SequenceMatcher(None, normalized, normalize_text(item)).ratio() for item in previous if item),
        default=0.0,
    )


def _image_value(image: Any, name: str, default: Any = None) -> Any:
    if isinstance(image, dict):
        return image.get(name, default)
    return getattr(image, name, default)


def _ocr_value(result: Any, name: str, default: Any = None) -> Any:
    if isinstance(result, dict):
        return result.get(name, default)
    return getattr(result, name, default)


def analyze_submission_rules(
    *,
    activity: Any,
    images: Sequence[Any],
    ocr_results: Sequence[Any],
    title: str | None = None,
    url: str | None = None,
    summary: str | None = None,
    exact_duplicate_flags: Sequence[bool] | None = None,
    similar_duplicate_flags: Sequence[bool] | None = None,
    previous_summaries: Iterable[str] = (),
    auto_approve_confidence: float = 0.88,
) -> AnalysisDecision:
    checks: list[RuleResult] = []
    exact_flags = list(exact_duplicate_flags or [False] * len(images))
    similar_flags = list(similar_duplicate_flags or [False] * len(images))
    requires_images = bool(getattr(activity, "requires_images", True))

    image_count_ok = bool(images) if requires_images else True
    checks.append(
        RuleResult(
            "required_images",
            CheckOutcome.PASS if image_count_ok else CheckOutcome.FAIL,
            requires_images,
            1.0 if image_count_ok else 0.0,
            "Imagens recebidas" if image_count_ok else "A atividade exige imagem",
            {"count": len(images)},
        )
    )

    invalid_images = [
        index for index, image in enumerate(images) if not _image_value(image, "valid", True)
    ]
    checks.append(
        RuleResult(
            "valid_image_content",
            CheckOutcome.FAIL if invalid_images else CheckOutcome.PASS,
            requires_images,
            0.0 if invalid_images else 1.0,
            "Arquivo de imagem inválido" if invalid_images else "Conteúdo real validado",
            {"invalid_indexes": invalid_images, "hard_reject": bool(invalid_images)},
        )
    )

    blurry = [
        index
        for index, image in enumerate(images)
        if _image_value(image, "legible", True) is False
    ]
    checks.append(
        RuleResult(
            "legibility",
            CheckOutcome.REVIEW if blurry else CheckOutcome.PASS,
            requires_images,
            0.45 if blurry else 1.0,
            "Imagem possivelmente ilegível" if blurry else "Legibilidade suficiente",
            {"indexes": blurry},
        )
    )

    exact = [index for index, value in enumerate(exact_flags) if value]
    checks.append(
        RuleResult(
            "exact_duplicate",
            CheckOutcome.FAIL if exact else CheckOutcome.PASS,
            True,
            0.0 if exact else 1.0,
            "Duplicata exata comprovada" if exact else "Nenhuma duplicata exata",
            {"indexes": exact, "hard_reject": bool(exact)},
        )
    )
    similar = [index for index, value in enumerate(similar_flags) if value]
    checks.append(
        RuleResult(
            "perceptual_similarity",
            CheckOutcome.REVIEW if similar else CheckOutcome.PASS,
            False,
            0.55 if similar else 1.0,
            "Imagem visualmente semelhante; revisão necessária" if similar else "Sem alerta perceptual",
            {"indexes": similar},
        )
    )

    all_texts = [str(_ocr_value(result, "text", "") or "") for result in ocr_results]
    consolidated_text = "\n".join(text for text in all_texts if text).strip()
    ocr_confidences = [
        float(_ocr_value(result, "confidence", 0.0) or 0.0) for result in ocr_results
    ]
    ocr_confidence = sum(ocr_confidences) / len(ocr_confidences) if ocr_confidences else 0.0
    ocr_ok = bool(normalize_text(consolidated_text))
    checks.append(
        RuleResult(
            "ocr_text",
            CheckOutcome.PASS if ocr_ok and ocr_confidence >= 0.55 else CheckOutcome.REVIEW,
            requires_images,
            min(1.0, ocr_confidence),
            "Texto extraído localmente" if ocr_ok else "OCR não encontrou texto confiável",
            {"confidence": round(ocr_confidence, 4), "characters": len(consolidated_text)},
        )
    )

    platforms: list[str] = []
    platform_scores: list[float] = []
    completions = 0
    completion_scores: list[float] = []
    per_image_details: list[dict[str, Any]] = []
    for index, text in enumerate(all_texts):
        colors = _image_value(images[index], "color_signals", {}) if index < len(images) else {}
        platform, platform_score, details = detect_platform(text, color_signals=colors)
        completed, completion_score, phrases = detect_completion(text, platform)
        if platform and platform != "ambiguous":
            platforms.append(platform)
            platform_scores.append(platform_score)
        if completed and index not in exact:
            completions += 1
            completion_scores.append(completion_score)
        per_image_details.append(
            {
                "index": index,
                "platform": platform,
                "platform_confidence": platform_score,
                "completion_phrases": phrases,
                "counted_units": 1 if completed and index not in exact else 0,
                **details,
            }
        )

    declared_group = getattr(activity, "code", "") == "duolingo_beconfident"
    unique_platforms = set(platforms)
    detected_platform = next(iter(unique_platforms)) if len(unique_platforms) == 1 else (
        "mixed" if unique_platforms else None
    )
    if declared_group:
        platform_ok = bool(platforms) and len(platforms) == len(images)
        completion_ok = completions == len(images) and completions > 0
        avg_platform = sum(platform_scores) / len(platform_scores) if platform_scores else 0.0
        avg_completion = sum(completion_scores) / len(completion_scores) if completion_scores else 0.0
        checks.extend(
            [
                RuleResult(
                    "declared_platform",
                    CheckOutcome.PASS if platform_ok and avg_platform >= 0.80 else CheckOutcome.REVIEW,
                    True,
                    avg_platform,
                    "Plataforma compatível" if platform_ok else "Plataforma não conclusiva",
                    {"images": per_image_details},
                ),
                RuleResult(
                    "completion_evidence",
                    CheckOutcome.PASS if completion_ok and avg_completion >= 0.80 else CheckOutcome.REVIEW,
                    True,
                    avg_completion,
                    "Cada imagem representa uma conclusão" if completion_ok else "Conclusão não confirmada em todas as imagens",
                    {"recognized_units": completions, "combo_ignored": True},
                ),
            ]
        )

    if bool(getattr(activity, "requires_title_or_url", False)):
        present = bool((title or "").strip() or (url or "").strip())
        checks.append(
            RuleResult(
                "title_or_url",
                CheckOutcome.PASS if present else CheckOutcome.FAIL,
                True,
                1.0 if present else 0.0,
                "Título/URL informado" if present else "Informe título ou URL",
            )
        )

    if bool(getattr(activity, "requires_summary", False)):
        clean_summary = (summary or "").strip()
        minimum = int(getattr(activity, "summary_min_chars", 0) or 0)
        length_ok = len(clean_summary) >= minimum
        checks.append(
            RuleResult(
                "summary_length",
                CheckOutcome.PASS if length_ok else CheckOutcome.FAIL,
                True,
                min(1.0, len(clean_summary) / max(1, minimum)),
                "Tamanho mínimo atendido" if length_ok else f"Resumo deve ter pelo menos {minimum} caracteres",
                {"characters": len(clean_summary), "minimum": minimum},
            )
        )
        portuguese, language_score = likely_portuguese(clean_summary)
        checks.append(
            RuleResult(
                "summary_language",
                CheckOutcome.PASS if portuguese else CheckOutcome.REVIEW,
                True,
                language_score,
                "Texto provavelmente em português" if portuguese else "Idioma do texto não é conclusivo",
            )
        )
        similarity = maximum_text_similarity(clean_summary, previous_summaries)
        checks.append(
            RuleResult(
                "summary_similarity",
                CheckOutcome.REVIEW if similarity >= 0.9 else CheckOutcome.PASS,
                False,
                1.0 - similarity,
                "Resumo muito semelhante a entrega anterior" if similarity >= 0.9 else "Sem similaridade textual elevada",
                {"maximum_similarity": round(similarity, 4)},
            )
        )

    if bool(getattr(activity, "content_review_required", False)):
        checks.append(
            RuleResult(
                "content_quality",
                CheckOutcome.REVIEW,
                True,
                0.5,
                "Qualidade/veracidade do conteúdo exige avaliação humana",
            )
        )

    hard_reject = any(
        result.outcome == CheckOutcome.FAIL and result.details.get("hard_reject")
        for result in checks
    )
    required_problem = any(
        result.required and result.outcome != CheckOutcome.PASS for result in checks
    )
    any_review = any(result.outcome == CheckOutcome.REVIEW for result in checks)
    scored = [result.score for result in checks if result.required]
    confidence = round(sum(scored) / len(scored), 4) if scored else 0.0

    minimum_required_score = min(scored, default=0.0)
    if hard_reject:
        status = SubmissionStatus.REJECTED
        reason = "Arquivo inválido ou duplicata exata comprovada"
    elif (
        bool(getattr(activity, "auto_approvable", False))
        and not required_problem
        and not any_review
        and confidence >= auto_approve_confidence
        and minimum_required_score >= 0.80
    ):
        status = SubmissionStatus.APPROVED_AUTO
        reason = "Todas as regras obrigatórias passaram com alta confiança"
    else:
        status = SubmissionStatus.NEEDS_REVIEW
        reason = "Revisão administrativa necessária"

    return AnalysisDecision(
        status=status,
        confidence=confidence,
        recognized_units=(
            0
            if status == SubmissionStatus.REJECTED
            else completions if declared_group else (1 if images else 0)
        ),
        detected_platform=detected_platform,
        ocr_text=consolidated_text,
        checks=tuple(checks),
        reason=reason,
    )


__all__ = [
    "AnalysisDecision",
    "RuleResult",
    "analyze_submission_rules",
    "detect_completion",
    "detect_platform",
    "likely_portuguese",
    "maximum_text_similarity",
    "normalize_text",
]
