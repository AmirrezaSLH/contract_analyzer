"""The five criteria as a job: submit, poll, read the report.

Four states, and this module renders all four: **no analysis yet**, **queued**,
**running**, **done**. Queued and running are the *same card*, mutated -- never
a different layout, so nothing jumps as the run progresses.

**The poll lives in a fragment.** `@st.fragment(run_every="2s")` re-runs that
block and nothing else. Without it a two-second poll re-runs the whole script
for three minutes: re-fetching the document list, re-rendering the transcript
in the other tab, and re-drawing the report as it arrives.

**Nothing here needs an event stream.** The `criteria` array on
`GET /analyses/{id}` is exactly the progress table the running view draws, so
the API's per-process SSE cut costs this UI nothing. Polling is the contract.
"""

from __future__ import annotations

import json

import streamlit as st

from .. import errors, state, theme
from ..client import ApiClient, ApiError, new_trace_id
from ..layout import escape, header

#: The four terminal statuses. Reached from `queued`/`running`, and the point
#: at which the fragment stops polling and the page draws the report.
TERMINAL = ("done", "failed", "cancelled", "interrupted")

COST_WARNING = (
    "A run answers all five compliance questions against this contract alone. It takes "
    "about a minute and costs roughly a dollar, so it is never started for you."
)


def render(api: ApiClient) -> None:
    document = st.session_state.document
    if document is None:
        return
    document_id = document["document_id"]

    current = _current(api, document, document_id)
    if isinstance(current, ApiError):
        header(document["filename"], "")
        errors.show(current, context="This analysis could not be read")
        return

    if current is None:
        header(
            document["filename"],
            f"{document.get('pages') or '?'} pages · {document.get('chunks', 0)} passages · "
            "not analysed yet",
        )
        _empty_state(api, document)
        return

    status = current["status"]
    if status in ("queued", "running"):
        header(
            document["filename"],
            f"Analysis {current['analysis_id']} · {status}"
            + (f" · started {_clock(current['started_at'])}" if current.get("started_at") else ""),
        )
        _live(api, current["analysis_id"])
        return

    header(
        document["filename"],
        _done_meta(current),
        actions=lambda: _done_actions(api, document, current),
    )
    if status == "done":
        _report(current)
    else:
        _not_done(api, document, current)


# -- which analysis is on screen -------------------------------------------


def _current(api: ApiClient, document: dict, document_id: int):
    """The analysis this view is about, or None if there is none.

    Preference order: the run this session started, then the newest one the
    server knows about. The second half is what makes a report produced by
    `make analyze` -- or by another browser tab -- show up here: the record is
    durable, so "my session started it" is a convenience, not a requirement.
    """
    watched = state.analysis_id(document_id)
    try:
        if watched:
            return api.analysis(watched)
        last = document.get("last_analysis")
        if last:
            state.set_analysis(document_id, last["analysis_id"])
            return api.analysis(last["analysis_id"])
    except ApiError as exc:
        if exc.code == "analysis_not_found":
            # Watched an analysis that is gone. Forget it and offer a new run
            # rather than showing a dead id.
            state.set_analysis(document_id, None)
            return None
        return exc
    return None


# -- state a: nothing has been run -----------------------------------------


def _empty_state(api: ApiClient, document: dict) -> None:
    with st.container(border=True):
        st.markdown(
            f"<div style='text-align:center;padding:38px 12px 6px'>"
            f"<div style='font-family:{theme.SERIF};font-size:19px;font-weight:600;"
            f"color:{theme.INK}'>{escape(document['filename'])} has not been "
            "analysed yet</div>"
            f"<div style='font-family:{theme.SANS};font-size:14px;line-height:1.6;"
            f"color:{theme.MUTED};max-width:560px;margin:10px auto 0'>{COST_WARNING}</div>"
            "</div>",
            unsafe_allow_html=True,
        )
        _, middle, _ = st.columns([2, 2, 2])
        with middle:
            _run_button(api, document, label="Run compliance analysis")


