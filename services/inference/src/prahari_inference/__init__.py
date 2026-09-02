"""PRAHARI edge inference.

Video is decoded here, at the edge, and only metadata leaves. Nothing in this
package uploads a frame.
"""

from prahari_common.catalogue import CameraEntry, Catalogue, CatalogueClient

from .capture import Frame, SampleGate, StreamCapture
from .timing import FrameTiming, PTSClock

__all__ = [
    "CameraEntry",
    "Catalogue",
    "CatalogueClient",
    "Frame",
    "FrameTiming",
    "PTSClock",
    "SampleGate",
    "StreamCapture",
]
