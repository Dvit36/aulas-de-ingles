from __future__ import annotations

from typing import Any

from english_leaderboard.ui_styles import render_global_styles, responsive_css


class FakeStreamlit:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def markdown(self, body: str, **kwargs: Any) -> None:
        self.calls.append((body, kwargs))


def test_responsive_css_contains_required_mobile_behaviour() -> None:
    css = responsive_css()

    assert "@media (max-width: 768px)" in css
    assert "font-size: 16px !important" in css
    assert "flex-direction: column" in css
    assert "overflow-x: auto" in css
    assert "max-width: 100%" in css
    assert ".stButton > button" in css
    assert '[data-testid="stLinkButton"]' in css
    assert '[data-testid="stFileUploaderDropzone"]' in css
    assert '[data-testid="stDataFrame"]' in css
    assert '[data-testid="stImage"] img' in css
    assert "padding-top" not in css
    assert ".st-key-top_nav" in css
    assert "flex-flow: row wrap" in css
    assert "min-height: var(--app-touch-target)" in css
    assert "width: 100% !important" in css
    assert '[data-testid="stFileUploaderDropzone"] button' in css
    assert "st-key-review_evidence_" in css
    assert "st-key-duplicate_comparison_" in css
    assert "max-height: min(32rem, 62vh)" in css


def test_styles_apply_robonaticos_visual_identity() -> None:
    css = responsive_css()

    assert "--robo-yellow: #F7C11E" in css
    assert "--robo-red: #C5210D" in css
    assert "--robo-charcoal: #333333" in css
    assert "--robo-shadow: 4px 4px 0" in css
    assert 'font-family: "League Spartan"' in css
    assert ".st-key-brand_header" in css
    assert "background: var(--robo-charcoal)" in css
    assert 'a[aria-current="page"]' in css


def test_styles_avoid_generated_or_position_dependent_selectors() -> None:
    css = responsive_css()

    assert ".st-emotion-cache-" not in css
    assert ".css-" not in css
    assert ":nth-child" not in css
    assert "style^=" not in css


def test_render_global_styles_uses_one_static_markdown_block() -> None:
    fake = FakeStreamlit()

    returned = render_global_styles(fake)

    assert returned == responsive_css()
    assert len(fake.calls) == 1
    body, kwargs = fake.calls[0]
    assert body == f"<style>\n{returned}\n</style>"
    assert kwargs == {"unsafe_allow_html": True}


def test_leaderboard_podium_and_board_styles_exist() -> None:
    css = responsive_css()

    assert ".robo-podium {" in css
    assert ".robo-podium-block {" in css
    assert ".robo-board-row {" in css
    assert ".robo-board-badge {" in css
    # O pódio herda o cartão escuro e o contorno da identidade Robonáticos.
    assert "background: var(--robo-charcoal)" in css
    assert "border: var(--robo-border)" in css
