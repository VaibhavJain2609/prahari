"""dedup.py: one vehicle dwell must produce one alert (DAY2-DESIGN.md §7.3),
and the table doing that bucketing must not grow without bound over a
long-running process.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from prahari_match.dedup import Deduper


def test_first_sighting_in_a_bucket_is_alertable() -> None:
    deduper = Deduper(bucket_s=8.0, max_entries=1000)
    assert deduper.should_alert("CAM-1", "GJ01AB1234", wall_clock_s=1000.0) is True


def test_repeat_sighting_in_the_same_bucket_is_suppressed() -> None:
    # A vehicle in frame for 8s at 3fps: many detections, one alert.
    deduper = Deduper(bucket_s=8.0, max_entries=1000)
    assert deduper.should_alert("CAM-1", "GJ01AB1234", wall_clock_s=1000.0) is True
    for t in (1000.5, 1001.0, 1003.9, 1007.9):
        assert deduper.should_alert("CAM-1", "GJ01AB1234", wall_clock_s=t) is False


def test_sighting_in_the_next_bucket_alerts_again() -> None:
    deduper = Deduper(bucket_s=8.0, max_entries=1000)
    assert deduper.should_alert("CAM-1", "GJ01AB1234", wall_clock_s=1000.0) is True
    assert deduper.should_alert("CAM-1", "GJ01AB1234", wall_clock_s=1009.0) is True


def test_different_camera_is_a_different_key() -> None:
    deduper = Deduper(bucket_s=8.0, max_entries=1000)
    assert deduper.should_alert("CAM-1", "GJ01AB1234", wall_clock_s=1000.0) is True
    assert deduper.should_alert("CAM-2", "GJ01AB1234", wall_clock_s=1000.0) is True


def test_different_plate_is_a_different_key() -> None:
    deduper = Deduper(bucket_s=8.0, max_entries=1000)
    assert deduper.should_alert("CAM-1", "GJ01AB1234", wall_clock_s=1000.0) is True
    assert deduper.should_alert("CAM-1", "GJ05CD5678", wall_clock_s=1000.0) is True


def test_bounded_memory_evicts_oldest_entry() -> None:
    deduper = Deduper(bucket_s=8.0, max_entries=3)
    for i in range(5):
        deduper.should_alert(f"CAM-{i}", "GJ01AB1234", wall_clock_s=1000.0)
    assert len(deduper) == 3


def test_construction_rejects_non_positive_parameters() -> None:
    import pytest

    with pytest.raises(ValueError):
        Deduper(bucket_s=0, max_entries=10)
    with pytest.raises(ValueError):
        Deduper(bucket_s=8.0, max_entries=0)


def test_concurrent_first_sightings_in_the_same_bucket_produce_exactly_one_alert() -> None:
    # P4: `should_alert` is called from `MetadataIngestServicer`'s
    # `ThreadPoolExecutor`. Before the fix, two threads could both see
    # `key in self._seen` as False before either assigned it, and both
    # return True -- two alerts for one dwell. Hammer the same
    # (camera, plate, bucket) from many threads at once; exactly one call
    # must win.
    deduper = Deduper(bucket_s=8.0, max_entries=1000)
    workers = 32

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(
            pool.map(
                lambda _: deduper.should_alert("CAM-1", "GJ01AB1234", wall_clock_s=1000.0),
                range(workers),
            )
        )

    assert results.count(True) == 1
    assert results.count(False) == workers - 1
