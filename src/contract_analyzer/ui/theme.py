"""The tokens, and the three fragments of HTML Streamlit has no primitive for.

Most of the design lives in ``.streamlit/config.toml`` -- the palette, both
type families, the radius -- so what is left here is small on purpose. Three
elements cannot be built from native components:

* **the state chip**, which must carry its words as well as its colour;
* **the sub-requirement marker**, four 11px squares whose *shape* is the
  distinction (a dashed outline for "we could not tell" must not read like a
  solid box for "met");
* **the quote card**, whose 3px left rule cannot come from
  ``st.container(border=True)`` -- that draws four uniform sides.

**Everything interpolated is escaped.** A quote's text was extracted from a PDF
a user uploaded, and a filename is whatever the client put in the multipart
header; both reach these builders. `html.escape` is applied at the boundary,
and no builder here formats a string it has not escaped. The colours and the
statuses are looked up in the tables below rather than interpolated, so a value
the API invents cannot become CSS.
"""

from __future__ import annotations

import html
from typing import Any

import streamlit as st

# -- tokens ----------------------------------------------------------------

INK = "#23201B"
INK_BODY = "#3A342B"
MUTED = "#6E665A"
META = "#7C7365"
LABEL = "#9A9082"
FAINT = "#A69C8C"
ACCENT = "#7A3B2E"
SURFACE = "#FFFFFF"
BORDER_CARD = "#E5DDD0"
BORDER_CONTROL = "#D6CDBD"
DIVIDER = "#EFE9DE"
QUOTE_RULE = "#C8A88C"

SERIF = "'Source Serif 4', Georgia, serif"
SANS = "'Source Sans 3', system-ui, sans-serif"

#: The three compliance states. The words are part of the chip, always: state
#: carried in colour alone is state a colourblind reviewer cannot read, and
#: this is the most important thing on the screen.
STATE_STYLE: dict[str, str] = {
    "Fully Compliant": "color:#2F6B4F;background:#EEF5F0;border:1px solid #B9D3C4;",
    "Partially Compliant": "color:#8A6108;background:#FBF3E3;border:1px solid #E4D0A6;",
    "Non-Compliant": "color:#8F2E2E;background:#FAEDEC;border:1px solid #E3BFBB;",
}
NEUTRAL_STYLE = f"color:{META};background:#F4F0E8;border:1px solid #E0D9CC;"

#: From `SubRequirementStatus`. `not_determined` is dashed on purpose -- "we
#: could not tell" must not read as "we checked and it is absent".
SUB_MARKER: dict[str, str] = {
    "met": "background:#2F6B4F;",
    "partial": (
        "background:linear-gradient(135deg,#A9720B 50%,#FFFFFF 50%);"
        "border:1px solid #A9720B;"
    ),
    "missing": "background:#FFFFFF;border:1.5px solid #8F2E2E;",
    "not_determined": "background:#FFFFFF;border:1.5px dashed #B3A896;",
}

#: Stroke SVG only, on a 24px grid. Eight in the whole design; keep it that way.
DOCUMENT_CHECK_ICON = (
    "<svg width='20' height='20' viewBox='0 0 24 24' fill='none' stroke='%237A3B2E' "
    "stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'>"
    "<path d='M14 3v5h5'/><path d='M19 9v10a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7z'/>"
    "<path d='m9 14 2 2 4-4'/></svg>"
)
ASSISTANT_AVATAR = f"data:image/svg+xml;utf8,{DOCUMENT_CHECK_ICON}"
USER_AVATAR = "data:image/svg+xml;utf8," + (
    "<svg width='20' height='20' viewBox='0 0 24 24' fill='none' stroke='%237A3B2E' "
    "stroke-width='1.6' stroke-linecap='round' stroke-linejoin='round'>"
    "<circle cx='12' cy='8' r='4'/><path d='M4 21a8 8 0 0 1 16 0'/></svg>"
)


