"""Client for GET /api/ingest — the camera catalogue.

"Camera ids and the set of available cameras can change; the catalogue is the
contract, the URL pattern is not."

Two consequences shape this module:

* Nothing constructs a stream URL from a template if the catalogue supplied one.
  `CameraEntry.rtsp_url` prefers the catalogue's own value and only falls back
  to the documented pattern when the field is absent.
* Per-camera properties (codec, resolution, declared fps) are carried through to
  the decoder, because §3 warns "a fixed-shape inference batch across every
  camera will not work unscaled".

FIELD NAMES ARE UNVERIFIED. The guide documents *what* the catalogue returns
(id, location, codec, live status, stream properties, all three URLs) but not
the JSON keys. Parsing is therefore alias-tolerant and every entry keeps its
`raw` payload. Confirm the real keys against the first captured snapshot and
tighten this — do not let the tolerance become permanent.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from .config import GatewaySettings, gateway_settings


def _first(payload: dict[str, Any], *names: str) -> Any | None:
    for n in names:
        if n in payload and payload[n] is not None:
            return payload[n]
    return None


class StreamProperties(BaseModel):
    codec: str | None = None
    """H.264 or H.265. Selects rtph264depay/rtph265depay in the GStreamer path
    and is required for correct hardware-decoder setup."""

    width: int | None = None
    height: int | None = None

    declared_fps: float | None = None
    """The catalogue's stated frame rate. Recorded for reference and for
    comparison against reality — NEVER used to derive time. §3: using it for
    "speed, dwell time, or any time-derived metric will produce incorrect
    results". PTSClock.measured_fps is the number that counts."""

    bitrate_kbps: int | None = None

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> StreamProperties:
        res = _first(payload, "resolution", "res")
        width = _first(payload, "width", "w")
        height = _first(payload, "height", "h")
        if width is None and isinstance(res, str) and "x" in res.lower():
            try:
                w, h = res.lower().split("x", 1)
                width, height = int(w.strip()), int(h.strip())
            except ValueError:
                pass
        elif isinstance(res, dict):
            width = width or res.get("width")
            height = height or res.get("height")

        return cls(
            codec=_first(payload, "codec", "video_codec", "encoding"),
            width=width,
            height=height,
            declared_fps=_first(payload, "fps", "frame_rate", "framerate"),
            bitrate_kbps=_first(payload, "bitrate_kbps", "bitrate"),
        )


class CameraEntry(BaseModel):
    """One camera as the catalogue describes it."""

    id: str
    name: str | None = None
    location: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    live: bool = True
    """§5: "Confirm the camera's live status in /api/ingest before reporting it
    as down." We also refuse to open a capture against a camera the catalogue
    says is not live — that would burn a connection slot on a known-dead feed."""

    properties: StreamProperties = Field(default_factory=StreamProperties)

    catalogue_rtsp_url: str | None = None
    catalogue_hls_url: str | None = None
    catalogue_whep_url: str | None = None

    raw: dict[str, Any] = Field(default_factory=dict, repr=False)

    @classmethod
    def parse(cls, payload: dict[str, Any]) -> CameraEntry:
        cam_id = _first(payload, "id", "camera_id", "stream_id", "streamId")
        if cam_id is None:
            raise ValueError(f"catalogue entry has no recognisable id: {sorted(payload)}")

        loc = _first(payload, "location", "site", "place")
        lat = _first(payload, "latitude", "lat")
        lon = _first(payload, "longitude", "lon", "lng")
        if isinstance(loc, dict):
            lat = lat if lat is not None else _first(loc, "latitude", "lat")
            lon = lon if lon is not None else _first(loc, "longitude", "lon", "lng")
            loc = _first(loc, "name", "label", "address")

        live = _first(payload, "live", "is_live", "online", "status")
        if isinstance(live, str):
            live = live.strip().lower() in {"live", "online", "up", "ok", "active", "true"}

        urls = _first(payload, "urls", "endpoints", "streams") or payload

        return cls(
            id=str(cam_id),
            name=_first(payload, "name", "title", "label"),
            location=loc if isinstance(loc, str) else None,
            latitude=lat,
            longitude=lon,
            live=True if live is None else bool(live),
            properties=StreamProperties.parse(payload),
            catalogue_rtsp_url=_first(urls, "rtsp", "rtsp_url", "rtspUrl"),
            catalogue_hls_url=_first(urls, "hls", "hls_url", "hlsUrl", "m3u8"),
            catalogue_whep_url=_first(urls, "whep", "whep_url", "whepUrl", "webrtc"),
            raw=payload,
        )

    def rtsp_url(self, settings: GatewaySettings) -> str:
        """Prefer the catalogue's URL; fall back to the documented pattern."""
        if self.catalogue_rtsp_url:
            return self.catalogue_rtsp_url
        return f"rtsp://{settings.host}:{settings.rtsp_port}/stream/{self.id}"

    def hls_url(self, settings: GatewaySettings) -> str:
        """HLS fallback for when port 8554 is blocked (§3).

        Note the path shape: /live/stream/<id>/index.m3u8 — it is NOT the RTSP
        path with a different port, and it is not MediaMTX's default layout.
        """
        if self.catalogue_hls_url:
            return self.catalogue_hls_url
        return f"{settings.base_url}/live/stream/{self.id}/index.m3u8"

    def whep_url(self, settings: GatewaySettings) -> str:
        """Browser preview only. Never an inference source — the WebRTC path
        loses the PTS fidelity that evidence timestamps depend on."""
        if self.catalogue_whep_url:
            return self.catalogue_whep_url
        return f"{settings.base_url}:{settings.whep_port}/stream/{self.id}/whep"


