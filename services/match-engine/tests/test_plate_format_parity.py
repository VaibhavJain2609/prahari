"""`prahari_common.plates.PlateFormat` <-> `prahari.v1.PlateFormat` parity.

The IntEnum is written straight onto the proto wire field, so a drifting
integer is a silent mislabel rather than an error -- nothing raises, every
match is just quietly scored under the wrong format. This is the only check
that fails when the two vocabularies diverge.

The test lives in this service's suite rather than `prahari-common`'s because
`prahari-common` deliberately does not depend on `prahari-proto`; this service
declares both, so both are importable here by contract rather than by accident
of the dev environment.
"""

from __future__ import annotations

from prahari.v1 import events_pb2
from prahari_common.plates import PlateFormat


def test_plate_format_values_match_proto() -> None:
    for member in PlateFormat:
        proto_value = events_pb2.PlateFormat.Value(f"PLATE_FORMAT_{member.name}")
        assert int(member) == proto_value, (
            f"PlateFormat.{member.name} is {int(member)} in prahari-common but "
            f"{proto_value} in events.proto"
        )


def test_plate_format_has_same_members_as_proto() -> None:
    proto_names = {
        name.removeprefix("PLATE_FORMAT_") for name in events_pb2.PlateFormat.keys()
    }
    assert proto_names == {member.name for member in PlateFormat}, (
        "proto and prahari-common disagree on which formats exist -- a value "
        "added to one without the other normalises into a format the wire "
        "cannot name, or vice versa"
    )