# -- the CSS injection ------------------------------------------------------

#: Kept small on purpose: config.toml carries the palette and the fonts, so
#: this is only the handful of rules a theme setting cannot express. Each one
#: is here because a native component is a few pixels away from the design,
#: never because a component was avoided.
_CSS = f"""
<style>
/* Micro label: the 11px uppercase key above a value. */
.ca-lbl {{
  font-family: {SANS}; font-size: 11px; font-weight: 700; letter-spacing: .07em;
  text-transform: uppercase; color: {LABEL}; margin: 0 0 4px;
}}
.ca-chip {{
  display: inline-block; font-family: {SANS}; font-size: 12px; font-weight: 600;
  border-radius: 3px; padding: 4px 10px; white-space: nowrap;
}}
.ca-marker {{
  display: inline-block; width: 11px; height: 11px; border-radius: 2px;
  flex: 0 0 11px; margin-top: 4px;
}}
.ca-sub {{ display: flex; gap: 9px; align-items: flex-start; margin: 0 0 10px; }}
.ca-sub-text {{
  font-family: {SANS}; font-size: 13.5px; line-height: 1.5; color: {INK_BODY};
}}
/* The quote card. The left rule is the whole reason this is not a container. */
.ca-quote {{
  background: {SURFACE}; border: 1px solid {BORDER_CARD};
  border-left: 3px solid {QUOTE_RULE}; border-radius: 0 6px 6px 0;
  padding: 12px 14px; margin: 0 0 10px;
}}
.ca-quote-text {{
  font-family: {SERIF}; font-size: 15px; line-height: 1.55; color: {INK}; margin: 0;
}}
.ca-quote-meta {{
  font-family: {SANS}; font-size: 12px; color: {META}; margin: 7px 0 0;
}}
.ca-unverified {{ color: #8A6108; font-weight: 600; }}
.ca-meta {{ font-family: {SANS}; font-size: 13px; color: {META}; margin: 0; }}
/* The four-card strip under the drop zone, and the library's closing line. */
.ca-note {{
  font-family: {SANS}; font-size: 13px; line-height: 1.6; color: {MUTED};
  margin: 6px 0 0; max-width: 900px;
}}
.ca-rationale {{
  font-family: {SANS}; font-size: 14px; line-height: 1.65; color: {INK_BODY};
  max-width: 900px;
}}
/* Streamlit renders a metric value in the body font; the design sets metric
   values in the serif, and there is no theme token for that alone. */
[data-testid="stMetricValue"] {{ font-family: {SERIF}; font-weight: 600; }}
[data-testid="stMetricLabel"] p {{
  font-size: 11px; font-weight: 700; letter-spacing: .07em; text-transform: uppercase;
  color: {LABEL};
}}
/* The sidebar's document rows and the library's action buttons are tertiary
   buttons; left-aligning them is what makes them read as a list. */
[data-testid="stSidebar"] .stButton button {{ text-align: left; justify-content: flex-start; }}
</style>
"""

#: Loaded rather than bundled: both faces are Google Fonts, and the fallbacks
#: (`Georgia` and `system-ui`) are real, so a machine with no network gets a
#: readable page rather than a broken one.
_FONTS = (
    '<style>@import url("https://fonts.googleapis.com/css2?'
    "family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&"
    'family=Source+Sans+3:wght@400;500;600;700&display=swap");</style>'
)


def inject() -> None:
    """Once per run, at the top of `app.py`."""
    st.html(_FONTS + _CSS)


# -- the builders -----------------------------------------------------------


def state_chip(state: str) -> str:
    """A compliance state as a chip. The words are always in it."""
    style = STATE_STYLE.get(state, NEUTRAL_STYLE)
    return f'<span class="ca-chip" style="{style}">{html.escape(state)}</span>'


