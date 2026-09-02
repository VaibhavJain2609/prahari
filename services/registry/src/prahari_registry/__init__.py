"""PRAHARI camera registry — the control plane.

Small, authoritative, and the only service that knows the full estate. It holds
no pixels: what it stores is where cameras are, who owns them, how to reach
them, and whether they are working right now.
"""

from .health import HealthPolicy, HealthVerdict, derive_state
from .models import Camera, CameraHealth, HealthState, Heartbeat, Lifecycle

__all__ = [
    "Camera",
    "CameraHealth",
    "HealthPolicy",
    "HealthState",
    "HealthVerdict",
    "Heartbeat",
    "Lifecycle",
    "derive_state",
]
