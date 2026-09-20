"""Chart↔settings parity for EVERY service, generalising
`services/match-engine/tests/test_match_settings.py` (which covers only the
`matchEngineEnv` block) and `services/inference/tests/test_detector_settings.py`
(only `PRAHARI_DETECT_*` in `inference.yaml`) to the whole chart:

    every `PRAHARI_*` env var the Helm chart emits for a deployment must
    resolve to a real field on one of that service's settings classes.

That is the hard invariant in CLAUDE.md: "a knob the chart writes and the
code never reads is a profile switch that silently does not switch." The
per-service tests each see one slice of the chart; the worst drift today is
in `commonEnv`, which no per-service test parses at all.

How env names are extracted: regex over the raw templates, same reasoning as
the per-service tests — these are Helm templates, not valid YAML, and only
the env *names* matter. `commonEnv` applies to every deployment that
`include`s it; each `if eq $name "..."` block in `services.yaml` applies to
that service only; `inference.yaml`'s inline env applies to the inference
worker. The field sets are built by introspecting each settings class's
`model_fields` + `env_prefix`, never hardcoded, so a new field or a renamed
prefix is picked up automatically.

`_KNOWN_DRIFT` is a RATCHET, not a waiver. It names exactly the env vars the
chart sets today that no settings class reads — the test asserts
`dead == _KNOWN_DRIFT`, so:

  * a NEW dead env (chart writes a name nothing reads) fails, and
  * a FIXED env (drift resolved by the chart-fix work landing in parallel)
    fails until its name is removed here.

The allowlist must only ever shrink, and must reach empty once the chart fix
lands — delete entries as they are fixed, add nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

from prahari_bff.config import BFFSettings
from prahari_common.config import GatewaySettings
from prahari_correlation.config import CorrelationSettings
from prahari_inference.config import DetectorSettings, IngestSettings
from prahari_match.config import MatchSettings
from prahari_registry.config import RegistrySettings
from pydantic_settings import BaseSettings

_TEMPLATES = Path(__file__).parents[1] / "infra" / "helm" / "prahari" / "templates"

_DEFINE_BLOCK = re.compile(
    r'\{\{-\s*define "prahari\.(\w+)"\s*-?\}\}(.*?)\{\{-\s*end\s*-?\}\}', re.DOTALL
)
_ENV_NAME = re.compile(r"-\s*name:\s*(PRAHARI_[A-Z0-9_]+)")
_SVC_NAMES_DICT = re.compile(r'\$svcNames\s*:=\s*dict\s+((?:"[^"]*"\s*)+)')
_QUOTED = re.compile(r'"([^"]*)"')
_CONDITIONAL_INCLUDE = re.compile(
    r'\{\{-\s*if\s+eq\s+\$name\s+"([^"]+)"\s*\}\}\s*\{\{-\s*include\s+"prahari\.(\w+)"'
)

# Deployments whose env is checked, mapped to every settings class that
# service actually reads. `env_prefix` is honoured per class: an env counts
# as read iff `env == prefix + field.upper()` for some field of some class
# listed here. (Checking every listed class, not just the first prefix that
# matches, is what lets PRAHARI_GATEWAY_HOST resolve to GatewaySettings.host
# on services whose own prefix is the wider PRAHARI_.)
#
# "web" is deliberately absent: it is a Next.js app with no pydantic
# settings class (its env contract is NEXT_PUBLIC_*), so there is nothing
# here for a PRAHARI_* name to map to. The web suite owns its own env checks.
_SERVICE_SETTINGS: dict[str, tuple[type[BaseSettings], ...]] = {
    "registry": (RegistrySettings, GatewaySettings),
    "match-engine": (MatchSettings,),
    "correlation": (CorrelationSettings,),
    "bff": (BFFSettings,),
    "inference": (IngestSettings, DetectorSettings, GatewaySettings),
}

# Templates that render third-party workloads (postgres, redis, mediamtx).
# Any PRAHARI_* env emitted there is dead by construction — none of those
# images is a PRAHARI service — so such names land in `dead` unconditionally
# under the file's own stem.
_THIRD_PARTY_TEMPLATES = ("infra.yaml", "mediamtx-config.yaml")

# The exact set of dead env names as of this branch, per deployment. See the
# module docstring: this must shrink to empty as the chart fix lands.
_KNOWN_DRIFT: dict[str, frozenset[str]] = {
    # commonEnv is emitted into every service-loop deployment. On the
    # registry itself only PRAHARI_DATABASE_URL resolves (PRAHARI_ +
    # `database_url`); the bus kind, Redis URL, registry URL, audit knobs and
    # the profile label are written into the pod but read by nothing.
    "registry": frozenset(
        {
            "PRAHARI_AUDIT_ENABLED",
            "PRAHARI_AUDIT_REQUIRE_PURPOSE",
            "PRAHARI_BUS_KIND",
            "PRAHARI_PROFILE",
            "PRAHARI_REDIS_URL",
            "PRAHARI_REGISTRY_URL",
        }
    ),
    # MatchSettings' prefix is PRAHARI_MATCH_, so ALL of commonEnv is dead on
    # the match engine — including PRAHARI_REDIS_URL, which is exactly why
    # `PRAHARI_MATCH_REDIS_URL` had to be added separately (M3's bug class).
    "match-engine": frozenset(
        {
            "PRAHARI_AUDIT_ENABLED",
            "PRAHARI_AUDIT_REQUIRE_PURPOSE",
            "PRAHARI_BUS_KIND",
            "PRAHARI_DATABASE_URL",
            "PRAHARI_PROFILE",
            "PRAHARI_REDIS_URL",
            "PRAHARI_REGISTRY_URL",
        }
    ),
    # The chart sets no PRAHARI_CORRELATION_* at all, so commonEnv is fully
    # dead here — and CorrelationSettings.redis_url stays None in every
    # deployed profile, meaning the detection consumer never starts. Same
    # silent-no-transport failure mode M3 found on the match engine.
    "correlation": frozenset(
        {
            "PRAHARI_AUDIT_ENABLED",
            "PRAHARI_AUDIT_REQUIRE_PURPOSE",
            "PRAHARI_BUS_KIND",
            "PRAHARI_DATABASE_URL",
            "PRAHARI_PROFILE",
            "PRAHARI_REDIS_URL",
            "PRAHARI_REGISTRY_URL",
        }
    ),
    # The BFF's prefix is PRAHARI_, so PRAHARI_DATABASE_URL and
    # PRAHARI_REDIS_URL do resolve (database_url, redis_url). The rest of
    # commonEnv does not — note PRAHARI_REGISTRY_URL in particular is dead:
    # the field is `registry_base_url` (PRAHARI_REGISTRY_BASE_URL), so the
    # BFF's registry address is not actually chart-set today.
    "bff": frozenset(
        {
            "PRAHARI_AUDIT_ENABLED",
            "PRAHARI_AUDIT_REQUIRE_PURPOSE",
            "PRAHARI_BUS_KIND",
            "PRAHARI_PROFILE",
            "PRAHARI_REGISTRY_URL",
        }
    ),
    # All of commonEnv is dead on the worker (its prefixes are
    # PRAHARI_INGEST_/PRAHARI_DETECT_/PRAHARI_GATEWAY_), plus the two
    # PRAHARI_MEDIAMTX_* base URLs inference.yaml sets directly — no service
    # reads a PRAHARI_MEDIAMTX_-prefixed settings class.
    "inference": frozenset(
        {
            "PRAHARI_AUDIT_ENABLED",
            "PRAHARI_AUDIT_REQUIRE_PURPOSE",
            "PRAHARI_BUS_KIND",
            "PRAHARI_DATABASE_URL",
            "PRAHARI_MEDIAMTX_HLS",
            "PRAHARI_MEDIAMTX_RTSP",
            "PRAHARI_PROFILE",
            "PRAHARI_REDIS_URL",
            "PRAHARI_REGISTRY_URL",
        }
    ),
}


def _read(template: str) -> str:
    return (_TEMPLATES / template).read_text()


def _env_helper_blocks() -> dict[str, set[str]]:
    """define-block name -> PRAHARI_* env names it emits, for every `*Env`
    helper in _helpers.tpl."""
    return {
        name: set(_ENV_NAME.findall(body))
        for name, body in _DEFINE_BLOCK.findall(_read("_helpers.tpl"))
        if name.endswith("Env")
    }


def _service_loop_names(services_yaml: str) -> list[str]:
    """Deployment names the services.yaml range emits — the values of the
    `$svcNames` dict (odd positions: key, value, key, value, ...)."""
    match = _SVC_NAMES_DICT.search(services_yaml)
    assert match, f"could not find the $svcNames dict in {_TEMPLATES / 'services.yaml'}"
    names = _QUOTED.findall(match.group(1))
    return names[1::2]


def _deployment_envs() -> dict[str, set[str]]:
    """deployment name -> every PRAHARI_* env the chart renders into it."""
    blocks = _env_helper_blocks()
    common = blocks["commonEnv"]
    services_yaml = _read("services.yaml")
    assert 'include "prahari.commonEnv"' in services_yaml, (
        "services.yaml no longer includes prahari.commonEnv — the common-env "
        "assumption this test is built on changed; re-derive _deployment_envs"
    )

    envs = {name: set(common) for name in _service_loop_names(services_yaml)}
    for service, block in _CONDITIONAL_INCLUDE.findall(services_yaml):
        envs.setdefault(service, set(common)).update(blocks[block])

    inference_yaml = _read("inference.yaml")
    assert 'include "prahari.commonEnv"' in inference_yaml, (
        "inference.yaml no longer includes prahari.commonEnv — see above"
    )
    envs["inference"] = set(common) | set(_ENV_NAME.findall(inference_yaml))
    return envs


def _covered_env_names(classes: tuple[type[BaseSettings], ...]) -> set[str]:
    """Every env name at least one of the classes would actually read."""
    covered: set[str] = set()
    for cls in classes:
        prefix = cls.model_config["env_prefix"]
        covered.update(prefix + field.upper() for field in cls.model_fields)
    return covered


def _dead_envs() -> dict[str, frozenset[str]]:
    """deployment -> chart-set env names no settings field reads."""
    dead: dict[str, frozenset[str]] = {}
    for deployment, env_names in _deployment_envs().items():
        classes = _SERVICE_SETTINGS.get(deployment)
        if classes is None:
            continue  # "web" — no settings class to check against; see above
        missing = env_names - _covered_env_names(classes)
        if missing:
            dead[deployment] = frozenset(missing)
    for template in _THIRD_PARTY_TEMPLATES:
        names = set(_ENV_NAME.findall(_read(template)))
        if names:
            dead[template.removesuffix(".yaml")] = frozenset(names)
    return dead


def test_every_env_helper_block_is_included_by_a_template():
    """A `*Env` define block no template includes would silently drop out of
    the parity check — the parse must prove each block is wired in."""
    blocks = _env_helper_blocks()
    assert blocks, "found no prahari.*Env define blocks in _helpers.tpl"
    included = {
        name
        for template in _TEMPLATES.glob("*.yaml")
        for name in re.findall(r'include "prahari\.(\w+)"', template.read_text())
    }
    orphan = set(blocks) - included
    assert not orphan, f"env helper block(s) never included by any template: {orphan}"


def test_every_deployment_sets_at_least_one_env_and_is_classified():
    """Non-vacuity + no unclassified deployment: a renamed service or a new
    Deployment must not fall out of the parity check unnoticed."""
    envs = _deployment_envs()
    assert envs, "parsed no deployments at all — the template parse is vacuous"
    for deployment, names in envs.items():
        assert names, f"deployment {deployment} renders no PRAHARI_* env — parse is vacuous"
    unclassified = set(envs) - set(_SERVICE_SETTINGS) - {"web"}
    assert not unclassified, (
        f"deployment(s) {unclassified} have PRAHARI_* env but no settings-class "
        "mapping in _SERVICE_SETTINGS — classify them there (or 'web'-exempt them "
        "with a comment) so the parity check applies"
    )
    missing = set(_SERVICE_SETTINGS) - set(envs)
    assert not missing, (
        f"_SERVICE_SETTINGS names deployment(s) {missing} that the chart no "
        "longer renders — remove them"
    )


def test_chart_env_drift_is_exactly_the_known_set():
    """The ratchet: `dead == _KNOWN_DRIFT`, both directions.

    New dead env -> the chart wrote a switch nothing reads; fix the chart or
    the settings, not this list. A name the fix made real -> delete it from
    _KNOWN_DRIFT so the ratchet holds at the smaller set.
    """
    dead = _dead_envs()
    if dead != _KNOWN_DRIFT:
        new = {
            dep: sorted(names - _KNOWN_DRIFT.get(dep, frozenset()))
            for dep, names in dead.items()
            if names - _KNOWN_DRIFT.get(dep, frozenset())
        }
        fixed = {
            dep: sorted(names - dead.get(dep, frozenset()))
            for dep, names in _KNOWN_DRIFT.items()
            if names - dead.get(dep, frozenset())
        }
        raise AssertionError(
            "chart/settings drift changed.\n"
            f"  NEW dead envs (chart sets, nothing reads — fix the chart or the "
            f"settings class, not this list): {new}\n"
            f"  RESOLVED drift (now maps to a field — delete from _KNOWN_DRIFT "
            f"so the ratchet holds): {fixed}"
        )
