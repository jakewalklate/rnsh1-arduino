"""Shared look for utility pages: white cards on the grey page, an accent colour,
status pills and pass/fail tiles.

Streamlit gives every container created with key="x" the CSS class "st-key-x", so
card() keys start with "ui-card" and the stylesheet targets that prefix. Call
inject_styles() once per run; app.py does this, so pages only use the helpers.
It also gives Streamlit's own metrics, expanders and alerts the same card look.
"""

from html import escape

import streamlit as st

ACCENT = "#1f6fb2"
ACCENT_SOFT = "#e8f1fa"
PASS = "#1e8e5a"
PASS_SOFT = "#e6f5ee"
FAIL = "#c2410c"
FAIL_SOFT = "#fdf0e9"
WARN = "#b45309"
WARN_SOFT = "#fef3c7"
DANGER = "#c62828"
DANGER_DARK = "#a51f1f"
MUTED = "#6b7280"
BORDER = "#e3e6eb"

_CSS = f"""
<style>
[class*="st-key-ui-card"] {{
    background: #ffffff;
    border: 1px solid {BORDER};
    border-radius: 12px;
    padding: 1.1rem 1.25rem 1.2rem;
    box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04), 0 4px 14px rgba(16, 24, 40, 0.06);
}}
[class*="st-key-ui-card-accent"] {{
    border-top: 4px solid {ACCENT};
}}
[class*="st-key-ui-card-muted"] {{
    background: #fafbfc;
    box-shadow: none;
}}
.ui-card-title {{
    display: flex; align-items: center; gap: 0.6rem;
    font-size: 1.05rem; font-weight: 650; color: #1f2937; margin: 0 0 0.15rem;
}}
.ui-card-sub {{ color: {MUTED}; font-size: 0.88rem; margin: 0 0 0.4rem; }}
.ui-step {{
    display: inline-flex; align-items: center; justify-content: center;
    width: 1.6rem; height: 1.6rem; border-radius: 50%;
    background: {ACCENT}; color: #fff; font-size: 0.85rem; font-weight: 700;
    flex: none;
}}
.ui-hero {{
    display: flex; justify-content: space-between; align-items: center;
    gap: 1rem; flex-wrap: wrap;
    background: linear-gradient(135deg, {ACCENT} 0%, #2b8ac9 100%);
    color: #fff; border-radius: 12px; padding: 1.1rem 1.4rem;
    box-shadow: 0 6px 18px rgba(31, 111, 178, 0.25); margin-bottom: 1rem;
}}
.ui-hero h3 {{ color: #fff; margin: 0 0 0.2rem !important; padding: 0 !important; font-size: 1.3rem; }}
.ui-hero p {{ margin: 0 !important; opacity: 0.92; font-size: 0.92rem; max-width: 46rem; }}
/* The top bar is empty (toolbar hidden) but opaque, and covered the first panel. */
header[data-testid="stHeader"] {{ background: transparent; pointer-events: none; }}
.ui-hero .ui-pills {{ display: flex; gap: 0.5rem; flex-wrap: wrap; }}
.ui-pill {{
    display: inline-flex; align-items: center; gap: 0.35rem;
    padding: 0.2rem 0.65rem; border-radius: 999px;
    font-size: 0.8rem; font-weight: 600; white-space: nowrap;
}}
.ui-pill::before {{
    content: ""; width: 0.5rem; height: 0.5rem; border-radius: 50%;
    background: currentColor;
}}
.ui-pill.pass {{ background: {PASS_SOFT}; color: {PASS}; }}
.ui-pill.fail {{ background: {FAIL_SOFT}; color: {FAIL}; }}
.ui-pill.warn {{ background: {WARN_SOFT}; color: {WARN}; }}
.ui-pill.info {{ background: {ACCENT_SOFT}; color: {ACCENT}; }}
.ui-pill.neutral {{ background: #eef0f3; color: {MUTED}; }}
.ui-hero .ui-pill {{ background: rgba(255, 255, 255, 0.95); }}
.ui-tiles {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 0.75rem; margin: 0.5rem 0 0.25rem;
}}
.ui-tile {{
    border: 1px solid {BORDER}; border-radius: 10px; padding: 0.75rem 0.85rem;
    background: #fff; position: relative;
}}
.ui-tile.pass {{ border-top: 4px solid {PASS}; }}
.ui-tile.fail {{ border-top: 4px solid {FAIL}; background: {FAIL_SOFT}; }}
.ui-tile.neutral {{ border-top: 4px solid #cfd4db; }}
.ui-tile.highlight {{ border-color: {ACCENT}; box-shadow: 0 0 0 1px {ACCENT} inset; }}
.ui-tile-head {{
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 0.45rem;
}}
.ui-tile-freq {{ font-weight: 700; font-size: 1.05rem; color: #111827; }}
.ui-tile-row {{
    display: flex; justify-content: space-between; font-size: 0.85rem;
    color: {MUTED}; line-height: 1.6;
}}
.ui-tile-row b {{ color: #111827; font-weight: 650; font-variant-numeric: tabular-nums; }}
.ui-tile-note {{ font-size: 0.78rem; color: {MUTED}; margin-top: 0.4rem; }}
.ui-tile-reason {{ font-size: 0.8rem; color: {FAIL}; margin-top: 0.45rem; line-height: 1.35; }}
.ui-steps {{ display: grid; gap: 0.45rem; margin: 0.35rem 0 0.8rem; }}
.ui-steps .ui-card-title {{ font-size: 0.92rem; font-weight: 500; }}
.ui-section {{
    font-size: 0.78rem; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;
    color: {MUTED}; margin: 0.6rem 0 0.3rem;
}}

/* ---- Streamlit elements, app-wide ---- */
/* Streamlit's Deploy button; toolbarMode = "minimal" removes it too. */
[data-testid="stAppDeployButton"] {{ display: none !important; }}
h1 {{ letter-spacing: -0.01em; }}
[data-testid="stMetric"] {{
    background: #f7f9fb; border: 1px solid {BORDER}; border-left: 4px solid {ACCENT};
    border-radius: 10px; padding: 0.65rem 0.9rem;
}}
[data-testid="stExpander"] details {{
    background: #ffffff; border: 1px solid {BORDER}; border-radius: 10px;
    box-shadow: 0 1px 2px rgba(16, 24, 40, 0.04);
}}
[data-testid="stExpander"] summary:hover {{ color: {ACCENT}; }}
[data-testid="stAlert"] > div {{ border-radius: 10px; }}
[data-baseweb="tab-list"] button[aria-selected="true"] p {{ font-weight: 650; }}
[data-testid="stDataFrame"], [data-testid="stDataEditor"] {{
    border-radius: 8px; overflow: hidden;
}}
/* st.divider() is a bare <hr> in a markdown block with 2em margins above and below.
   In the home page's Device tools card that splits two short items, so keep it close:
   only the normal gap between elements on each side. */
[class*="st-key-ui-card-tools"] [data-testid="stMarkdownContainer"]:has(> hr:only-child) {{
    margin-bottom: 0;
}}
[class*="st-key-ui-card-tools"] hr {{
    margin: 0;
}}
/* ---- List rows (patient list) ---- */
/* One bordered table (key ui-rows-<list>): a grey heading row (key ui-rowhead-<list>)
   and data rows (keys ui-row-<id>) built the same way, so their columns line up
   exactly. Used by the patient list and a patient's previous tests. */
[class*="st-key-ui-rows"] {{
    gap: 0 !important; border: 1px solid {BORDER}; border-radius: 10px;
    overflow: hidden; background: #fff;
}}
[class*="st-key-ui-rowhead"] {{
    background: #f5f7fa; padding: 0.7rem 1rem; border-bottom: 1px solid {BORDER};
}}
/* Streamlit pulls each text block up by 1rem to cancel a paragraph's bottom margin.
   These cells are not paragraphs, so without this every cell collapses to a few
   pixels and its text spills out below the row. */
[class*="st-key-ui-row"] [data-testid="stMarkdownContainer"] {{ margin-bottom: 0 !important; }}
[class*="st-key-ui-row-"] {{
    border-top: 1px solid {BORDER}; padding: 0.6rem 1rem;
    transition: background 0.12s ease;
}}
[class*="st-key-ui-rowhead"] + div [class*="st-key-ui-row-"],
[class*="st-key-ui-rowhead"] + [class*="st-key-ui-row-"] {{ border-top: none; }}
[class*="st-key-ui-row-"]:hover {{ background: {ACCENT_SOFT}; }}
.ui-row-head {{
    font-size: 0.85rem; font-weight: 650; color: #374151; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
.ui-row-name {{
    font-weight: 650; color: #111827; line-height: 1.3;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
.ui-row-value {{
    color: #1f2937; line-height: 1.3; font-variant-numeric: tabular-nums;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}}
.ui-row-warn {{
    font-size: 0.75rem; font-weight: 600; color: {WARN}; margin-left: 0.4rem;
    white-space: nowrap;
}}
.ui-row-sub {{ font-size: 0.8rem; color: {MUTED}; line-height: 1.3; }}
/* Keep each row on one line on a phone instead of stacking its cells. */
[class*="st-key-ui-row"] [data-testid="stHorizontalBlock"] {{ flex-wrap: nowrap !important; }}
[class*="st-key-ui-row"] [data-testid="stColumn"] {{ min-width: 0 !important; }}
/* The Open column is a fixed small width on the right; the others share the rest. */
[class*="st-key-ui-row"] [data-testid="stColumn"]:last-child {{
    flex: 0 0 6.5rem !important; width: 6.5rem !important;
}}
.ui-row-head {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
@media (max-width: 640px) {{
    [class*="st-key-ui-row"] [data-testid="stColumn"]:nth-child(3) {{ display: none; }}
    /* Previous tests: Status goes too; ID, date and Open are what matters there. */
    [class*="st-key-ui-rows-tests"] [data-testid="stColumn"]:nth-child(4) {{ display: none; }}
    [class*="st-key-ui-row"] [data-testid="stColumn"]:last-child {{
        flex: 0 0 5.3rem !important; width: 5.3rem !important;
    }}
    [class*="st-key-ui-row-"], [class*="st-key-ui-rowhead"] {{ padding-left: 0.6rem; padding-right: 0.6rem; }}
}}

/* ---- Destructive buttons: give the button a key starting "danger" ---- */
[class*="st-key-danger"] button {{
    background: {DANGER}; border-color: {DANGER}; color: #fff;
}}
[class*="st-key-danger"] button:hover {{
    background: {DANGER_DARK}; border-color: {DANGER_DARK}; color: #fff;
}}
[class*="st-key-danger"] button:focus:not(:active) {{ border-color: {DANGER_DARK}; color: #fff; }}

/* ---- Responsive ---- */
/* Columns that hold panels wrap instead of squashing: on a narrow window each
   panel takes the full width and they stack. Columns inside a panel (form
   fields, number tiles) keep Streamlit's own behaviour. */
[data-testid="stHorizontalBlock"]:has(> [data-testid="stColumn"] [class*="st-key-ui-card"]) {{
    flex-wrap: wrap;
}}
@media (max-width: 1100px) {{
    [data-testid="stColumn"]:has([class*="st-key-ui-card"]) {{
        flex: 1 1 100% !important; width: 100% !important; min-width: 100% !important;
    }}
}}
@media (max-width: 700px) {{
    .ui-hero {{ padding: 0.9rem 1rem; }}
    [class*="st-key-ui-card"] {{ padding: 0.9rem 0.9rem 1rem; }}
    h1 {{ font-size: 2rem !important; }}
}}
/* Buttons keep their label on one line rather than breaking it word by word. */
[data-testid="stBaseButton-primary"] p, [data-testid="stBaseButton-secondary"] p {{
    white-space: nowrap;
}}
.ui-tag {{
    font-size: 0.7rem; font-weight: 700; letter-spacing: 0.03em; text-transform: uppercase;
    color: {ACCENT}; background: {ACCENT_SOFT}; padding: 0.1rem 0.45rem; border-radius: 4px;
}}
</style>
"""


