"""Host snapshots into `system_samples`, on a daemon thread of the API process.

This is the one Monitor capture that has no existing seam. Stages and (later)
upstream are queries over `spans`. HTTP live tiles will be an in-memory ring.
RAM and disk have to be sampled, or the chart is empty.

stdlib only: `VmRSS` and `MemTotal` from `/proc`, `shutil.disk_usage` of the
database directory. `ru_maxrss` is a high-water mark and is not what the tile
is asking. The thread is started from the API lifespan, not from
`MetricsStore.install()`, because `make analyze` on a laptop is not "is the
deployment healthy."
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from shutil import disk_usage
from typing import Any, Callable

from ..logger import get_logger

log = get_logger(__name__)

_STATUS = Path("/proc/self/status")
_MEMINFO = Path("/proc/meminfo")

_INSERT = """
INSERT OR REPLACE INTO system_samples (
    ts, rss_mb, rss_pct, disk_used_pct, disk_used_gb, disk_total_gb,
    http_rpm, http_5xx_rate, http_p95_ms
) VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
"""


def snapshot(*, db_path: Path) -> dict[str, Any]:
    """One reading. Never raises: a missing `/proc` still returns disk."""
    rss_kb = _proc_kb(_STATUS, "VmRSS")
    total_kb = _proc_kb(_MEMINFO, "MemTotal")
    rss_mb = None if rss_kb is None else round(rss_kb / 1024.0, 3)
    rss_pct = None
    if rss_kb is not None and total_kb:
        rss_pct = round(rss_kb / total_kb, 4)
    usage = disk_usage(db_path.parent)
    disk_pct = None if not usage.total else round(usage.used / usage.total, 4)
    return {
        "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
        "rss_mb": rss_mb,
        "rss_pct": rss_pct,
        "disk_used_pct": disk_pct,
        "disk_used_gb": round(usage.used / (1024 ** 3), 3),
        "disk_total_gb": round(usage.total / (1024 ** 3), 3),
    }


class HostSampler:
    """Writes `snapshot()` on a timer. Daemon: a missed close drops one tick."""

    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection],
        *,
        db_path: Path,
        interval: float = 30.0,
    ) -> None:
        self._connect = connect
        self._db_path = db_path
        self._interval = interval
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def start(self) -> HostSampler:
        if self._thread is not None:
            return self
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._run, name="monitor-sampler", daemon=True
        )
        self._thread.start()
        return self

    def close(self) -> None:
        thread, self._thread = self._thread, None
        if thread is None:
            return
        self._stopping.set()
        thread.join(timeout=2.0)

    def tick(self) -> dict[str, Any]:
        """Write one row now. Tests call this instead of waiting on the timer."""
        row = snapshot(db_path=self._db_path)
        conn = self._connect()
        try:
            conn.execute(
                _INSERT,
                (
                    row["ts"],
                    row["rss_mb"],
                    row["rss_pct"],
                    row["disk_used_pct"],
                    row["disk_used_gb"],
                    row["disk_total_gb"],
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return row

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - a sample must not take down the API
                log.warning("monitor.sample_failed", extra={"error": str(exc)})
            self._stopping.wait(self._interval)


def _proc_kb(path: Path, key: str) -> int | None:
    try:
        for line in path.read_text(encoding="ascii", errors="ignore").splitlines():
            if line.startswith(key):
                return int(line.split()[1])
    except (OSError, IndexError, ValueError):
        return None
    return None


__all__ = ["HostSampler", "snapshot"]