def _run_button(api: ApiClient, document: dict, *, label: str, key: str = "run") -> None:
    """Submit a run. Disabled without an answer key, and says why.

    Disabled rather than allowed-and-refused: `/health` reports `key_present`
    exactly so a UI can grey the button out instead of spending a click to
    discover a 503.
    """
    key_present = (st.session_state.health or {}).get("key_present", True)
    if st.button(
        label,
        type="primary",
        key=key,
        use_container_width=True,
        disabled=not key_present,
        help=None if key_present else "ANTHROPIC_API_KEY is not set on the API.",
    ):
        _submit(api, document)


def _submit(api: ApiClient, document: dict, *, force: bool = False) -> None:
    document_id = document["document_id"]
    trace_id = new_trace_id()
    st.session_state.trace_id = trace_id
    try:
        # A 202 and a 200 carrying an already-running analysis are the same
        # thing here: take `analysis_id` from the body either way. The 200 is
        # the duplicate-submit guard saving a dollar, not an error.
        # `Idempotency-Key` is what a deliberate re-run sends to override it.
        response = api.create_analysis(
            document_id,
            trace_id=trace_id,
            idempotency_key=trace_id if force else None,
        )
    except ApiError as exc:
        errors.stash(exc)
    else:
        state.set_analysis(document_id, response["analysis_id"])
        st.session_state.open_criterion = None
    st.rerun()


# -- states b and c: queued and running ------------------------------------


def _live(api: ApiClient, analysis_id: str) -> None:
    """The progress card, re-rendered on its own every two seconds."""

    @st.fragment(run_every="2s")
    def block() -> None:
        try:
            current = api.analysis(analysis_id)
        except ApiError as exc:
            errors.show(exc, context="This analysis could not be read")
            return
        _progress_card(api, current)
        if current["status"] in TERMINAL:
            # Out of the fragment and into a whole-page run, which is what
            # draws the report: a fragment can only redraw itself.
            st.rerun()

    block()


def _progress_card(api: ApiClient, current: dict) -> None:
    running = current["status"] == "running"
    progress = current.get("progress") or {}
    done, total = progress.get("done", 0), progress.get("total", 5) or 5

    with st.container(border=True):
        left, right = st.columns([7, 2], vertical_alignment="center")
        with left:
            st.markdown(
                f"<div style='display:flex;align-items:center;gap:9px'>"
                f"<span style='width:9px;height:9px;border-radius:50%;"
                f"background:{'#2F6B4F' if running else '#C9A227'}'></span>"
                f"<span style='font-family:{theme.SERIF};font-size:19px;font-weight:600;"
                f"color:{theme.INK}'>"
                f"{'Analysing five criteria' if running else 'Queued'}</span>"
                f"<span style='font-family:{theme.SANS};font-size:13px;color:{theme.META}'>"
                f"{_sub_line(current, running)}</span></div>",
                unsafe_allow_html=True,
            )
        with right:
            if st.button("Cancel", use_container_width=True, key="cancel"):
                _cancel(api, current["analysis_id"])

        st.progress(done / total if total else 0.0)
        # The stage line is the one place a raw criterion id is shown to a
        # user: it is the honest name of the thing in flight.
        st.caption(current.get("stage") or _waiting_line(running))

        st.divider()
        for index, criterion in enumerate(current.get("criteria") or [], start=1):
            _progress_row(index, criterion)

        st.divider()
        _footer(current)


def _waiting_line(running: bool) -> str:
    if running:
        return ""
    jobs = (st.session_state.health or {}).get("api_workers")
    return (
        f"Waiting for a worker — {jobs} analyses run at a time" if jobs
        else "Waiting for a worker"
    )


def _sub_line(current: dict, running: bool) -> str:
    if running:
        return f"started {_clock(current.get('started_at'))}"
    return "waiting for a free worker"