def inject_styles():
    st.markdown(_CSS, unsafe_allow_html=True)


def card(name, tone=""):
    """A white panel. tone is "" (plain), "accent" (coloured top edge) or "muted"."""
    suffix = f"-{tone}" if tone else ""
    return st.container(key=f"ui-card{suffix}-{name}")


def card_header(title, subtitle=None, step=None):
    badge = f'<span class="ui-step">{step}</span>' if step is not None else ""
    sub = f'<p class="ui-card-sub">{escape(subtitle)}</p>' if subtitle else ""
    st.markdown(
        f'<div class="ui-card-title">{badge}{escape(title)}</div>{sub}',
        unsafe_allow_html=True,
    )


def steps(items):
    """Numbered instructions. items are plain strings (escaped)."""
    rows = "".join(
        f'<div class="ui-card-title"><span class="ui-step">{i}</span>{escape(text)}</div>'
        for i, text in enumerate(items, start=1)
    )
    st.markdown(f'<div class="ui-steps">{rows}</div>', unsafe_allow_html=True)


def section_label(text):
    st.markdown(f'<div class="ui-section">{escape(text)}</div>', unsafe_allow_html=True)


def pill(text, tone="neutral"):
    """HTML for a small status pill. tone: pass, warn, fail, info or neutral."""
    return f'<span class="ui-pill {tone}">{escape(text)}</span>'


