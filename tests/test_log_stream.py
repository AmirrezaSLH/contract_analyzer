"""The live console hub: a logging handler that never blocks the run."""

from __future__ import annotations

from contract_analyzer.api.log_stream import LogStream
from contract_analyzer.logger import configure_logging, get_logger


def test_a_log_line_is_published_as_a_console_line():
    configure_logging("INFO", None, console=False, force=True)
    hub = LogStream().start()
    try:
        get_logger("probe").info("hello.there", extra={"chunks": 3})
        event = next(hub.subscribe())
        assert event.name == "log"
        assert event.data["level"] == "INFO"
        assert event.data["source"] == "api"
        assert "hello.there" in event.data["line"]
        assert "chunks=3" in event.data["line"]
    finally:
        hub.close()


def test_a_late_subscriber_gets_the_buffer_not_a_blank_screen():
    configure_logging("INFO", None, console=False, force=True)
    hub = LogStream(buffer=8).start()
    try:
        log = get_logger("probe")
        for n in range(3):
            log.info("counted", extra={"n": n})
        events = []
        for event in hub.subscribe():
            events.append(event)
            if len(events) == 3:
                break
        assert len(events) == 3
        assert all(
            e.name == "log" and e.data["source"] == "api" and "counted" in e.data["line"]
            for e in events
        )
    finally:
        hub.close()
