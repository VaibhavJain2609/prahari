#!/usr/bin/env bash
# make verify — the profile switch, asserted.
#
# `helm template` succeeding on both profiles proves the templates parse. It
# does NOT prove `profile` switches anything — a values file whose gpu keys
# nothing reads renders green while scheduling a CPU image on the GPU node.
# That is the same failure class as a dead env knob, one level up: the switch
# looks applied, is not, and the number it was meant to change ends up on a
# slide.
#
# So this renders both profiles and asserts, in BOTH directions:
#   * the gpu-only machinery appears under profile=gpu (cuda device, nvidia
#     runtime + resource, ScaledObject, secure cookies, mandatory internal
#     Secret), and is absent under profile=local — a leaked GPU setting on the
#     laptop profile breaks `make up` on a machine with no GPU;
#   * the shared substrate is IDENTICAL in both renders (postgres/redis/
#     mediamtx images, the database DSN shape, the watchlist path) — the
#     "profile is the ONLY thing that differs" direction of the invariant.
#
# Assertions are semantic greps over the rendered YAML, not a line diff: a
# diff whitelist rots every time a values comment moves.
set -euo pipefail

CHART="${CHART:-infra/helm/prahari}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

helm template prahari "$CHART" --values "$CHART/values-local.yaml" >"$TMP/local.yaml"
helm template prahari "$CHART" --values "$CHART/values-gpu.yaml" >"$TMP/gpu.yaml"

fail() { echo "verify: $*" >&2; exit 1; }

# env_val <render> <ENV_NAME> -> the env's `value:` with quotes stripped.
env_val() {
    awk -v name="$2" '
        $0 ~ "- name: " name "$" { want=1; next }
        want && /value:/ { sub(/.*value:[ ]*/, ""); gsub(/"/, ""); print; exit }
        want && /^[[:space:]]*- name:/ { exit }
    ' "$1"
}

has() { grep -q "$2" "$1" || fail "$3"; }
hasnt() { ! grep -q "$2" "$1" || fail "$3"; }

# --- local: the laptop profile must be genuinely CPU-viable -----------------

[ "$(env_val "$TMP/local.yaml" PRAHARI_DETECT_DEVICE)" = "cpu" ] \
    || fail "local: PRAHARI_DETECT_DEVICE is not cpu"
[ "$(env_val "$TMP/local.yaml" PRAHARI_DETECT_DECODE_BACKEND)" = "videotoolbox" ] \
    || fail "local: PRAHARI_DETECT_DECODE_BACKEND is not videotoolbox"
[ "$(env_val "$TMP/local.yaml" PRAHARI_SESSION_COOKIE_SECURE)" = "false" ] \
    || fail "local: session cookies must not be Secure over plain-HTTP k3d"
hasnt "$TMP/local.yaml" 'runtimeClassName: nvidia' \
    "local: nvidia runtimeClass leaked into the CPU profile"
hasnt "$TMP/local.yaml" 'nvidia.com/gpu' \
    "local: GPU resource request leaked into the CPU profile"
hasnt "$TMP/local.yaml" 'kind: ScaledObject' \
    "local: KEDA rendered without a CRD-bearing cluster — install would fail"

# --- gpu: the switch must visibly switch -------------------------------------

[ "$(env_val "$TMP/gpu.yaml" PRAHARI_DETECT_DEVICE)" = "cuda" ] \
    || fail "gpu: PRAHARI_DETECT_DEVICE is not cuda — the profile switch is dead"
[ "$(env_val "$TMP/gpu.yaml" PRAHARI_DETECT_DECODE_BACKEND)" = "nvdec" ] \
    || fail "gpu: PRAHARI_DETECT_DECODE_BACKEND is not nvdec"
[ "$(env_val "$TMP/gpu.yaml" PRAHARI_SESSION_COOKIE_SECURE)" = "true" ] \
    || fail "gpu: session cookies must be Secure behind TLS"
has "$TMP/gpu.yaml" 'runtimeClassName: nvidia' \
    "gpu: inference pod lacks the nvidia runtimeClass"
has "$TMP/gpu.yaml" 'nvidia.com/gpu' \
    "gpu: inference pod requests no GPU"
has "$TMP/gpu.yaml" 'kind: ScaledObject' \
    "gpu: KEDA ScaledObject missing — the scale-up demo beat is gone"

# security.internalSecretRequired: the prahari-internal secretKeyRefs must be
# optional locally (enforcement-off dev mode) and mandatory under gpu (a
# missing Secret fails the pod rather than silently disarming every gate).
grep -A4 'key: internal-token' "$TMP/gpu.yaml" | grep -q 'optional: false' \
    || fail "gpu: prahari-internal refs are still optional — a missing Secret disarms every gate"
grep -A4 'key: internal-token' "$TMP/local.yaml" | grep -q 'optional: true' \
    || fail "local: prahari-internal refs turned mandatory — plain make up now needs the Secret"

# --- both: the substrate must NOT differ -------------------------------------

for image in 'postgis/postgis:16-3.4' 'redis:7.4-alpine3.21' 'bluenviron/mediamtx:1.9.3'; do
    has "$TMP/local.yaml" "$image" "local: $image missing"
    has "$TMP/gpu.yaml" "$image" "gpu: $image missing — the shared substrate drifted per-profile"
done

# Same DSN shape in both — only the Secret carries the password difference.
[ "$(env_val "$TMP/local.yaml" PRAHARI_DATABASE_URL)" = "$(env_val "$TMP/gpu.yaml" PRAHARI_DATABASE_URL)" ] \
    || fail "PRAHARI_DATABASE_URL differs across profiles — only secrets may differ"
[ "$(env_val "$TMP/local.yaml" PRAHARI_MATCH_WATCHLIST_DIR)" = "$(env_val "$TMP/gpu.yaml" PRAHARI_MATCH_WATCHLIST_DIR)" ] \
    || fail "PRAHARI_MATCH_WATCHLIST_DIR differs across profiles"

# The renders must actually differ — a switch that changes nothing is off.
cmp -s "$TMP/local.yaml" "$TMP/gpu.yaml" \
    && fail "local and gpu render identically — the profile switch does nothing"

echo "both profiles render, and the switch switches"