def hero(title, text, pills=()):
    st.markdown(
        f'<div class="ui-hero"><div><h3>{escape(title)}</h3><p>{escape(text)}</p></div>'
        + (f'<div class="ui-pills">{"".join(pills)}</div>' if pills else "")
        + "</div>",
        unsafe_allow_html=True,
    )


def tile(title, rows, tone="neutral", badge="", note="", reason="", highlight=False):
    """HTML for one tile: a heading, label/value rows and optional footnotes.
    rows is a list of (label, value) pairs; values are escaped."""
    body = "".join(
        f'<div class="ui-tile-row"><span>{escape(label)}</span><b>{escape(value)}</b></div>'
        for label, value in rows
    )
    extra = ""
    if note:
        extra += f'<div class="ui-tile-note">{note}</div>'
    if reason:
        extra += f'<div class="ui-tile-reason">{escape(reason)}</div>'
    classes = f"ui-tile {tone}" + (" highlight" if highlight else "")
    return (
        f'<div class="{classes}"><div class="ui-tile-head">'
        f'<span class="ui-tile-freq">{escape(title)}</span>{badge}</div>{body}{extra}</div>'
    )


def tiles(items, columns=None):
    """A grid of tiles. By default as many per row as fit; columns=2 fixes two per
    row (e.g. four frequencies as 2 x 2 rather than 3 + 1)."""
    style = f' style="grid-template-columns: repeat({columns}, minmax(0, 1fr))"' if columns else ""
    st.markdown(
        f'<div class="ui-tiles"{style}>{"".join(items)}</div>', unsafe_allow_html=True
    )
