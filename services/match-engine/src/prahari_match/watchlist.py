"""Load and index watchlist entries.

Every entry is normalised through `prahari_common.plates` on load -- the same
grammar the inference service applies to what OCR reads. If this module
normalised differently, `GJ 01 AB 1234` loaded here and `GJ01AB1234` reported
by inference would produce different skeletons, and stage 1 of the matcher
would reject a genuine hit before it ever reached scoring. That is the exact
silent failure the whole match engine exists to prevent.

Indexed by skeleton bucket, plus a precomputed single-deletion index so a
watchlist plate that is one character LONGER than what OCR read (a dropped
character) is still found in O(1) rather than by diffing the query against
every entry.
"""

from __future__ import annotations

import csv
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.protobuf.timestamp_pb2 import Timestamp
from prahari.v1 import events_pb2
from prahari_common.plates import NormalisedPlate, normalise_plate

from .confusion import skeleton

__all__ = ["Watchlist", "WatchlistRecord", "single_char_deletions"]

log = logging.getLogger(__name__)

_REASON_BY_NAME: dict[str, int] = {
    "stolen": events_pb2.WATCHLIST_REASON_STOLEN,
    "wanted": events_pb2.WATCHLIST_REASON_WANTED,
    "missing_person": events_pb2.WATCHLIST_REASON_MISSING_PERSON,
    "missing": events_pb2.WATCHLIST_REASON_MISSING_PERSON,
    "blacklisted": events_pb2.WATCHLIST_REASON_BLACKLISTED,
    "suspect": events_pb2.WATCHLIST_REASON_SUSPECT,
}


def single_char_deletions(text: str) -> set[str]:
    """Every string one character shorter than `text`, formed by removing
    exactly one position. Deduplicated -- `AA00` yields one variant for its
    repeated `A`, not two identical ones -- since the caller only cares which
    distinct strings result, not how many ways they were produced.

    Bounded: `len(text)` variants for a string of that length, never a scan of
    anything else. This is what makes stage 2 candidate generation O(len) per
    detection instead of O(watchlist size).
    """
    return {text[:i] + text[i + 1 :] for i in range(len(text))}


def _parse_reason(raw: str | None) -> int:
    if not raw:
        return events_pb2.WATCHLIST_REASON_UNSPECIFIED
    reason = _REASON_BY_NAME.get(raw.strip().lower())
    if reason is None:
        log.warning("unrecognised watchlist reason %r; storing UNSPECIFIED", raw)
        return events_pb2.WATCHLIST_REASON_UNSPECIFIED
    return reason


def _parse_timestamp(raw: str | None) -> Timestamp | None:
    if not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    ts = Timestamp()
    ts.FromDatetime(dt)
    return ts


@dataclass(frozen=True)
class WatchlistRecord:
    """One indexed watchlist entry. `entry.plate` is always the normalised
    text -- never the raw string a source system supplied -- so a consumer
    reading `matched_entry.plate` off an `Alert` sees the same grammar
    everywhere else in the system uses."""

    entry: events_pb2.WatchlistEntry
    normalised: NormalisedPlate
    skeleton: str


