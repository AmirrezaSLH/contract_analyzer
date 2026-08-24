"""logger.py: JSON lines, context ids, spans -- and that nobody bypasses it."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from contract_analyzer import logger as L

SRC = Path(__file__).resolve().parents[1] / "src" / "contract_analyzer"


@pytest.fixture
def json_log(tmp_path):
    path = tmp_path / "app.jsonl"
    L.configure_logging("DEBUG", path, console=False, force=True)
    yield path
    L.configure_logging("INFO", None, console=False, force=True)


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_json_line_carries_extra_fields_and_trace_id(json_log):
    log = L.get_logger("test.module")
    with L.trace_context("abc123") as tid:
        log.info("hello", extra={"chunks": 3, "file": "x.pdf"})
    (rec,) = _lines(json_log)
    assert tid == "abc123"
    assert rec["msg"] == "hello"
    assert rec["logger"] == "contract_analyzer.test.module"
    assert rec["chunks"] == 3 and rec["file"] == "x.pdf"
    assert rec["trace_id"] == "abc123"
    assert rec["level"] == "INFO"
    assert re.match(r"\d{4}-\d{2}-\d{2}T", rec["ts"])


def test_trace_context_mints_an_id_and_nested_blocks_inherit_it():
    with L.trace_context() as outer:
        assert len(outer) == 32
        with L.trace_context() as inner:
            assert inner == outer
    assert L.current_trace_id() is None


def test_span_logs_start_and_end_with_latency_and_parent(json_log):
    with L.trace_context("t1"), L.span("outer", stage="parse") as s:
        s["pages"] = 21
        with L.span("inner"):
            pass
    recs = _lines(json_log)
    msgs = [(r["msg"], r["span"]) for r in recs]
    assert msgs == [
        ("span.start", "outer"),
        ("span.start", "inner"),
        ("span.end", "inner"),
        ("span.end", "outer"),
    ]
    outer_end = recs[-1]
    assert outer_end["status"] == "ok"
    assert outer_end["pages"] == 21 and outer_end["stage"] == "parse"
    assert outer_end["latency_ms"] >= 0
    inner_start = recs[1]
    assert inner_start["parent_span_id"] == recs[0]["span_id"]
    assert all(r["trace_id"] == "t1" for r in recs)


def test_span_records_errors_and_reraises(json_log):
    with pytest.raises(ValueError), L.span("boom"):
        raise ValueError("bad")
    end = _lines(json_log)[-1]
    assert end["status"] == "error" and end["error"] == "ValueError: bad"


def test_get_logger_namespaces_under_the_project_root():
    assert L.get_logger("x").name == "contract_analyzer.x"
    assert L.get_logger("contract_analyzer.parse").name == "contract_analyzer.parse"


def test_no_module_imports_logging_directly():
    """The whole point of logger.py: one surface, one format, one context."""
    offenders = [
        p.relative_to(SRC)
        for p in SRC.rglob("*.py")
        if p.name != "logger.py"
        and re.search(r"^\s*(import logging|from logging)", p.read_text(), re.M)
    ]
    assert not offenders, f"import logging directly: {offenders}"
