"""catalogue.py: the /api/ingest client.

The catalogue is the contract — "camera ids and the set of available cameras
can change". These tests pin the alias-tolerant parsing (the field names are
unverified against the live gateway, so every documented alias must keep
working), the URL fallbacks (catalogue-supplied URL wins over the documented
pattern), and the cookie-login flow — all against httpx.MockTransport, the
same fake the inference worker's registry client uses.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

import prahari_common.catalogue as catalogue_module
from prahari_common.catalogue import (
    CameraEntry,
    Catalogue,
    CatalogueClient,
    StreamProperties,
    _parse_entries,
)
from prahari_common.config import GatewaySettings


def _settings(**overrides) -> GatewaySettings:
    kwargs: dict = {
        "host": "cdn.example.test",
        "password": SecretStr("s3cret"),
    }
    kwargs.update(overrides)
    return GatewaySettings(**kwargs)


# --- field aliases ---------------------------------------------------------------


class TestFirst:
    def test_returns_the_first_present_non_none_value(self) -> None:
        assert catalogue_module._first({"b": 2, "a": 1}, "a", "b") == 1  # noqa: SLF001
        assert catalogue_module._first({"a": None, "b": 2}, "a", "b") == 2  # noqa: SLF001

    def test_returns_none_when_nothing_matches(self) -> None:
        assert catalogue_module._first({"x": 1}, "a", "b") is None  # noqa: SLF001


class TestStreamProperties:
    def test_parses_a_resolution_string(self) -> None:
        props = StreamProperties.parse({"resolution": "1920x1080", "codec": "h265"})
        assert (props.width, props.height) == (1920, 1080)
        assert props.codec == "h265"

    def test_explicit_width_height_win_over_resolution(self) -> None:
        props = StreamProperties.parse({"width": 640, "height": 480, "resolution": "1x1"})
        assert (props.width, props.height) == (640, 480)

    def test_resolution_dict_is_unpacked(self) -> None:
        props = StreamProperties.parse({"resolution": {"width": 1280, "height": 720}})
        assert (props.width, props.height) == (1280, 720)

    def test_an_unparsable_resolution_is_ignored_not_fatal(self) -> None:
        # A res string with an "x" but non-integer halves must not kill the
        # camera entry — codec and fps still parse, width/height stay unknown.
        props = StreamProperties.parse({"resolution": "1280xbogus", "fps": 25})
        assert props.width is None and props.height is None
        assert props.declared_fps == 25

    def test_alias_fields(self) -> None:
        props = StreamProperties.parse(
            {"video_codec": "h264", "frame_rate": 15, "bitrate": 800, "w": 320, "h": 240}
        )
        assert props.codec == "h264"
        assert props.declared_fps == 15
        assert props.bitrate_kbps == 800
        assert (props.width, props.height) == (320, 240)


class TestCameraEntryParse:
    def test_minimal_entry(self) -> None:
        entry = CameraEntry.parse({"id": "cam-1"})
        assert entry.id == "cam-1"
        assert entry.live is True  # absent live flag defaults to live
        assert entry.raw == {"id": "cam-1"}

    @pytest.mark.parametrize("key", ["id", "camera_id", "stream_id", "streamId"])
    def test_id_aliases(self, key: str) -> None:
        assert CameraEntry.parse({key: "cam-9"}).id == "cam-9"

    def test_numeric_id_is_coerced_to_str(self) -> None:
        assert CameraEntry.parse({"id": 42}).id == "42"

    def test_no_recognisable_id_raises(self) -> None:
        with pytest.raises(ValueError, match="no recognisable id"):
            CameraEntry.parse({"name": "mystery"})

    def test_location_dict_supplies_name_and_coordinates(self) -> None:
        entry = CameraEntry.parse(
            {"id": "c", "location": {"name": "Ring Road", "lat": 23.0, "lng": 72.5}}
        )
        assert entry.location == "Ring Road"
        assert entry.latitude == 23.0
        assert entry.longitude == 72.5

    def test_top_level_coordinates_win_over_location_dict(self) -> None:
        entry = CameraEntry.parse(
            {
                "id": "c",
                "lat": 10.0,
                "lon": 20.0,
                "location": {"lat": 99.0, "lng": 99.0, "label": "Site"},
            }
        )
        assert (entry.latitude, entry.longitude) == (10.0, 20.0)
        assert entry.location == "Site"

    def test_a_non_dict_non_str_location_is_dropped(self) -> None:
        entry = CameraEntry.parse({"id": "c", "location": 5})
        assert entry.location is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("live", True),
            ("ONLINE", True),
            ("up", True),
            ("ok", True),
            ("active", True),
            ("true", True),
            ("offline", False),
            ("", False),
            (False, False),
            (True, True),
        ],
    )
    def test_live_flag_parsing(self, raw, expected: bool) -> None:
        assert CameraEntry.parse({"id": "c", "status": raw}).live is expected

    def test_nested_urls_object(self) -> None:
        entry = CameraEntry.parse(
            {
                "id": "c",
                "urls": {"rtsp": "rtsp://x/1", "hls": "https://x/1.m3u8", "whep": "http://x/w"},
            }
        )
        assert entry.catalogue_rtsp_url == "rtsp://x/1"
        assert entry.catalogue_hls_url == "https://x/1.m3u8"
        assert entry.catalogue_whep_url == "http://x/w"

    def test_flat_url_fields_on_the_payload(self) -> None:
        entry = CameraEntry.parse({"id": "c", "rtsp_url": "rtsp://flat/1", "m3u8": "h"})
        assert entry.catalogue_rtsp_url == "rtsp://flat/1"
        assert entry.catalogue_hls_url == "h"

    def test_name_aliases(self) -> None:
        assert CameraEntry.parse({"id": "c", "title": "Gate 4"}).name == "Gate 4"


class TestUrlFallbacks:
    """The catalogue's own URL always wins; the documented pattern is only a
    fallback — the invariant that keeps `stream/<id>` out of our code when the
    catalogue says otherwise."""

    def test_catalogue_rtsp_url_wins(self) -> None:
        entry = CameraEntry.parse({"id": "c", "urls": {"rtsp": "rtsp://given/1"}})
        assert entry.rtsp_url(_settings()) == "rtsp://given/1"

    def test_rtsp_fallback_uses_direct_host_not_the_cdn(self) -> None:
        # Raw RTSP cannot traverse the CDN — the fallback must go to
        # direct_host, and to host only when direct_host is unset.
        settings = _settings(direct_host="203.0.113.5")
        entry = CameraEntry.parse({"id": "cam-7"})
        assert entry.rtsp_url(settings) == "rtsp://203.0.113.5:8554/stream/cam-7"
        assert entry.rtsp_url(_settings()) == "rtsp://cdn.example.test:8554/stream/cam-7"

    def test_hls_fallback_shape(self) -> None:
        entry = CameraEntry.parse({"id": "cam-7"})
        assert entry.hls_url(_settings()) == "https://cdn.example.test/cam-7/index.m3u8"
        entry2 = CameraEntry.parse({"id": "c", "urls": {"hls": "https://given/2.m3u8"}})
        assert entry2.hls_url(_settings()) == "https://given/2.m3u8"

    def test_whep_fallback_is_plain_http_on_direct_host(self) -> None:
        settings = _settings(direct_host="203.0.113.5")
        entry = CameraEntry.parse({"id": "cam-7"})
        assert entry.whep_url(settings) == "http://203.0.113.5:8889/stream/cam-7/whep"
        entry2 = CameraEntry.parse({"id": "c", "urls": {"whep": "http://given/w"}})
        assert entry2.whep_url(settings) == "http://given/w"


class TestCatalogue:
    def _catalogue(self) -> Catalogue:
        from datetime import UTC, datetime

        return Catalogue(
            cameras=[
                CameraEntry.parse({"id": "a", "codec": "h264", "live": True}),
                CameraEntry.parse({"id": "b", "codec": "H265", "live": False}),
                CameraEntry.parse({"id": "c"}),  # no codec -> "unknown"
            ],
            fetched_at=datetime.now(UTC),
        )

    def test_live_cameras_filters_dead_entries(self) -> None:
        assert [c.id for c in self._catalogue().live_cameras] == ["a", "c"]

    def test_by_id(self) -> None:
        cat = self._catalogue()
        assert cat.by_id("b").id == "b"
        assert cat.by_id("missing") is None

    def test_codec_mix_is_case_insensitive_and_counts_unknown(self) -> None:
        assert self._catalogue().codec_mix() == {"h264": 1, "h265": 1, "unknown": 1}


class TestParseEntries:
    def test_bare_list(self) -> None:
        entries = _parse_entries([{"id": "a"}])
        assert [e.id for e in entries] == ["a"]

    @pytest.mark.parametrize("key", ["cameras", "streams", "data", "items", "results"])
    def test_envelope_keys(self, key: str) -> None:
        entries = _parse_entries({key: [{"id": "a"}], "unrelated": 1})
        assert [e.id for e in entries] == ["a"]

    def test_unrecognised_envelope_raises_with_keys(self) -> None:
        with pytest.raises(ValueError, match="unrecognised catalogue envelope.*'weird'"):
            _parse_entries({"weird": []})

    def test_non_list_payload_raises(self) -> None:
        with pytest.raises(ValueError, match="expected a list of cameras, got str"):
            _parse_entries("not json shaped like a catalogue")


# --- the HTTP client ---------------------------------------------------------------


def _catalogue_handler(requests: list, login_status: int = 200):
    """A MockTransport handler for the two-request flow: POST login (form
    field `password`, sets a session cookie) then GET the catalogue."""

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/auth/login":
            if login_status >= 400:
                return httpx.Response(login_status, json={"detail": "denied"})
            return httpx.Response(
                200,
                json={"ok": True},
                headers={"set-cookie": "session=tok123; Path=/"},
            )
        if request.url.path == "/cameras.json":
            return httpx.Response(
                200,
                json={
                    "cameras": [
                        {
                            "id": "cam-1",
                            "name": "Ring Road",
                            "live": True,
                            "resolution": "1920x1080",
                            "codec": "h264",
                            "urls": {"rtsp": "rtsp://given/cam-1"},
                        },
                        {"camera_id": "cam-2", "is_live": "offline"},
                    ]
                },
            )
        return httpx.Response(404)

    return handler


def _mock_httpx(monkeypatch, handler) -> None:
    """Route `httpx.Client(...)` inside catalogue.py through MockTransport.

    `partial(httpx.Client, transport=...)` keeps every other kwarg (timeout,
    verify, follow_redirects) real — the client code is exercised as written,
    only the wire is faked."""
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(httpx, "Client", partial(httpx.Client, transport=transport))


def test_fetch_logs_in_then_gets_the_catalogue(monkeypatch) -> None:
    requests: list[httpx.Request] = []
    _mock_httpx(monkeypatch, _catalogue_handler(requests))
    client = CatalogueClient(_settings())

    catalogue = client.fetch()

    assert [r.url.path for r in requests] == ["/auth/login", "/cameras.json"]
    # The login POST carried the password as a form field — the gateway
    # authenticates by session cookie, not a header.
    assert b"password=s3cret" in requests[0].content
    # ...and the catalogue GET rode the Set-Cookie the login response set.
    assert requests[1].headers.get("cookie") == "session=tok123"
    assert [c.id for c in catalogue.cameras] == ["cam-1", "cam-2"]
    cam1 = catalogue.by_id("cam-1")
    assert cam1.properties.width == 1920 and cam1.properties.codec == "h264"
    assert cam1.catalogue_rtsp_url == "rtsp://given/cam-1"
    assert not catalogue.by_id("cam-2").live


def test_fetch_raises_when_login_is_rejected(monkeypatch) -> None:
    _mock_httpx(monkeypatch, _catalogue_handler([], login_status=403))
    client = CatalogueClient(_settings())

    with pytest.raises(RuntimeError, match="gateway login failed.*403"):
        client.fetch()


def test_fetch_raises_when_login_sets_no_cookie(monkeypatch) -> None:
    # A 200 with no Set-Cookie means the login silently did not authenticate —
    # treating it as success would fetch the catalogue as an anonymous client.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, json={"ok": True})  # no cookie
        return httpx.Response(200, json=[])

    _mock_httpx(monkeypatch, handler)
    client = CatalogueClient(_settings())

    with pytest.raises(RuntimeError, match="gateway login failed"):
        client.fetch()


def test_fetch_propagates_a_catalogue_http_error(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/login":
            return httpx.Response(200, headers={"set-cookie": "session=t; Path=/"})
        return httpx.Response(500, json={"detail": "upstream exploded"})

    _mock_httpx(monkeypatch, handler)
    client = CatalogueClient(_settings())

    with pytest.raises(httpx.HTTPStatusError):
        client.fetch()


# --- snapshots ---------------------------------------------------------------


def test_snapshot_round_trips_through_load_snapshot(tmp_path: Path, monkeypatch) -> None:
    _mock_httpx(monkeypatch, _catalogue_handler([]))
    client = CatalogueClient(_settings())
    catalogue = client.fetch()

    path = client.snapshot(catalogue, tmp_path)

    # The filename is stamped from fetched_at, UTC — a support report cites
    # what the catalogue said at a given time.
    assert path.name.startswith("ingest-") and path.suffix == ".json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["camera_count"] == 2
    assert payload["live_count"] == 1
    assert payload["codec_mix"] == {"h264": 1, "unknown": 1}

    reloaded = CatalogueClient.load_snapshot(path)
    assert [c.id for c in reloaded.cameras] == ["cam-1", "cam-2"]
    assert reloaded.fetched_at == catalogue.fetched_at
    # raw payloads round-trip, so rehydrated entries re-parse identically.
    assert reloaded.by_id("cam-1").catalogue_rtsp_url == "rtsp://given/cam-1"
