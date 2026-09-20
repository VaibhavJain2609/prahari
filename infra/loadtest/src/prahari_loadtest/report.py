"""Sample collection and run-directory output.

Every measurement lands in `runs/<timestamp>/samples.jsonl` as one JSON object
per line — `{"ts": ..., "metric": ..., "value": ..., ...}` — so a run is
replayable and auditable rather than reduced to a summary somebody trusts.
`summary.md` is the committable artifact; `runs/` itself is gitignored (raw
samples are large and machine-local), and summaries worth keeping are copied
into `docs/` or committed under a named directory deliberately.
"""

from __future__ import annotations

import json
import math
import statistics
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Samples:
    """In-memory sample sink; flushed to samples.jsonl at run end."""

    rows: list[dict[str, Any]] = field(default_factory=list)

    def add(self, metric: str, value: float, **meta: Any) -> None:
        self.rows.append({"ts": round(time.time(), 3), "metric": metric, "value": value, **meta})

    def by_metric(self, metric: str, phase: str | None = None) -> list[float]:
        return [
            r["value"]
            for r in self.rows
            if r["metric"] == metric and (phase is None or r.get("phase") == phase)
        ]

    def count(self, metric: str, phase: str | None = None) -> int:
        return len(self.by_metric(metric, phase))


def percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile — no interpolation, no dependency."""
    if not values:
        return None
    ordered = sorted(values)
    k = max(0, min(len(ordered) - 1, math.ceil(p / 100 * len(ordered)) - 1))
    return ordered[k]


def describe(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min": round(min(values), 3),
        "p50": round(percentile(values, 50) or 0, 3),
        "p95": round(percentile(values, 95) or 0, 3),
        "p99": round(percentile(values, 99) or 0, 3),
        "max": round(max(values), 3),
        "mean": round(statistics.fmean(values), 3),
    }


class RunDir:
    """`runs/<timestamp>-<label>/` — created lazily, always under the harness."""

    def __init__(self, base: Path, label: str = "") -> None:
        stamp = time.strftime("%Y%m%dT%H%M%S")
        suffix = f"-{label}" if label else ""
        self.path = base / f"{stamp}{suffix}"
        self.path.mkdir(parents=True, exist_ok=False)

    def write_config(self, config: dict[str, Any]) -> None:
        (self.path / "config.json").write_text(json.dumps(config, indent=2, default=str))

    def write_samples(self, samples: Samples) -> Path:
        out = self.path / "samples.jsonl"
        with out.open("w") as fh:
            for row in samples.rows:
                fh.write(json.dumps(row, default=str) + "\n")
        return out

    def write_summary(self, text: str) -> Path:
        out = self.path / "summary.md"
        out.write_text(text)
        return out
