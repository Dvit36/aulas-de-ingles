"""Robonáticos brand and mobile-first layout helpers for Streamlit."""

from __future__ import annotations

from typing import Any


_RESPONSIVE_CSS = r"""
@import url('https://fonts.googleapis.com/css2?family=Arimo:wght@400;500;600;700&family=League+Spartan:wght@600;700;800&display=swap');

:root {
  --robo-yellow: #F7C11E;
  --robo-red: #C5210D;
  --robo-ink: #20201D;
  --robo-charcoal: #333333;
  --robo-grey: #545454;
  --robo-silver: #D9D9D9;
  --robo-white: #FFFFFF;
  --robo-border: 3px solid var(--robo-ink);
  --robo-shadow: 4px 4px 0 var(--robo-ink);
  --app-content-max-width: 90rem;
  --app-touch-target: 2.75rem;
  --app-mobile-gutter: 0.875rem;
}

html {
  -webkit-text-size-adjust: 100%;
  text-size-adjust: 100%;
}

.stApp,
[data-testid="stAppViewContainer"] {
  background: var(--robo-silver);
  color: var(--robo-ink);
  font-family: "Arimo", Arial, sans-serif;
  line-height: 1.5;
}

[data-testid="stHeader"] {
  background: var(--robo-charcoal);
  border-bottom: 3px solid var(--robo-yellow);
}

[data-testid="stToolbar"] {
  color: var(--robo-white);
}

[data-testid="stMainBlockContainer"],
.block-container {
  width: 100%;
  max-width: var(--app-content-max-width);
  padding-right: clamp(1rem, 3vw, 3rem);
  padding-bottom: 3rem;
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
h6 {
  color: var(--robo-ink);
  font-family: "League Spartan", Impact, sans-serif;
  font-weight: 800;
  letter-spacing: 0.025em;
  line-height: 1.05;
  overflow-wrap: anywhere;
  text-transform: uppercase;
}

p,
li,
label {
  overflow-wrap: anywhere;
}

a {
  color: var(--robo-red);
  font-weight: 700;
}

main img,
.stImage img,
[data-testid="stImage"] img {
  display: block;
  max-width: 100%;
  height: auto;
  object-fit: contain;
}

/* Cabeçalho de marca e navegação superior */
.st-key-brand_header {
  width: 100%;
  margin-bottom: 0;
  padding: 0.8rem 1rem;
  border: var(--robo-border);
  border-bottom: 0;
  border-radius: 1.5rem 1.5rem 0 0;
  background: var(--robo-charcoal);
}

.st-key-brand_header[data-testid="stHorizontalBlock"] {
  flex-flow: row nowrap;
  align-items: center;
  gap: 1rem;
}

.st-key-brand_header [data-testid="stImage"] {
  flex: 0 0 auto;
}

.st-key-brand_header .brand-copy {
  min-width: 0;
  color: var(--robo-white);
}

.st-key-brand_header .brand-title {
  margin: 0;
  color: var(--robo-yellow);
  font-family: "League Spartan", Impact, sans-serif;
  font-size: clamp(1.1rem, 2.3vw, 1.65rem);
  font-weight: 800;
  letter-spacing: 0.04em;
  line-height: 1;
  text-transform: uppercase;
}

.st-key-brand_header .brand-subtitle {
  margin: 0.25rem 0 0;
  color: var(--robo-white);
  font-size: 0.78rem;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

.st-key-top_nav {
  position: sticky;
  top: 3.25rem;
  z-index: 990;
  width: 100%;
  margin-bottom: 1.75rem;
  padding: 0.45rem 0.65rem;
  border: var(--robo-border);
  border-radius: 0 0 1.25rem 1.25rem;
  background: var(--robo-charcoal);
  box-shadow: var(--robo-shadow);
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
  padding-inline: 0.85rem;
  border: 2px solid transparent;
  border-radius: 999px;
  color: var(--robo-white);
  font-size: 0.88rem;
  font-weight: 800;
  text-decoration: none;
  white-space: nowrap;
}

.st-key-top_nav [data-testid="stPageLink"] a * {
  color: var(--robo-white) !important;
}

.st-key-top_nav [data-testid="stPageLink"] a:hover {
  border-color: var(--robo-yellow);
  color: var(--robo-yellow);
}

.st-key-top_nav [data-testid="stPageLink"] a:hover * {
  color: var(--robo-yellow) !important;
}

.st-key-top_nav [data-testid="stPageLink"] a[aria-current="page"] {
  border-color: var(--robo-ink);
  background: var(--robo-yellow);
  color: var(--robo-ink);
  box-shadow: 2px 2px 0 var(--robo-red);
}

.st-key-top_nav [data-testid="stPageLink"] a[aria-current="page"] * {
  color: var(--robo-ink) !important;
}

/* Superfícies, cartões e métricas */
[data-testid="stVerticalBlockBorderWrapper"] {
  border: var(--robo-border) !important;
  border-radius: 1.5rem !important;
  background: var(--robo-white);
  box-shadow: var(--robo-shadow);
}

.st-key-public_hero,
.st-key-login_card,
.st-key-account_card {
  padding: clamp(1rem, 2.5vw, 2rem) !important;
  border: var(--robo-border) !important;
  border-radius: 1.5rem !important;
  background: var(--robo-white) !important;
  box-shadow: var(--robo-shadow) !important;
}

.st-key-public_hero [data-testid="stVerticalBlockBorderWrapper"],
.st-key-login_card [data-testid="stVerticalBlockBorderWrapper"] {
  padding: clamp(0.35rem, 1vw, 0.9rem);
}

.st-key-login_card,
.st-key-account_card {
  width: 100%;
  max-width: 38rem;
  margin-inline: auto;
}

.st-key-public_hero .hero-kicker,
.st-key-login_card .login-kicker {
  display: inline-block;
  margin-bottom: 0.25rem;
  padding: 0.35rem 0.75rem;
  border: 2px solid var(--robo-ink);
  border-radius: 999px;
  background: var(--robo-yellow);
  color: var(--robo-ink);
  font-size: 0.78rem;
  font-weight: 800;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

[data-testid="stMetric"] {
  height: 100%;
  padding: 1rem 1.1rem;
  border: var(--robo-border);
  border-radius: 1rem;
  background: var(--robo-white);
  box-shadow: var(--robo-shadow);
}

[data-testid="stMetricLabel"] {
  color: var(--robo-grey);
  font-weight: 800;
  letter-spacing: 0.035em;
  text-transform: uppercase;
}

[data-testid="stMetricValue"] {
  color: var(--robo-red);
  font-family: "League Spartan", Impact, sans-serif;
  font-weight: 800;
}

/* Controles */
.stButton > button,
.stDownloadButton > button,
[data-testid="stFormSubmitButton"] > button,
[data-testid="stLinkButton"] a {
  min-height: var(--app-touch-target);
  border: 2px solid var(--robo-ink) !important;
  border-radius: 999px !important;
  background: var(--robo-yellow);
  color: var(--robo-ink);
  font-weight: 800;
  box-shadow: 3px 3px 0 var(--robo-ink);
  white-space: normal;
  transition: transform 120ms ease, box-shadow 120ms ease;
}

.stButton > button:hover,
.stDownloadButton > button:hover,
[data-testid="stFormSubmitButton"] > button:hover,
[data-testid="stLinkButton"] a:hover {
  border-color: var(--robo-ink) !important;
  color: var(--robo-ink);
  transform: translate(1px, 1px);
  box-shadow: 2px 2px 0 var(--robo-ink);
}

.stButton > button[kind="primary"],
[data-testid="stFormSubmitButton"] > button[kind="primary"] {
  background: var(--robo-red);
  color: var(--robo-white);
}

input,
textarea,
[data-baseweb="select"] > div,
[data-testid="stNumberInput"] > div > div {
  border-color: var(--robo-ink) !important;
  border-radius: 0.65rem !important;
  background: var(--robo-white) !important;
}

input:focus,
textarea:focus,
[data-baseweb="select"] > div:focus-within {
  outline: 3px solid var(--robo-yellow) !important;
  outline-offset: 1px;
}

[data-testid="stFileUploaderDropzone"] {
  border: 3px dashed var(--robo-grey);
  border-radius: 1rem;
  background: #f7f7f5;
}

[data-testid="stCheckbox"] svg {
  color: var(--robo-red);
}

/* Dados, abas e mensagens */
[data-testid="stDataFrame"] {
  width: 100%;
  max-width: 100%;
  min-width: 0;
  overflow: hidden;
  border: 2px solid var(--robo-ink);
  border-radius: 1rem;
  background: var(--robo-white);
}

[data-testid="stTable"] {
  max-width: 100%;
  overflow-x: auto;
  border: 2px solid var(--robo-ink);
  border-radius: 1rem;
  background: var(--robo-white);
  overscroll-behavior-inline: contain;
  -webkit-overflow-scrolling: touch;
}

[data-testid="stTable"] table {
  width: max-content;
  min-width: 100%;
}

[data-testid="stTabs"] [role="tablist"] {
  gap: 0.35rem;
  border-bottom: 3px solid var(--robo-ink);
}

[data-testid="stTabs"] [role="tab"] {
  border-radius: 0.75rem 0.75rem 0 0;
  color: var(--robo-grey);
  font-weight: 800;
}

[data-testid="stTabs"] [role="tab"][aria-selected="true"] {
  background: var(--robo-yellow);
  color: var(--robo-ink);
}

[data-testid="stAlert"] {
  border: 2px solid var(--robo-ink);
  border-radius: 1rem;
  box-shadow: 3px 3px 0 rgb(32 32 29 / 18%);
}

hr {
  border-color: var(--robo-ink);
  border-width: 2px;
}

@media (max-width: 768px) {
  [data-testid="stMainBlockContainer"],
  .block-container {
    padding-right: max(var(--app-mobile-gutter), env(safe-area-inset-right));
    padding-bottom: max(2rem, env(safe-area-inset-bottom));
    padding-left: max(var(--app-mobile-gutter), env(safe-area-inset-left));
  }

  h1 {
    font-size: clamp(1.75rem, 8vw, 2.25rem);
    line-height: 1.05;
  }

  h2 {
    font-size: clamp(1.4rem, 6vw, 1.8rem);
    line-height: 1.1;
  }

  h3 {
    font-size: clamp(1.15rem, 5vw, 1.45rem);
    line-height: 1.15;
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

  .st-key-brand_header {
    padding: 0.7rem 0.75rem;
    border-radius: 1rem 1rem 0 0;
  }

  .st-key-brand_header[data-testid="stHorizontalBlock"] {
    flex-flow: row nowrap;
    align-items: center;
    gap: 0.65rem;
  }

  .st-key-brand_header .brand-subtitle {
    display: none;
  }

  .st-key-top_nav {
    top: 3rem;
    margin-bottom: 1.25rem;
    padding: 0.4rem;
    border-radius: 0 0 1rem 1rem;
  }

  .st-key-top_nav[data-testid="stHorizontalBlock"] {
    flex-flow: row wrap;
    align-items: center;
    gap: 0.25rem;
  }

  .st-key-top_nav [data-testid="stPageLink"],
  .st-key-top_nav [data-testid="stPageLink"] a {
    width: auto !important;
  }

  .st-key-top_nav [data-testid="stPageLink"] a {
    padding-inline: 0.65rem;
    font-size: 0.8rem;
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

  [data-testid="stMetric"] {
    padding: 0.85rem 1rem;
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
    """Return the static responsive and brand stylesheet."""

    return _RESPONSIVE_CSS


def render_global_styles(st_module: Any) -> str:
    """Inject the static stylesheet through a Streamlit-compatible module."""

    css = responsive_css()
    st_module.markdown(f"<style>\n{css}\n</style>", unsafe_allow_html=True)
    return css


__all__ = ["render_global_styles", "responsive_css"]