def _progress_row(index: int, criterion: dict) -> None:
    status = criterion.get("status", "queued")
    state_label = criterion.get("state")
    done = status == "done"
    active = status == "running"
    dot = (
        "background:#2F6B4F" if done
        else f"background:{theme.ACCENT}" if active
        else "background:#FFFFFF;border:1.5px solid #DDD5C6"
    )
    name_colour = theme.INK if (done or active) else theme.FAINT
    label = (
        state_label if done and state_label
        else "retrieving…" if active
        else "skipped" if status == "skipped"
        else "failed" if status == "failed"
        else "waiting"
    )
    confidence = criterion.get("confidence")

    columns = st.columns([6, 3, 1.4], vertical_alignment="center")
    columns[0].markdown(
        f"<div style='display:flex;align-items:center;gap:9px'>"
        f"<span style='width:9px;height:9px;border-radius:50%;{dot}'></span>"
        f"<span style='font-family:{theme.SERIF};font-size:15px;font-weight:600;"
        f"color:{name_colour}'>{index} · {_title(criterion['id'])}</span></div>",
        unsafe_allow_html=True,
    )
    columns[1].markdown(
        f"<span style='font-family:{theme.SANS};font-size:13px;"
        f"color:{'#2F6B4F' if done else theme.FAINT};"
        f"font-weight:{600 if done else 400}'>{escape(str(label))}</span>",
        unsafe_allow_html=True,
    )
    columns[2].markdown(
        theme.meta(f"{confidence:.2f}" if confidence is not None else "—"),
        unsafe_allow_html=True,
    )


def _footer(current: dict) -> None:
    """Elapsed, cost so far, the pool shape, and the trace id.

    The pool numbers come from `/health` rather than from a constant here: they
    are `api_workers` and `analysis_workers`, and a UI that states the shape of
    a pool it has hardcoded will state it wrongly the first time someone tunes
    it. The trace id is always on screen -- it is what makes the walkthrough of
    `.run/app.jsonl` possible.
    """
    totals = current.get("totals") or {}
    health = st.session_state.health or {}
    parts = [
        f"elapsed {_elapsed(current)}",
        f"cost so far ${totals.get('cost_usd', 0):.2f}",
    ]
    jobs, criteria = health.get("api_workers"), health.get("analysis_workers")
    if jobs and criteria:
        parts.append(f"{jobs} workers · {criteria} criteria in parallel")
    if current.get("trace_id"):
        parts.append(f"trace {current['trace_id']}")
    st.markdown(theme.meta(" · ".join(parts)), unsafe_allow_html=True)


def _cancel(api: ApiClient, analysis_id: str) -> None:
    """Skip whatever has not started.

    Honestly named in the copy: with five workers and five criteria all five
    start at once and there is nothing left to skip, so cancel only stops a job
    still waiting for a worker. Stopping a *running* criterion would mean
    threading the flag into the agent loop, which belongs to the library.
    """
    try:
        api.cancel_analysis(analysis_id, trace_id=st.session_state.trace_id)
    except ApiError as exc:
        errors.stash(exc)
    st.rerun()


# -- state d: done ----------------------------------------------------------


def _done_meta(current: dict) -> str:
    totals = current.get("totals") or {}
    when = _clock(current.get("completed_at"), date=True)
    parts = [f"Analysed {when}"] if when != "—" else [current["status"].title()]
    if totals:
        parts += [
            f"{totals.get('criteria', 0)} criteria",
            f"{totals.get('latency_s', 0):.1f} s",
            f"${totals.get('cost_usd', 0):.2f}",
        ]
    return " · ".join(parts)


def export_payload(current: dict) -> str:
    """The report as the download carries it: **verbatim**.

    A function rather than an expression inline, so it can be asserted against
    `AnalysisReport` in a test -- Streamlit's download button does not put its
    payload anywhere a headless run can read it. Verbatim matters: this is the
    same object `scripts/analyze.py --out` writes, so what a reviewer downloads
    validates as a report rather than as a UI's idea of one.
    """
    report = current.get("report")
    return json.dumps(report, indent=2) if report else "{}"


