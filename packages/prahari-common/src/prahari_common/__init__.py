"""PRAHARI shared contract layer.

What belongs here: anything two or more services must agree on at runtime and
where a second copy would be a bug — the gateway connection rules and the
`/api/ingest` catalogue client.

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
