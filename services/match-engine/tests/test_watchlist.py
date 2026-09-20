"""watchlist.py: loading and indexing. If a watchlist plate is normalised or
indexed differently here than the way inference reports plates, a genuine hit
misses silently -- so these tests lean on the real `normalise_plate` rather
than mocking it, the same way `matcher.py` does at runtime.
"""

from __future__ import annotations

import json
from pathlib import Path

from prahari_match.watchlist import Watchlist, single_char_deletions


class TestSingleCharDeletions:
    def test_produces_one_variant_per_position(self) -> None:
        assert single_char_deletions("ABC") == {"BC", "AC", "AB"}

    def test_deduplicates_identical_results(self) -> None:
        # Removing either "A" from "AA00" yields the same string -- one
        # variant, not two, since callers only care which strings result.
        assert single_char_deletions("AA00") == {"A00", "AA0"}

    def test_empty_string_has_no_deletions(self) -> None:
        assert single_char_deletions("") == set()

    def test_single_character_deletes_to_empty(self) -> None:
        assert single_char_deletions("A") == {""}


class TestLoading:
    def test_missing_directory_starts_empty_not_an_error(self, tmp_path: Path) -> None:
        watchlist = Watchlist.load_dir(tmp_path / "does-not-exist")
        assert len(watchlist) == 0

    def test_loads_json_file(self, tmp_path: Path) -> None:
        (tmp_path / "wl.json").write_text(
            json.dumps(
                [
                    {
                        "entry_id": "E1",
                        "plate": "GJ01AB1234",
                        "reason": "stolen",
                        "case_reference": "FIR/1",
                        "source_system": "manual",
                    }
                ]
            ),
            encoding="utf-8",
        )
        watchlist = Watchlist.load_dir(tmp_path)
        assert len(watchlist) == 1

    def test_loads_csv_file(self, tmp_path: Path) -> None:
        (tmp_path / "wl.csv").write_text(
            "entry_id,plate,reason,case_reference,source_system,added_at,expires_at\n"
            "E2,GJ05CD5678,wanted,FIR/2,manual,,\n",
            encoding="utf-8",
        )
        watchlist = Watchlist.load_dir(tmp_path)
        assert len(watchlist) == 1

    def test_loads_json_and_csv_from_the_same_directory(self, tmp_path: Path) -> None:
        (tmp_path / "a.json").write_text(
            json.dumps([{"entry_id": "E1", "plate": "GJ01AB1234", "reason": "stolen"}]),
            encoding="utf-8",
        )
        (tmp_path / "b.csv").write_text(
            "entry_id,plate,reason\nE2,GJ05CD5678,wanted\n", encoding="utf-8"
        )
        watchlist = Watchlist.load_dir(tmp_path)
        assert len(watchlist) == 2

    def test_malformed_row_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        (tmp_path / "wl.json").write_text(
            json.dumps(
                [
                    {"entry_id": "E1", "plate": "GJ01AB1234", "reason": "stolen"},
                    {"reason": "wanted"},  # missing plate -- malformed
                    {"entry_id": "E3", "plate": "GJ05CD5678", "reason": "wanted"},
                ]
            ),
            encoding="utf-8",
        )
        watchlist = Watchlist.load_dir(tmp_path)
        assert len(watchlist) == 2

    def test_duplicate_entry_id_is_idempotent(self, tmp_path: Path) -> None:
        (tmp_path / "wl.json").write_text(
            json.dumps(
                [
                    {"entry_id": "E1", "plate": "GJ01AB1234", "reason": "stolen"},
                    {"entry_id": "E1", "plate": "GJ01AB1234", "reason": "stolen"},
                ]
            ),
            encoding="utf-8",
        )
        watchlist = Watchlist.load_dir(tmp_path)
        assert len(watchlist) == 1

    def test_plate_is_stored_normalised_not_raw(self, tmp_path: Path) -> None:
        (tmp_path / "wl.json").write_text(
            json.dumps([{"entry_id": "E1", "plate": "gj-01-ab-1234", "reason": "stolen"}]),
            encoding="utf-8",
        )
        watchlist = Watchlist.load_dir(tmp_path)
        skel = next(iter(watchlist.skeletons()))
        record = watchlist.exact_skeleton(skel)[0]
        assert record.entry.plate == "GJ01AB1234"

    def test_unrecognised_reason_does_not_crash_load(self, tmp_path: Path) -> None:
        (tmp_path / "wl.json").write_text(
            json.dumps([{"entry_id": "E1", "plate": "GJ01AB1234", "reason": "not-a-real-reason"}]),
            encoding="utf-8",
        )
        watchlist = Watchlist.load_dir(tmp_path)
        assert len(watchlist) == 1

    def test_missing_reason_stores_unspecified(self, tmp_path: Path) -> None:
        from prahari.v1 import events_pb2

        (tmp_path / "wl.json").write_text(
            json.dumps([{"entry_id": "E1", "plate": "GJ01AB1234"}]), encoding="utf-8"
        )
        watchlist = Watchlist.load_dir(tmp_path)
        record = next(iter(watchlist._by_entry_id.values()))  # noqa: SLF001
        assert record.entry.reason == events_pb2.WATCHLIST_REASON_UNSPECIFIED

    def test_row_without_any_id_is_skipped_not_fatal(self, tmp_path: Path) -> None:
        # `_entry_from_row` raises ValueError for a missing entry_id; the
        # loader must skip the row and keep the rest — same malformed-row
        # contract as a missing plate.
        (tmp_path / "wl.json").write_text(
            json.dumps(
                [
                    {"plate": "GJ01AB1234", "reason": "stolen"},  # no entry_id or id
                    {"entry_id": "E2", "plate": "GJ05CD5678", "reason": "wanted"},
                ]
            ),
            encoding="utf-8",
        )
        watchlist = Watchlist.load_dir(tmp_path)
        assert len(watchlist) == 1

    def test_id_column_is_accepted_as_entry_id(self, tmp_path: Path) -> None:
        # Some upstream snapshots key the row "id" rather than "entry_id" —
        # the loader accepts either, and the alias must map to entry_id.
        (tmp_path / "wl.csv").write_text(
            "id,plate,reason\nW-7,GJ05CD5678,wanted\n", encoding="utf-8"
        )
        watchlist = Watchlist.load_dir(tmp_path)
        assert len(watchlist) == 1
        record = next(iter(watchlist._by_entry_id.values()))  # noqa: SLF001
        assert record.entry.entry_id == "W-7"

    def test_reason_aliases_resolve_to_their_proto_values(self, tmp_path: Path) -> None:
        # "missing" is the documented alias for missing_person — a snapshot
        # that spells it the short way must not land as UNSPECIFIED.
        from prahari.v1 import events_pb2

        (tmp_path / "wl.json").write_text(
            json.dumps(
                [
                    {"entry_id": "E1", "plate": "GJ01AB1234", "reason": "missing"},
                    {"entry_id": "E2", "plate": "GJ05CD5678", "reason": "suspect"},
                ]
            ),
            encoding="utf-8",
        )
        watchlist = Watchlist.load_dir(tmp_path)
        by_id = watchlist._by_entry_id  # noqa: SLF001
        assert by_id["E1"].entry.reason == events_pb2.WATCHLIST_REASON_MISSING_PERSON
        assert by_id["E2"].entry.reason == events_pb2.WATCHLIST_REASON_SUSPECT

    def test_added_at_and_expires_at_parse_to_proto_timestamps(self, tmp_path: Path) -> None:
        # expires_at is what matcher._expired reads — a parse that silently
        # dropped it would keep expired entries matching forever.
        from datetime import UTC, datetime

        (tmp_path / "wl.json").write_text(
            json.dumps(
                [
                    {
                        "entry_id": "E1",
                        "plate": "GJ01AB1234",
                        "reason": "stolen",
                        "added_at": "2026-01-01T00:00:00+00:00",
                        # Naive timestamp: treated as UTC, per _parse_timestamp.
                        "expires_at": "2027-01-01T00:00:00",
                    }
                ]
            ),
            encoding="utf-8",
        )
        watchlist = Watchlist.load_dir(tmp_path)
        entry = next(iter(watchlist._by_entry_id.values())).entry  # noqa: SLF001
        assert entry.HasField("added_at")
        assert entry.HasField("expires_at")
        assert entry.added_at.ToDatetime(tzinfo=UTC) == datetime(2026, 1, 1, tzinfo=UTC)
        assert entry.expires_at.ToDatetime(tzinfo=UTC) == datetime(2027, 1, 1, tzinfo=UTC)

    def test_blank_timestamps_leave_the_proto_fields_unset(self, tmp_path: Path) -> None:
        (tmp_path / "wl.csv").write_text(
            "entry_id,plate,reason,added_at,expires_at\nE1,GJ01AB1234,stolen, , \n",
            encoding="utf-8",
        )
        watchlist = Watchlist.load_dir(tmp_path)
        entry = next(iter(watchlist._by_entry_id.values())).entry  # noqa: SLF001
        assert not entry.HasField("added_at")
        assert not entry.HasField("expires_at")