def _done_actions(api: ApiClient, document: dict, current: dict) -> None:
    left, right = st.columns(2)
    report = current.get("report")
    with left:
        st.download_button(
            "Export JSON",
            data=export_payload(current),
            file_name=f"analysis-{current['analysis_id']}.json",
            mime="application/json",
            use_container_width=True,
            disabled=report is None,
        )
    with right:
        if st.button("Re-run", use_container_width=True, key="rerun"):
            # `force`: a re-run is a caller who genuinely wants a second
            # opinion, which is exactly what `Idempotency-Key` overrides the
            # duplicate-submit guard for. The older run is kept.
            _submit(api, document, force=True)


def _not_done(api: ApiClient, document: dict, current: dict) -> None:
    """`failed`, `cancelled` or `interrupted`.

    Three different sentences, because they are three different events: the
    model refused, a person stopped it, or the machine went away. A cancelled
    run still has a partial report and it is shown.
    """
    status = current["status"]
    if status == "failed":
        # `error` is a string from the runner. Rendering it beats a generic
        # message: it is the only description of what actually happened.
        st.error(
            f"**This analysis failed.**\n\n{current.get('error') or 'No reason was recorded.'}"
            "\n\nRun it again, or check the API's log for this trace id.",
            icon=":material/error:",
        )
    elif status == "interrupted":
        st.warning(
            "**This analysis was interrupted.** The worker running it went away before it "
            "finished — nothing refused, the process stopped. Run it again.",
            icon=":material/error:",
        )
    else:
        st.info(
            "**This analysis was cancelled.** Criteria that had not started were skipped; "
            "what finished is below.",
            icon=":material/info:",
        )
    left, _ = st.columns([2, 6])
    with left:
        _run_button(api, document, label="Run it again", key="rerun-failed")
    if current.get("report"):
        st.write("")
        _report(current)


def _report(current: dict) -> None:
    report = current.get("report")
    if not report:
        st.info("This run produced no report.", icon=":material/info:")
        return
    results = report.get("results") or []
    _tiles(report, results)
    st.write("")
    for index, result in enumerate(results, start=1):
        _criterion_row(index, result)
    skipped = report.get("skipped") or []
    if skipped:
        st.caption(f"Skipped: {', '.join(_title(s) for s in skipped)}")


def _tiles(report: dict, results: list[dict]) -> None:
    """Four numbers, three of them computed here.

    `Overall`, `Quotes verified` and `Needs review` are walked out of
    `report.results` rather than read off `totals`: they are presentation
    decisions -- the worst state across five, and two counts -- not backend
    facts. If `totals` ever grows them, prefer those.
    """
    order = ["Non-Compliant", "Partially Compliant", "Fully Compliant"]
    states = [r.get("compliance_state") for r in results]
    overall = next((s for s in order if s in states), "—")
    quotes = [q for r in results for q in (r.get("relevant_quotes") or [])]
    verified = sum(1 for q in quotes if q.get("verified"))
    needs_review = sum(1 for r in results if r.get("needs_review"))
    totals = report.get("totals") or {}

    tiles = st.columns(4)
    with tiles[0]:
        st.markdown(theme.label("Overall"), unsafe_allow_html=True)
        st.markdown(theme.state_chip(overall), unsafe_allow_html=True)
    tiles[1].metric("Mean confidence", f"{totals.get('mean_confidence', 0):.2f}")
    tiles[2].metric("Quotes verified", f"{verified} / {len(quotes)}")
    tiles[3].metric("Needs review", needs_review)


