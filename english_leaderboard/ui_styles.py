"""Global, mobile-first layout helpers for the Streamlit interface.

The stylesheet intentionally avoids generated Emotion class names.  Streamlit
``data-testid`` selectors are used only for structural elements that have no useful
semantic selector (main container, columns, tables, tabs, and sidebar).
"""

from __future__ import annotations

from typing import Any

_RESPONSIVE_CSS = r"""
:root {
  --app-content-max-width: 90rem;
  --app-touch-target: 2.75rem;
  --app-mobile-gutter: 0.875rem;
}

html {
  -webkit-text-size-adjust: 100%;
  text-size-adjust: 100%;
}

.stApp {
  line-height: 1.5;
}

[data-testid="stMainBlockContainer"],
.block-container {
  width: 100%;
  max-width: var(--app-content-max-width);
  padding-right: clamp(1rem, 3vw, 3rem);
  padding-bottom: 2rem;
  padding-left: clamp(1rem, 3vw, 3rem);
}

main,
main section,
.element-container {
  min-width: 0;
}

h1,
h2,
h3,
h4,
h5,
h6,
p,
li,
label {
  overflow-wrap: anywhere;
}

main img,
.stImage img,
[data-testid="stImage"] img {
  display: block;
  max-width: 100%;
  height: auto;
  object-fit: contain;
}

[data-testid="stDataFrame"] {
  width: 100%;
  max-width: 100%;
  min-width: 0;
}

[data-testid="stTable"] {
  max-width: 100%;
  overflow-x: auto;
  overscroll-behavior-inline: contain;
  -webkit-overflow-scrolling: touch;
}

[data-testid="stTable"] table {
  width: max-content;
  min-width: 100%;
}

.stButton > button,
.stDownloadButton > button,
[data-testid="stFormSubmitButton"] > button,
[data-testid="stLinkButton"] a {
  min-height: var(--app-touch-target);
  white-space: normal;
}

.st-key-top_nav {
  position: sticky;
  top: 3.25rem;
  z-index: 990;
  width: 100%;
  margin-bottom: 1.25rem;
  padding: 0.5rem;
  border: 1px solid color-mix(in srgb, currentColor 18%, transparent);
  border-radius: 0.75rem;
  background: var(--background-color);
  box-shadow: 0 0.35rem 1.1rem rgb(0 0 0 / 8%);
}

.st-key-top_nav[data-testid="stHorizontalBlock"] {
  flex-flow: row wrap;
  align-items: center;
  gap: 0.35rem;
}

.st-key-top_nav [data-testid="stPageLink"] {
  flex: 0 0 auto;
  width: auto;
}

.st-key-top_nav [data-testid="stPageLink"] a {
  min-height: var(--app-touch-target);
  padding-inline: 0.8rem;
  border-radius: 0.55rem;
  white-space: nowrap;
}

.st-key-login_card,
.st-key-account_card {
  width: 100%;
  max-width: 34rem;
  margin-inline: auto;
}

@media (max-width: 768px) {
  [data-testid="stMainBlockContainer"],
  .block-container {
    padding-right: max(var(--app-mobile-gutter), env(safe-area-inset-right));
    padding-bottom: max(1.5rem, env(safe-area-inset-bottom));
    padding-left: max(var(--app-mobile-gutter), env(safe-area-inset-left));
  }

  [data-testid="stSidebarContent"] {
    padding-right: max(1rem, env(safe-area-inset-right));
    padding-left: max(1rem, env(safe-area-inset-left));
  }

  h1 {
    font-size: clamp(1.75rem, 8vw, 2.25rem);
    line-height: 1.15;
  }

  h2 {
    font-size: clamp(1.4rem, 6vw, 1.8rem);
    line-height: 1.2;
  }

  h3 {
    font-size: clamp(1.15rem, 5vw, 1.45rem);
    line-height: 1.25;
  }

  input,
  textarea,
  select,
  [contenteditable="true"] {
    font-size: 16px !important;
  }

  input,
  select,
  .stButton > button,
  .stDownloadButton > button,
  [data-testid="stFormSubmitButton"] > button,
  [data-testid="stLinkButton"] a {
    min-height: var(--app-touch-target);
  }

  .stButton,
  .stDownloadButton,
  [data-testid="stFormSubmitButton"],
  [data-testid="stLinkButton"],
  .stButton > button,
  .stDownloadButton > button,
  [data-testid="stFormSubmitButton"] > button,
  [data-testid="stLinkButton"] a {
    width: 100% !important;
  }

  [data-testid="stFileUploaderDropzone"] {
    min-height: 7rem;
  }

  [data-testid="stFileUploaderDropzone"] button {
    min-height: var(--app-touch-target);
  }

  [data-testid="stHorizontalBlock"] {
    flex-direction: column;
    align-items: stretch;
    gap: 0.75rem;
  }

  [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
    flex: 1 1 100%;
    width: 100%;
    min-width: 0;
  }

  .st-key-top_nav {
    top: 3rem;
    margin-bottom: 1rem;
    padding: 0.4rem;
  }

  .st-key-top_nav[data-testid="stHorizontalBlock"] {
    flex-flow: row wrap;
    align-items: center;
    gap: 0.25rem;
  }

  .st-key-top_nav [data-testid="stPageLink"],
  .st-key-top_nav [data-testid="stPageLink"] a {
    width: auto;
  }

  [data-testid="stDataFrame"] {
    width: 100%;
    max-width: 100%;
    min-width: 0;
  }

  [data-testid="stTable"] {
    width: 100%;
    overflow-x: auto;
  }

  [data-testid="stTabs"] [role="tablist"] {
    justify-content: flex-start;
    overflow-x: auto;
    overscroll-behavior-inline: contain;
    scrollbar-width: thin;
  }

  [data-testid="stTabs"] [role="tab"] {
    flex: 0 0 auto;
    min-height: var(--app-touch-target);
  }
}

@media (prefers-reduced-motion: reduce) {
  *,
  *::before,
  *::after {
    scroll-behavior: auto !important;
    transition-duration: 0.01ms !important;
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
  }
}
""".strip()


def responsive_css() -> str:
    """Return the static responsive stylesheet without importing Streamlit."""

    return _RESPONSIVE_CSS


def render_global_styles(st_module: Any) -> str:
    """Inject the global stylesheet through a Streamlit-compatible module.

    The return value is the same CSS string passed to ``st.markdown`` and is useful
    for diagnostics.  The content is static and never interpolates user input.
    """

    css = responsive_css()
    st_module.markdown(f"<style>\n{css}\n</style>", unsafe_allow_html=True)
    return css


__all__ = ["render_global_styles", "responsive_css"]
