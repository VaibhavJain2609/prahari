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
