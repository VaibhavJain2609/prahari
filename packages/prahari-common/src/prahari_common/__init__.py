"""PRAHARI shared contract layer.

What belongs here: anything two or more services must agree on at runtime and
where a second copy would be a bug —

- `catalogue`/`config`: the gateway connection rules and the `/api/ingest`
  catalogue client. The registry and every ingest worker reach the same
  gateway, so there is one copy of how to reach it.
- `plates`: the plate grammar. Inference and the match engine must agree
  exactly on what a plate looks like; if they normalised differently every
  lookup would miss and nothing would raise. Tolerance (confusion classes,
  edit costs) stays in the match engine — grammar lives here, once.
- `bus`: `RedisStreamConsumer`, the shared reader for protobuf messages on
  Redis Streams (`prahari:detections`, `prahari:alerts`). Correlation and the
  BFF consume the same entries, so the offset/xread semantics live in one
  place. `redis` is not a hard dependency — consumers declare it themselves.

What does not: anything service-specific. Ingest sampling policy stays in the
inference service, database access stays in the registry. This package has no
opinion about video decoding and deliberately does not depend on OpenCV, so a
service that only reads the catalogue does not inherit a 60 MB decoder.
"""

from .catalogue import CameraEntry, Catalogue, CatalogueClient, StreamProperties
from .config import GatewaySettings, gateway_settings

__all__ = [
    "CameraEntry",
    "Catalogue",
    "CatalogueClient",
    "GatewaySettings",
    "StreamProperties",
    "gateway_settings",
]