class TestLookup:
    def _watchlist_with(self, *plates: str) -> Watchlist:
        from prahari.v1 import events_pb2

        watchlist = Watchlist()
        for i, plate in enumerate(plates):
            watchlist.add(events_pb2.WatchlistEntry(entry_id=f"E{i}", plate=plate))
        return watchlist

    def test_exact_skeleton_finds_its_own_entry(self) -> None:
        watchlist = self._watchlist_with("GJ01AB1234")
        skel = next(iter(watchlist.skeletons()))
        records = watchlist.exact_skeleton(skel)
        assert len(records) == 1
        assert records[0].entry.plate == "GJ01AB1234"

    def test_exact_skeleton_misses_a_different_plate(self) -> None:
        watchlist = self._watchlist_with("GJ01AB1234")
        assert watchlist.exact_skeleton("NOTAPLATE") == []

    def test_deletion_variant_finds_an_entry_one_character_longer(self) -> None:
        # OCR dropped a character: the observed skeleton is one character
        # SHORTER than the watchlist entry it should still find.
        watchlist = self._watchlist_with("GJ01AB1234")
        entry_skeleton = next(iter(watchlist.skeletons()))
        observed_short = entry_skeleton[:-1]  # drop the trailing "4"
        records = watchlist.by_deletion_variant(observed_short)
        assert any(r.entry.plate == "GJ01AB1234" for r in records)

    def test_bucket_count_matches_distinct_skeletons(self) -> None:
        # Two entries sharing a skeleton (e.g. an OCR-confusable pair of real
        # plates) must count as one bucket, not two.
        watchlist = self._watchlist_with("GJ01AB1234", "GJ01AB1Z34")
        assert watchlist.bucket_count() == 1
        assert len(watchlist) == 2

    def test_bloom_keys_are_a_superset_of_both_lookup_indexes(self) -> None:
        # Stage 1's acceptance set must be a superset of stage 2's candidate
        # set: the full skeletons AND every single-deletion variant, or a
        # plate read one character short dies in the Bloom filter before the
        # matcher ever sees it.
        watchlist = self._watchlist_with("GJ01AB1234")
        keys = set(watchlist.bloom_keys())
        skel = next(iter(watchlist.skeletons()))
        assert skel in keys
        for variant in single_char_deletions(skel):
            assert variant in keys
