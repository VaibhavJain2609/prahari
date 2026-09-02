"""§3 invariant: "A test asserts the field names match the chart. Parse
`templates/inference.yaml`, collect every `PRAHARI_DETECT_*` it sets, and
assert each maps to a real field."

Regex over the raw template rather than a YAML parser: the file is a Helm
template, not valid YAML — `{{ ... }}` appears in value position throughout —
and only the env var *names* matter here, not the templated values.

Deliberately one-directional. `DetectorSettings` may define a field the chart
never overrides (a sensible default with no need for a profile switch); what
must never happen is the reverse — the chart writing a name the settings class
does not read, which is a profile switch that silently does not switch.
"""

from __future__ import annotations

import re
from pathlib import Path

from prahari_inference.config import DetectorSettings

_CHART = Path(__file__).parents[3] / "infra" / "helm" / "prahari" / "templates" / "inference.yaml"
_ENV_NAME = re.compile(r"-\s*name:\s*(PRAHARI_DETECT_[A-Z0-9_]+)")


def _chart_env_names() -> set[str]:
    text = _CHART.read_text()
    return set(_ENV_NAME.findall(text))


def test_chart_sets_at_least_one_detect_env_var():
    # A regex change or a chart rewrite that silently stops matching anything
    # would make every assertion below vacuously true.
    assert _chart_env_names(), f"found no PRAHARI_DETECT_* names in {_CHART}"


def test_every_chart_env_name_maps_to_a_settings_field():
    fields = set(DetectorSettings.model_fields)
    missing = []
    for name in _chart_env_names():
        field = name.removeprefix("PRAHARI_DETECT_").lower()
        if field not in fields:
            missing.append(name)

    assert not missing, (
        f"chart sets {missing} but DetectorSettings has no matching field — "
        "this env var is a profile switch that silently does not switch"
    )
