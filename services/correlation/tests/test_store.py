from __future__ import annotations

from datetime import UTC, datetime

from google.protobuf.timestamp_pb2 import Timestamp
from prahari.v1 import common_pb2, events_pb2

from prahari_correlation.store import DetectionStore, plate_key, wall_clock_s

_counter = 0


def _detection(
    camera_id: str,
    *,
    plate_text: str | None = None,
    wall_clock_s_value: float = 1000.0,
    appearance_embedding: list[float] | None = None,
    detection_id: str | None = None,
) -> events_pb2.VehicleDetection:
    global _counter
    _counter += 1
    ts = Timestamp()
    ts.FromDatetime(datetime.fromtimestamp(wall_clock_s_value, tz=UTC))
    kwargs = {}
    if plate_text is not None:
        kwargs["plate"] = events_pb2.PlateReading(normalised_text=plate_text)
    if appearance_embedding is not None:
        kwargs["appearance_embedding"] = appearance_embedding
    return events_pb2.VehicleDetection(
        # Unique per call by default: dedup by detection_id is a feature, so
        # a helper that minted the same id every time would silently drop
        # every detection after the first.
        detection_id=detection_id if detection_id is not None else f"D{_counter}",
        camera_id=camera_id,
        observed_at=common_pb2.StreamTime(wall_clock=ts),
        **kwargs,
    )


def test_plate_key_normalises_the_same_way_common_does() -> None:
    assert plate_key("GJ 01 AB 1234") == plate_key("gj01ab1234") == "6J01A81234"


def test_plate_key_is_the_confusion_skeleton_not_the_normalised_text() -> None:
    # The whole point of skeleton keying: one vehicle read two ways by OCR
    # (`2`/`Z`, `5`/`S` confusions) must land in ONE bucket, or the route
    # fractures into two half-routes.
    assert plate_key("GJ01AB1234") == plate_key("GJ01AB1Z34")
    assert plate_key("GJ01AB1234") != "GJ01AB1234"  # folded: G->6, B->8


def test_wall_clock_s_round_trips() -> None:
    detection = _detection("CAM-1", wall_clock_s_value=1700000000.0)
    assert abs(wall_clock_s(detection) - 1700000000.0) < 1e-6


def test_wall_clock_s_unset_is_zero() -> None:
    detection = events_pb2.VehicleDetection(detection_id="D1", camera_id="CAM-1")
    assert wall_clock_s(detection) == 0.0


def test_plated_detection_is_retrievable_by_plate() -> None:
    store = DetectionStore(max_per_plate=10, max_plates=10)
    store.add(_detection("CAM-1", plate_text="GJ01AB1234"))

    sightings = store.by_plate("gj 01 ab 1234")
    assert len(sightings) == 1
    assert sightings[0].camera_id == "CAM-1"


def test_by_plate_returns_sightings_oldest_first() -> None:
    store = DetectionStore(max_per_plate=10, max_plates=10)
    store.add(_detection("CAM-2", plate_text="GJ01AB1234", wall_clock_s_value=2000.0))
    store.add(_detection("CAM-1", plate_text="GJ01AB1234", wall_clock_s_value=1000.0))

    sightings = store.by_plate("GJ01AB1234")
    assert [s.camera_id for s in sightings] == ["CAM-1", "CAM-2"]


def test_per_plate_buffer_is_bounded() -> None:
    store = DetectionStore(max_per_plate=2, max_plates=10)
    for i in range(5):
        store.add(_detection("CAM-1", plate_text="GJ01AB1234", wall_clock_s_value=1000.0 + i))

    assert len(store.by_plate("GJ01AB1234")) == 2
    # The oldest sightings were evicted, not the newest.
    kept = [s.observed_at.wall_clock.ToSeconds() for s in store.by_plate("GJ01AB1234")]
    assert kept == [1003, 1004]


def test_distinct_plate_count_is_bounded_by_lru_eviction() -> None:
    store = DetectionStore(max_per_plate=10, max_plates=2)
    store.add(_detection("CAM-1", plate_text="AA01AA0001"))
    store.add(_detection("CAM-1", plate_text="AA01AA0002"))
    store.add(_detection("CAM-1", plate_text="AA01AA0003"))  # evicts AA01AA0001

    assert store.tracked_plate_count() == 2
    assert store.by_plate("AA01AA0001") == []
    assert len(store.by_plate("AA01AA0003")) == 1


def test_touching_a_plate_again_refreshes_its_lru_position() -> None:
    store = DetectionStore(max_per_plate=10, max_plates=2)
    store.add(_detection("CAM-1", plate_text="AA01AA0001"))
    store.add(_detection("CAM-1", plate_text="AA01AA0002"))
    store.add(_detection("CAM-1", plate_text="AA01AA0001"))  # touch -- now most-recent
    store.add(_detection("CAM-1", plate_text="AA01AA0003"))  # should evict AA01AA0002, not 0001

    assert len(store.by_plate("AA01AA0001")) == 2
    assert store.by_plate("AA01AA0002") == []


