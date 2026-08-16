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
    assert "[data-testid=\"stLinkButton\"]" in css
    assert "[data-testid=\"stFileUploaderDropzone\"]" in css
    assert "[data-testid=\"stDataFrame\"]" in css
    assert "[data-testid=\"stImage\"] img" in css
    assert "padding-top" not in css
    assert ".st-key-top_nav" in css
    assert 'flex-flow: row wrap' in css
    assert 'min-height: var(--app-touch-target)' in css
    assert 'width: 100% !important' in css
    assert '[data-testid="stFileUploaderDropzone"] button' in css


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