class Catalogue(BaseModel):
    cameras: list[CameraEntry]
    fetched_at: datetime

    @property
    def live_cameras(self) -> list[CameraEntry]:
        return [c for c in self.cameras if c.live]

    def by_id(self, camera_id: str) -> CameraEntry | None:
        return next((c for c in self.cameras if c.id == camera_id), None)

    def codec_mix(self) -> dict[str, int]:
        """Codec histogram. §4 requires the pipeline handle mixed H.264/H.265;
        this is how we show which mix we actually tested against."""
        mix: dict[str, int] = {}
        for c in self.cameras:
            key = (c.properties.codec or "unknown").lower()
            mix[key] = mix.get(key, 0) + 1
        return mix


class CatalogueClient:
    """Fetches and snapshots the catalogue.

    Read-only by construction: there is no method here that writes to the
    gateway. §3: "Consume only. Do not push streams to any path, and do not call
    the gateway's control API."
    """

    def __init__(self, settings: GatewaySettings | None = None) -> None:
        self._s = settings or gateway_settings()

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self._s.request_timeout_s,
            verify=self._s.verify_tls,
            follow_redirects=True,
            # The access password. The exact scheme the gateway expects is not
            # documented in the integrator's guide, so both the common forms are
            # presented and the unused one is ignored by the server. Replace
            # this with the single correct mechanism once confirmed against a
            # real 200 — leaving both in place permanently means sending the
            # credential somewhere it was not needed.
            auth=("", self._s.password.get_secret_value()),
            headers={"X-Access-Password": self._s.password.get_secret_value()},
        )

    def fetch(self) -> Catalogue:
        with self._client() as client:
            resp = client.get(self._s.catalogue_url)
            resp.raise_for_status()
            payload = resp.json()
        return Catalogue(cameras=_parse_entries(payload), fetched_at=datetime.now(UTC))

    def snapshot(self, catalogue: Catalogue, directory: str | Path) -> Path:
        """Persist a catalogue to disk.

        Snapshots are how a support report cites what the catalogue said at a
        given UTC time (§5), and how the demo stays reproducible when ids rotate
        between build and judging. They record the gateway host, so review the
        directory before publishing the repo.
        """
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        stamp = catalogue.fetched_at.strftime("%Y%m%dT%H%M%SZ")
        path = d / f"ingest-{stamp}.json"
        path.write_text(
            json.dumps(
                {
                    "fetched_at": catalogue.fetched_at.isoformat(),
                    "camera_count": len(catalogue.cameras),
                    "live_count": len(catalogue.live_cameras),
                    "codec_mix": catalogue.codec_mix(),
                    "cameras": [c.raw for c in catalogue.cameras],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        return path

    @staticmethod
    def load_snapshot(path: str | Path) -> Catalogue:
        """Rehydrate a snapshot. Lets the whole pipeline be developed and tested
        without holding open connections to the live government feed."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return Catalogue(
            cameras=[CameraEntry.parse(c) for c in data["cameras"]],
            fetched_at=datetime.fromisoformat(data["fetched_at"]),
        )


def _parse_entries(payload: Any) -> list[CameraEntry]:
    """Accept the three plausible envelope shapes: a bare list, {"cameras": [...]},
    or {"data": [...]}. Which one it actually is gets pinned down on first
    contact."""
    if isinstance(payload, dict):
        for key in ("cameras", "streams", "data", "items", "results"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            raise ValueError(
                f"unrecognised catalogue envelope, top-level keys: {sorted(payload)}"
            )
    if not isinstance(payload, list):
        raise ValueError(f"expected a list of cameras, got {type(payload).__name__}")
    return [CameraEntry.parse(c) for c in payload]
