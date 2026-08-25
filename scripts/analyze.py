#!/usr/bin/env python3
"""Analyse one contract against the five compliance criteria.

    python scripts/analyze.py data/samples/"Sample Contract.pdf"
    python scripts/analyze.py --document-id 3 --criteria password_management
    make analyze F="data/samples/Sample Contract.pdf"

A path is ingested first (a second run of an unchanged file costs nothing --
`ingest_file` skips on the content hash); `--document-id` analyses something
already in the database. Progress is printed as it happens: every tool call the
model makes, with its arguments, and every verdict as it lands.

This is the same `analyze_document()` the API's job worker calls, with the same
arguments. That is the point of it: the API adds HTTP and a job id, and no
logic the command line does not already have -- including the metrics store,
which is built here too, so a run from the command line lands in `spans` and
on the KPI page.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:  # runnable without `pip install -e .`
    sys.path.insert(0, str(ROOT / "src"))

from contract_analyzer.compliance import get_criteria  # noqa: E402
from contract_analyzer.config import get_settings  # noqa: E402
from contract_analyzer.db import get_db  # noqa: E402
from contract_analyzer.documents import get_document, list_documents  # noqa: E402
from contract_analyzer.embeddings import get_embedder  # noqa: E402
from contract_analyzer.generation.client import AnswerUnavailable  # noqa: E402
from contract_analyzer.ingest.pipeline import ingest_file  # noqa: E402
from contract_analyzer.logger import configure_logging, trace_context  # noqa: E402
from contract_analyzer.metrics import MetricsStore  # noqa: E402
from contract_analyzer.report import AnalysisReport, analyze_document  # noqa: E402

STATE_WIDTH = 22


def on_event(event: dict) -> None:
    """Print the run as it happens. The `criterion` key is on every event --
    that is `analyze_document`'s doing, and it is what makes five parallel runs
    readable."""
    kind = event.get("type")
    where = event.get("criterion", "")
    if kind == "tool_call":
        args = " ".join(f"{k}={v!r}" for k, v in (event.get("args") or {}).items())
        print(
            f"  [{where}] {event.get('name')} {args} -> {event.get('returned')} chunks, "
            f"{event.get('new')} new"
            + (f" ERROR {event['error']}" if event.get("error") else ""),
            flush=True,
        )
    elif kind == "structure_errors":
        print(f"  [{where}] correction round {event.get('round')}: {event.get('errors')}",
              flush=True)
    elif kind == "result":
        print(f"  [{where}] {event.get('state')} confidence={event.get('confidence')}", flush=True)
    elif kind == "skipped":
        print(f"  [{where}] skipped (cancelled)", flush=True)


def summarise(report: AnalysisReport) -> None:
    print(f"\n=== {report.filename} ({report.status}) ===", flush=True)
    for result in report.results:
        print(
            f"{result.criterion_id:26} {result.compliance_state:{STATE_WIDTH}} "
            f"conf={result.confidence:.2f} review={int(result.needs_review)} "
            f"ended={result.ended_by:6} tools={result.tool_calls} "
            f"${result.cost_usd:.4f}",
            flush=True,
        )
    for skipped in report.skipped:
        print(f"{skipped:26} {'(skipped)':{STATE_WIDTH}}", flush=True)
    totals = report.totals
    print(
        f"\n{totals.criteria} criteria in {totals.job_duration_s:.1f}s for ${totals.cost_usd:.4f} "
        f"({totals.input_tokens + totals.output_tokens} tokens, {totals.tool_calls} tool calls); "
        f"mean confidence {totals.mean_confidence:.2f}, {totals.needs_review} need review",
        flush=True,
    )
    print(f"trace_id={report.trace_id}", flush=True)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("path", nargs="?", type=Path, help="a PDF to ingest and analyse")
    source.add_argument("--document-id", type=int, help="analyse a document already ingested")
    parser.add_argument(
        "--criteria", nargs="+", metavar="ID",
        help=f"criteria to run (default: all). One or more of: "
             f"{', '.join(c.id for c in get_criteria())}",
    )
    parser.add_argument("--workers", type=int, help="criteria in parallel (default: settings)")
    parser.add_argument("--out", type=Path, help="write the report JSON here")
    parser.add_argument("--reingest", action="store_true", help="re-ingest an unchanged file")
    parser.add_argument("--quiet", action="store_true", help="no per-event progress")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_file, console=False)

    conn = get_db(settings)
    embedder = get_embedder(settings)
    # The same store the API builds, wired the same way: installing it attaches
    # a handler to the project's root logger that files every `span.end` as a
    # row. That is the whole of the CLI's instrumentation -- a KPI page that
    # only saw HTTP traffic would be measuring the surface, not the system.
    metrics = MetricsStore(settings).install()
    try:
        return _run(args, settings, conn, embedder)
    finally:
        # Whatever happened, the spans of what did run are on disk before the
        # process exits. The writer is a daemon thread, so skipping this would
        # lose at most the last batch -- which is still the batch a reader is
        # about to go looking for.
        metrics.close()


def _run(args, settings, conn, embedder) -> int:
    with trace_context() as trace_id:
        print(f"trace_id={trace_id}  db={settings.db_path}", flush=True)

        if args.path is not None:
            result = ingest_file(args.path, conn, embedder, settings, force=args.reingest)
            if not result.ok or result.document_id is None:
                print(f"ingest failed: {result.error}", file=sys.stderr)
                return 1
            print(
                f"{result.status}: document_id={result.document_id} "
                f"pages={result.pages} chunks={result.chunks} "
                f"sections={result.spine_source} in {result.elapsed:.1f}s",
                flush=True,
            )
            document_id = result.document_id
        else:
            document_id = args.document_id
            if get_document(conn, document_id) is None:
                known = ", ".join(f"{d.document_id} ({d.filename})" for d in list_documents(conn))
                print(f"no document with id {document_id}. Ingested: {known or 'none'}",
                      file=sys.stderr)
                return 1

        try:
            report = analyze_document(
                document_id, conn, embedder, settings,
                criteria=args.criteria,
                on_event=None if args.quiet else on_event,
                workers=args.workers,
            )
        except AnswerUnavailable as exc:
            print(f"cannot analyse: {exc}", file=sys.stderr)
            return 1

    summarise(report)
    out = args.out or ROOT / ".run" / f"analysis-{report.analysis_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}", flush=True)
    return 0 if report.complete else 2


if __name__ == "__main__":
    raise SystemExit(main())