def last_analysis_chip(last: dict[str, Any] | None) -> str:
    """The library's "Last analysis" column, composed here rather than by the
    API -- `last_analysis.states` is a count per state precisely so that each
    client can choose its own words for it."""
    if not last:
        return f'<span class="ca-chip" style="{NEUTRAL_STYLE}">Not analysed</span>'
    status = last.get("status")
    if status != "done":
        label = {"queued": "Queued", "running": "Running", "cancelled": "Cancelled",
                 "failed": "Failed", "interrupted": "Interrupted"}.get(status, str(status))
        return f'<span class="ca-chip" style="{NEUTRAL_STYLE}">{html.escape(label)}</span>'
    states = last.get("states") or {}
    total = sum(states.values())
    compliant = states.get("Fully Compliant", 0)
    if total and compliant == total:
        return (
            f'<span class="ca-chip" style="{STATE_STYLE["Fully Compliant"]}">'
            f"{compliant} of {total} compliant</span>"
        )
    gaps = total - compliant
    style = STATE_STYLE["Non-Compliant" if states.get("Non-Compliant") else "Partially Compliant"]
    return (
        f'<span class="ca-chip" style="{style}">'
        f'{gaps} gap{"" if gaps == 1 else "s"} found</span>'
    )


def sub_marker(status: str, requirement: str) -> str:
    """One sub-requirement: its marker and its full text.

    The full text, not the id -- a reviewer needs to know what was checked, and
    `GOV-04` does not say.
    """
    style = SUB_MARKER.get(status, SUB_MARKER["not_determined"])
    return (
        f'<div class="ca-sub"><span class="ca-marker" style="{style}"></span>'
        f'<span class="ca-sub-text">{html.escape(requirement)}</span></div>'
    )


def quote_card(quote: dict[str, Any]) -> str:
    """One `ResolvedQuote` -- or one chat citation, which now has the same
    field names -- as the card the design specifies.

    A quote that failed verification is not rendered like one that passed. The
    API tells us which is which and the reviewer is the person who needs to
    know, so `verified: false` says so in words and in amber.
    """
    text = html.escape(str(quote.get("text", "")))
    ref = html.escape(str(quote.get("section_ref", "") or "—"))
    page = html.escape(str(quote.get("page_display", "") or "—"))
    verified = quote.get("verified", True)
    mark = (
        "verified" if verified
        else '<span class="ca-unverified">not found verbatim &mdash; check the source</span>'
    )
    return (
        f'<div class="ca-quote"><p class="ca-quote-text">&ldquo;{text}&rdquo;</p>'
        f'<p class="ca-quote-meta">&sect; {ref} &middot; p. {page} &middot; {mark}</p></div>'
    )


def label(text: str) -> str:
    """The 11px uppercase key above a value."""
    return f'<p class="ca-lbl">{html.escape(text)}</p>'


def meta(text: str) -> str:
    return f'<p class="ca-meta">{html.escape(text)}</p>'


def note(text: str) -> str:
    return f'<p class="ca-note">{html.escape(text)}</p>'


def usage_line(usage: dict[str, Any] | None, elapsed: float | None = None) -> str:
    """The caption under a chat answer: what it cost, and the promise that the
    quotes were checked."""
    parts: list[str] = []
    if elapsed is not None:
        parts.append(f"{elapsed:.1f} s")
    if usage:
        if usage.get("cost_usd") is not None:
            parts.append(f"${usage['cost_usd']:.3f}")
        calls = usage.get("tool_calls")
        if calls is not None:
            parts.append(f"{calls} tool call{'' if calls == 1 else 's'}")
        if usage.get("model"):
            parts.append(str(usage["model"]))
    parts.append("every quote checked against the source passage")
    return " · ".join(parts)


__all__ = [
    "ACCENT",
    "ASSISTANT_AVATAR",
    "STATE_STYLE",
    "SUB_MARKER",
    "USER_AVATAR",
    "inject",
    "label",
    "last_analysis_chip",
    "meta",
    "note",
    "quote_card",
    "state_chip",
    "sub_marker",
    "usage_line",
]
