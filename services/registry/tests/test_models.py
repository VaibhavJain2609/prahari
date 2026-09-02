"""API models against the protobuf contract.

The wire format between services is protobuf; the REST face uses lowercase
strings. Both describe the same states, and a divergence would not fail loudly —
it would show up as a camera quietly missing from the map because the console
filtered on a value the API never emits.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from prahari_registry.models import (
    PROTO_CAMERA_TYPE,
    PROTO_HEALTH_STATE,
    CameraHealth,
    CameraType,
    HealthState,
)

PROTO = Path(__file__).resolve().parents[3] / "proto" / "prahari" / "v1" / "camera.proto"


def _proto_enum_values(name: str) -> set[str]:
    body = re.search(rf"enum {name} \{{(.*?)\}}", PROTO.read_text(), re.DOTALL)
    assert body, f"enum {name} not found in {PROTO}"
    return set(re.findall(r"^\s*([A-Z_]+)\s*=\s*\d+;", body.group(1), re.MULTILINE))


@pytest.mark.skipif(not PROTO.exists(), reason="proto contract not present")
def test_health_states_cover_the_proto_enum():
    assert set(PROTO_HEALTH_STATE.values()) == _proto_enum_values("HealthState")
    assert set(PROTO_HEALTH_STATE) == set(HealthState)


@pytest.mark.skipif(not PROTO.exists(), reason="proto contract not present")
def test_camera_types_cover_the_proto_enum():
    assert set(PROTO_CAMERA_TYPE.values()) == _proto_enum_values("CameraType")
    assert set(PROTO_CAMERA_TYPE) == set(CameraType)


def test_fps_drift_is_derived_from_both_rates():
    health = CameraHealth(observed_fps=8.0, declared_fps=25.0)
    assert health.fps_drift == 0.32


def test_fps_drift_is_absent_when_either_rate_is():
    """Drift against an unknown declared rate is not zero, and not 1.0. It is
    unknown, and the console must be able to tell the difference."""
    assert CameraHealth(observed_fps=8.0).fps_drift is None
    assert CameraHealth(declared_fps=25.0).fps_drift is None