def test_unplated_detection_is_not_indexed_by_plate_but_is_kept() -> None:
    store = DetectionStore(max_per_plate=10, max_plates=10)
    store.add(_detection("CAM-1", appearance_embedding=[0.1, 0.2]))

    candidates = list(store.unplated_between(0.0, 2000.0))
    assert len(candidates) == 1
    assert candidates[0].camera_id == "CAM-1"


def test_unplated_between_filters_by_window() -> None:
    store = DetectionStore(max_per_plate=10, max_plates=10)
    store.add(_detection("CAM-1", wall_clock_s_value=1000.0, appearance_embedding=[0.1]))
    store.add(_detection("CAM-2", wall_clock_s_value=5000.0, appearance_embedding=[0.1]))

    candidates = list(store.unplated_between(900.0, 1100.0))
    assert [c.camera_id for c in candidates] == ["CAM-1"]


def test_a_detection_with_no_usable_timestamp_is_dropped_not_silently_kept() -> None:
    # No observed_at at all -- wall_clock_s() would read this as 0.0, which
    # sorts before every real detection and makes every hop through it look
    # trivially feasible. add() must refuse it rather than index it.
    store = DetectionStore(max_per_plate=10, max_plates=10)
    plated = events_pb2.VehicleDetection(
        detection_id="D-NO-TS",
        camera_id="CAM-1",
        plate=events_pb2.PlateReading(normalised_text="GJ01AB1234"),
    )
    unplated = events_pb2.VehicleDetection(
        detection_id="D-NO-TS-2", camera_id="CAM-1", appearance_embedding=[0.1, 0.2]
    )

    store.add(plated)
    store.add(unplated)

    assert store.by_plate("GJ01AB1234") == []
    assert list(store.unplated_between(0.0, 1e12)) == []


def test_a_detection_timestamped_in_the_future_is_dropped_as_poisoned() -> None:
    # A wall_clock beyond now()+allowance would pin the detection at the END
    # of the plate's history and turn every real later sighting into a
    # negative-elapsed hop. Inside the allowance is tolerable skew; beyond it
    # is dropped, not quarantined -- same as a missing timestamp.
    store = DetectionStore(max_per_plate=10, max_plates=10, future_skew_allowance_s=5.0)
    now = datetime.now(tz=UTC).timestamp()
    store.add(_detection("CAM-1", plate_text="GJ01AB1234", wall_clock_s_value=now + 3600))
    store.add(_detection("CAM-1", plate_text="GJ01AB1234", wall_clock_s_value=now + 2))

    sightings = store.by_plate("GJ01AB1234")
    assert len(sightings) == 1
    assert abs(wall_clock_s(sightings[0]) - (now + 2)) < 1e-3


def test_ocr_confusable_readings_of_one_plate_share_a_bucket() -> None:
    # `GJ01AB1234` and `GJ01AB1Z34` differ by exactly one 2/Z confusion --
    # the fragmentation bug this store keys on skeletons to prevent.
    store = DetectionStore(max_per_plate=10, max_plates=10)
    store.add(_detection("CAM-1", plate_text="GJ01AB1234", wall_clock_s_value=1000.0))
    store.add(_detection("CAM-2", plate_text="GJ01AB1Z34", wall_clock_s_value=2000.0))

    sightings = store.by_plate("GJ01AB1234")
    assert [s.camera_id for s in sightings] == ["CAM-1", "CAM-2"]
    # The raw reading survives on the stored sighting -- the skeleton is only
    # the index key, never what gets displayed.
    assert sightings[1].plate.normalised_text == "GJ01AB1Z34"


def test_a_replayed_detection_id_is_deduped() -> None:
    # XADD retries / consumer restarts can deliver the same detection twice;
    # the second copy must not become a second sighting of the same instant.
    store = DetectionStore(max_per_plate=10, max_plates=10)
    detection = _detection("CAM-1", plate_text="GJ01AB1234", detection_id="D-REPLAY")
    store.add(detection)
    store.add(detection)

    assert len(store.by_plate("GJ01AB1234")) == 1


def test_dedup_falls_back_to_camera_pts_plate_tuple_when_no_detection_id() -> None:
    store = DetectionStore(max_per_plate=10, max_plates=10)
    first = _detection("CAM-1", plate_text="GJ01AB1234", detection_id="")
    first.observed_at.pts_ms = 5000
    replay = events_pb2.VehicleDetection()
    replay.CopyFrom(first)
    different = _detection("CAM-1", plate_text="GJ01AB1234", detection_id="")
    different.observed_at.pts_ms = 5333  # a later frame, not a replay

    store.add(first)
    store.add(replay)
    store.add(different)

    assert len(store.by_plate("GJ01AB1234")) == 2