class Watchlist:
    def __init__(self) -> None:
        self._by_skeleton: dict[str, list[WatchlistRecord]] = defaultdict(list)
        self._by_deletion: dict[str, list[WatchlistRecord]] = defaultdict(list)
        self._by_entry_id: dict[str, WatchlistRecord] = {}

    # --- indexing --------------------------------------------------------

    def add(self, entry: events_pb2.WatchlistEntry) -> None:
        """Index one entry, in place. Idempotent on `entry_id`: loading the
        same file twice (e.g. a directory reload) must not double-count or
        double-index a plate."""
        if entry.entry_id in self._by_entry_id:
            return

        normalised = normalise_plate(entry.plate)
        stored = events_pb2.WatchlistEntry()
        stored.CopyFrom(entry)
        stored.plate = normalised.text  # never the raw, unnormalised source string

        skel = skeleton(normalised.text)
        record = WatchlistRecord(entry=stored, normalised=normalised, skeleton=skel)

        self._by_entry_id[entry.entry_id] = record
        self._by_skeleton[skel].append(record)
        for variant in single_char_deletions(skel):
            self._by_deletion[variant].append(record)

    def __len__(self) -> int:
        return len(self._by_entry_id)

    def skeletons(self) -> Iterable[str]:
        """Every distinct skeleton currently indexed."""
        return self._by_skeleton.keys()

    def bloom_keys(self) -> Iterable[str]:
        """Every string stage 1 must accept, so that its acceptance set is a
        SUPERSET of stage 2's candidate set.

        Building the filter from `skeletons()` alone is a silent false
        negative: stage 2 exists precisely to match an observation whose
        LENGTH differs from the watchlist entry, and such an observation's
        skeleton is by definition not among the full-length skeletons. The
        deletion variants are what let a plate read one character short --
        `GJ01AB134` for `GJ01AB1234`, a routine crop failure -- survive stage
        1 at all. Paired with the deletion-variant probes in
        `matcher.bloom_probes`, this closes both directions.
        """
        yield from self._by_skeleton
        yield from self._by_deletion

    def bucket_count(self) -> int:
        return len(self._by_skeleton)

    # --- lookup ------------------------------------------------------------

    def exact_skeleton(self, skel: str) -> list[WatchlistRecord]:
        return self._by_skeleton.get(skel, [])

    def by_deletion_variant(self, variant: str) -> list[WatchlistRecord]:
        """Entries whose OWN skeleton becomes `variant` when one character is
        removed -- i.e. entries one character LONGER than `variant`. Matches
        the case where OCR dropped a character the real plate has."""
        return self._by_deletion.get(variant, [])

    # --- loading -------------------------------------------------------------

    @classmethod
    def load_dir(cls, directory: Path) -> Watchlist:
        """Load every `.json` and `.csv` file directly inside `directory`.

        Missing directory is not an error -- a fresh checkout before the
        watchlist snapshot is captured, or a demo run with `data/` deliberately
        empty, must still start the service. `/readyz` is what surfaces an
        empty watchlist as a problem, not this constructor.
        """
        watchlist = cls()
        if not directory.is_dir():
            log.warning("watchlist directory %s not found; starting with 0 entries", directory)
            return watchlist

        for path in sorted(directory.iterdir()):
            suffix = path.suffix.lower()
            if suffix == ".json":
                watchlist.load_json(path)
            elif suffix == ".csv":
                watchlist.load_csv(path)
        return watchlist

    def load_json(self, path: Path) -> int:
        import json

        rows = json.loads(path.read_text(encoding="utf-8"))
        return self._load_rows(rows, source=str(path))

    def load_csv(self, path: Path) -> int:
        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        return self._load_rows(rows, source=str(path))

    def _load_rows(self, rows: list[dict[str, Any]], *, source: str) -> int:
        loaded = 0
        for row in rows:
            try:
                entry = self._entry_from_row(row)
            except (KeyError, ValueError) as exc:
                # One malformed row must not fail the whole file -- a watchlist
                # snapshot from an upstream system is exactly the kind of input
                # that has one bad line in it, and the other 999 are still
                # actionable.
                log.warning("skipping malformed watchlist row in %s: %s (%r)", source, exc, row)
                continue
            self.add(entry)
            loaded += 1
        log.info("loaded %d watchlist entries from %s", loaded, source)
        return loaded

    @staticmethod
    def _entry_from_row(row: dict[str, Any]) -> events_pb2.WatchlistEntry:
        plate = row.get("plate")
        if not plate:
            raise ValueError("row has no 'plate'")

        kwargs: dict[str, Any] = {
            "entry_id": row.get("entry_id") or row.get("id") or "",
            "plate": plate,
            "reason": _parse_reason(row.get("reason")),
            "case_reference": row.get("case_reference") or "",
            "source_system": row.get("source_system") or "",
        }
        if not kwargs["entry_id"]:
            raise ValueError("row has no 'entry_id'")

        added_at = _parse_timestamp(row.get("added_at"))
        if added_at is not None:
            kwargs["added_at"] = added_at
        expires_at = _parse_timestamp(row.get("expires_at"))
        if expires_at is not None:
            kwargs["expires_at"] = expires_at

        return events_pb2.WatchlistEntry(**kwargs)
