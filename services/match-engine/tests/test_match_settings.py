"""Chart/settings parity for the match engine, both directions (M3).

`services/inference/tests/test_detector_settings.py` only asserts chart-to-
field: a `DetectorSettings` field the chart never overrides is fine, because
it just means no profile needs that knob yet. M3 found that the *reverse*
gap matters here in practice -- `MatchSettings.redis_url` was silently `None`
in every deployed profile because nothing in the chart ever set
`PRAHARI_MATCH_REDIS_URL`, even though the field existed and Redis itself was
already deployed for every other service. "One schema, two transports"
degraded to one transport with no test catching it.

So this file asserts both:
  1. every `PRAHARI_MATCH_*` name the chart sets maps to a real field
     (the DetectorSettings-style check), and
  2. every `MatchSettings` field is either chart-exposed, or named in
     `_DELIBERATELY_INTERNAL` with a reason it should stay that way.

A new field that is neither chart-exposed nor allowlisted fails (2) --
forcing the choice to be made on purpose, in this file, instead of by
omission.

Regex over the raw template rather than a YAML parser, same reasoning as
`test_detector_settings.py`: `_helpers.tpl` is a Helm template, not valid
YAML, and only env var names matter here, not the templated values.
"""

from __future__ import annotations

import re
from pathlib import Path

from prahari_match.config import MatchSettings

_CHART = Path(__file__).parents[3] / "infra" / "helm" / "prahari" / "templates" / "_helpers.tpl"
_BLOCK = re.compile(r'{{-\s*define "prahari\.matchEngineEnv"\s*-}}(.*?){{-\s*end\s*-}}', re.DOTALL)
_ENV_NAME = re.compile(r"-\s*name:\s*(PRAHARI_MATCH_[A-Z0-9_]+)")

# Fields the chart deliberately never sets, with the reason it should stay
# that way. Anything not listed here MUST be chart-exposed -- see module
# docstring.
_DELIBERATELY_INTERNAL = {
    "grpc_host": "always 0.0.0.0 in-cluster; no profile needs a different bind address",
    "grpc_max_workers": "thread-pool sizing, not an accuracy or deployment-topology knob",
    "bloom_expected_entries": (
        "sizing input tuned against the accuracy tests in tests/test_matcher.py, "
        "not a per-deployment value"
    ),
    "bloom_target_fp_rate": "tuned against tests/test_matcher.py, same as bloom_expected_entries",
    "score_decay": "tuned against the accuracy-table tests, not picked per deployment",
    "probable_score": (
        "the PROBABLE band threshold; only the WEAK and CONFIRMED edges are "
        "exposed as chart values today (see values.yaml matchEngine.minScore / "
        "confirmScore) -- add a chart value here first if a profile ever needs it"
    ),
    "max_candidates": "a safety cap on scoring work, not an accuracy or topology decision",
    "dedup_max_entries": "a memory-bound safety cap, not something a profile tunes",
    "redis_stream_key": "channel name is a code-level contract with consumers, not per-profile",
    "recent_alerts_size": "bounds a debug/admin ring buffer, not the system of record",
    "redis_detection_stream_key": (
        "channel name is a code-level contract with consumers (services/correlation), "
        "not per-profile -- same reasoning as redis_stream_key"
    ),
    "detection_stream_maxlen": (
        "a memory/retention safety cap on the higher-rate stream, not something a "
        "profile tunes -- same reasoning as dedup_max_entries"
    ),
}


def _chart_env_names() -> set[str]:
    text = _CHART.read_text()
    match = _BLOCK.search(text)
    assert match, f"could not find prahari.matchEngineEnv define block in {_CHART}"
    return set(_ENV_NAME.findall(match.group(1)))


def test_chart_sets_at_least_one_match_env_var():
    # A regex or chart-block-name change that silently stops matching anything
    # would make every assertion below vacuously true.
    assert _chart_env_names(), f"found no PRAHARI_MATCH_* names in {_CHART}"


def test_every_chart_env_name_maps_to_a_settings_field():
    fields = set(MatchSettings.model_fields)
    missing = []
    for name in _chart_env_names():
        field = name.removeprefix("PRAHARI_MATCH_").lower()
        if field not in fields:
            missing.append(name)

    assert not missing, (
        f"chart sets {missing} but MatchSettings has no matching field — "
        "this env var is a profile switch that silently does not switch"
    )


def test_every_settings_field_is_chart_exposed_or_deliberately_internal():
    chart_fields = {name.removeprefix("PRAHARI_MATCH_").lower() for name in _chart_env_names()}
    fields = set(MatchSettings.model_fields)

    unexplained = fields - chart_fields - set(_DELIBERATELY_INTERNAL)
    assert not unexplained, (
        f"MatchSettings field(s) {unexplained} are neither set by the chart nor "
        "listed in _DELIBERATELY_INTERNAL with a reason — a field a profile could "
        "plausibly want to override must be a conscious choice, not an omission "
        "(this is exactly how PRAHARI_MATCH_REDIS_URL went missing, M3)"
    )

    stale = set(_DELIBERATELY_INTERNAL) - fields
    assert not stale, f"_DELIBERATELY_INTERNAL names field(s) that no longer exist: {stale}"

    overlap = set(_DELIBERATELY_INTERNAL) & chart_fields
    assert not overlap, (
        f"field(s) {overlap} are both chart-exposed and listed as deliberately "
        "internal — remove them from _DELIBERATELY_INTERNAL"
    )
