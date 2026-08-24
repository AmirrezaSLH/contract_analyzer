"""Page chrome the views share: the title block, and escaping.

This is a separate module rather than a function in `app.py` for a reason that
is not style. `streamlit run` executes `app.py` as `__main__`, so a view doing
``from ..app import header`` would import `contract_analyzer.ui.app` as a
*second* module object -- re-running the script body, including the `main()`
call at the bottom, inside the first one. Anything a view needs from the shell
lives here, where importing it is just an import.
"""

from __future__ import annotations

import html

import streamlit as st

from . import theme


def escape(value: object) -> str:
    """Everything that reaches an HTML string goes through this.

    Filenames come from a multipart header and quotes come out of an uploaded
    PDF, so both are untrusted. Applying it to titles and meta lines too costs
    nothing and removes the question of which ones needed it.
    """
    return html.escape(str(value))


def header(title: str, meta: str, actions=None) -> None:
    """The page title and its one-line meta, with optional right-aligned
    actions -- **Export JSON** and **Re-run** are the only two."""
    left, right = st.columns([7, 3], vertical_alignment="bottom")
    with left:
        st.markdown(
            f"<div style='font-family:{theme.SERIF};font-size:30px;font-weight:600;"
            f"letter-spacing:-0.015em;color:{theme.INK}'>{escape(title)}</div>"
            f"<div style='font-family:{theme.SANS};font-size:13px;color:{theme.META};"
            f"margin-top:4px'>{escape(meta)}</div>",
            unsafe_allow_html=True,
        )
    if actions is not None:
        with right:
            actions()
    st.write("")


__all__ = ["escape", "header"]