def _criterion_row(index: int, result: dict) -> None:
    """One criterion, collapsed by default except the first.

    `st.expander`'s label is a single markdown string and cannot hold a
    right-aligned cluster, so the state travels in the label as a badge and the
    counts move to the first caption inside. The information survives; the
    alignment does not. Building the row by hand would buy the alignment for
    about thirty lines, and is the fallback if the collapsed row turns out to
    be hard to scan.
    """
    criterion_id = result["criterion_id"]
    subs = result.get("sub_requirements") or []
    met = sum(1 for s in subs if s.get("status") == "met")
    state_label = result.get("compliance_state", "")
    colour = {"Fully Compliant": "green", "Partially Compliant": "orange"}.get(state_label, "red")

    if st.session_state.open_criterion is None and index == 1:
        st.session_state.open_criterion = criterion_id

    # `compliance_requirement` is the criterion's title and travels with the
    # result, so a stored report renders correctly even if `/criteria` was
    # unreachable at boot.
    title = result.get("compliance_requirement") or _title(criterion_id)
    with st.expander(
        f"**{index} · {title}**  :{colour}-badge[{state_label}]",
        expanded=st.session_state.open_criterion == criterion_id,
    ):
        st.caption(
            f"{met} of {len(subs)} met · conf {result.get('confidence', 0):.2f}"
            + ("  ·  ⚠ needs review" if result.get("needs_review") else "")
        )

        st.markdown(theme.label("Sub-requirements"), unsafe_allow_html=True)
        columns = st.columns(2)
        for position, sub in enumerate(subs):
            with columns[position % 2]:
                st.markdown(
                    theme.sub_marker(
                        sub.get("status", "not_determined"), sub.get("requirement", "")
                    ),
                    unsafe_allow_html=True,
                )

        quotes = result.get("relevant_quotes") or []
        if quotes:
            st.markdown(theme.label("Relevant quotes"), unsafe_allow_html=True)
            unverified = sum(1 for q in quotes if not q.get("verified"))
            st.caption(
                f"showing {min(2, len(quotes))} of {len(quotes)} — "
                + ("all verified verbatim" if not unverified
                   else f"{unverified} could not be matched to its passage")
            )
            show_all = st.toggle(
                f"Show all {len(quotes)} quotes", key=f"q-{criterion_id}", value=False
            )
            for quote in quotes if show_all else quotes[:2]:
                st.html(theme.quote_card(quote))

        st.markdown(theme.label("Rationale"), unsafe_allow_html=True)
        st.markdown(
            f"<div class='ca-rationale'>{escape(result.get('rationale', ''))}</div>",
            unsafe_allow_html=True,
        )

        st.divider()
        # No latency here: `ComplianceResult` does not carry a per-criterion
        # duration -- `totals.latency_s` is the run's, and inventing a share of
        # it per criterion would be a number that looks measured and is not.
        usage = result.get("usage") or {}
        footer = [
            f"${result.get('cost_usd', 0):.3f}",
            f"{result.get('tool_calls', 0)} tool calls",
            f"{usage.get('input_tokens', 0) + usage.get('output_tokens', 0)} tokens",
            f"ended by {result.get('ended_by', '—')}",
        ]
        if result.get("structure_rounds"):
            footer.append(f"{result['structure_rounds']} correction rounds")
        for problem in result.get("unresolved_errors") or []:
            st.caption(f"⚠ {problem}")
        st.markdown(theme.meta(" · ".join(footer)), unsafe_allow_html=True)


# -- small helpers ----------------------------------------------------------


def _title(criterion_id: str) -> str:
    """The criterion's real name, from `GET /criteria`.

    A raw criterion id is not customer-facing copy -- except in the running
    stage line, where it is the honest name of the thing in flight. The
    fallback prettifies the id, which mangles an acronym (`it_asset_management`
    becomes "It Asset Management") and is why it is only a fallback.
    """
    titles = st.session_state.get("criteria_titles") or {}
    return titles.get(criterion_id) or str(criterion_id).replace("_", " ").title()


def _clock(timestamp: str | None, *, date: bool = False) -> str:
    from datetime import datetime

    if not timestamp:
        return "—"
    try:
        moment = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return str(timestamp)
    return moment.strftime("%d %b, %H:%M" if date else "%H:%M:%S")


def _elapsed(current: dict) -> str:
    from datetime import UTC, datetime

    started = current.get("started_at") or current.get("created_at")
    if not started:
        return "0 s"
    try:
        moment = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
    except ValueError:
        return "0 s"
    seconds = int((datetime.now(UTC) - moment).total_seconds())
    return f"{seconds} s" if seconds < 60 else f"{seconds // 60} m {seconds % 60} s"
