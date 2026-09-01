"""FFmpeg/OpenCV environment setup. MUST be imported before cv2, anywhere.

OpenCV reads OPENCV_FFMPEG_CAPTURE_OPTIONS once, when the FFmpeg backend is
first initialised. Setting it after `import cv2` has already happened is a
silent no-op: captures then negotiate RTSP over UDP, which the integrator's
guide warns "fails across NAT and most corporate firewalls" and whose partial
delivery "produces corrupt frames that look like model bugs".

That failure mode is expensive precisely because it is not an exception — it is
a plausible-looking frame with garbage in it, feeding a detector that will
happily emit a plate reading from the garbage.

So the ordering is enforced structurally rather than by convention: this module
sets the environment and then re-exports cv2. Every module in this package does

    from .rtsp_env import cv2

and never `import cv2` directly. A lint rule enforces it (see pyproject).
"""

from __future__ import annotations

import os

# rtsp_transport;tcp — semicolon-separated key;value pairs, pipe-separated pairs.
# stimeout is in MICROseconds: 10_000_000 = 10s connect/read timeout, so a dead
# feed surfaces as a failed read instead of hanging the worker thread forever.
_CAPTURE_OPTIONS = "rtsp_transport;tcp|stimeout;10000000|max_delay;500000"

_existing = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
if _existing and "rtsp_transport;tcp" not in _existing:
    raise RuntimeError(
        "OPENCV_FFMPEG_CAPTURE_OPTIONS is already set without rtsp_transport;tcp: "
        f"{_existing!r}. Refusing to start — UDP transport yields corrupt frames "
        "that look like detections. Unset it or include TCP."
    )
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = _CAPTURE_OPTIONS

# Decoder chatter on join ("Error constructing the frame RPS", "Could not find
# ref with POC") is expected until the first IDR arrives and is NOT fatal. Keep
# it at warning so it lands in logs for diagnosis without drowning them.
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "24")  # AV_LOG_WARNING

import cv2  # noqa: E402  — deliberately after the env vars above

__all__ = ["cv2", "capture_options"]


def capture_options() -> str:
    """The options actually handed to FFmpeg. Logged at startup so a support
    report can state the client configuration exactly, as §5 requires."""
    return _CAPTURE_OPTIONS
