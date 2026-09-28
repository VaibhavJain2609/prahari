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
# So this renders all profiles and asserts, in BOTH directions:
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
# eks renders with the registry injected — the same way `make eks-up` supplies
# it from terraform output — so assertions see real image refs, not the
# values-file placeholder.
helm template prahari "$CHART" --values "$CHART/values-eks.yaml" \
    --set global.imageRegistry=123456789012.dkr.ecr.ap-south-1.amazonaws.com \
    --set mediamtx.browserWhepBase=http://example.elb.amazonaws.com:8889 \
    >"$TMP/eks.yaml"

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

# Image refs are the CLUSTER's view of the registry. `localhost:5555` is the
# host-side push address — a pod that tries to pull it resolves localhost to
# itself and ImagePullBackOffs. Found by the first real `make up`.
hasnt "$TMP/local.yaml" 'image: localhost:' \
    "local: an image ref uses the host-side registry view — pods can't pull it"
hasnt "$TMP/gpu.yaml" 'image: localhost:' \
    "gpu: an image ref uses the host-side registry view — pods can't pull it"

# The renders must actually differ — a switch that changes nothing is off.
cmp -s "$TMP/local.yaml" "$TMP/gpu.yaml" \
    && fail "local and gpu render identically — the profile switch does nothing"

# --- eks: the cloud baseline must be a real, pullable deployment --------------

[ "$(env_val "$TMP/eks.yaml" PRAHARI_DETECT_DEVICE)" = "cpu" ] \
    || fail "eks: PRAHARI_DETECT_DEVICE is not cpu — the CPU node group can't run it"
[ "$(env_val "$TMP/eks.yaml" PRAHARI_DETECT_DECODE_BACKEND)" = "cpu" ] \
    || fail "eks: PRAHARI_DETECT_DECODE_BACKEND is not cpu — videotoolbox is macOS-only"
[ "$(env_val "$TMP/eks.yaml" PRAHARI_SESSION_COOKIE_SECURE)" = "false" ] \
    || fail "eks: Secure cookies over a plain-HTTP NLB break login — the override is lost"
has "$TMP/eks.yaml" 'provisioner: ebs.csi.aws.com' \
    "eks: gp3 StorageClass not rendered — PVCs would pend on the default class"
has "$TMP/eks.yaml" 'storageClassName: "gp3"' \
    "eks: claims not bound to gp3 — the storageClass knob is disconnected"
has "$TMP/eks.yaml" 'image: 123456789012.dkr.ecr.ap-south-1.amazonaws.com/prahari-registry:latest' \
    "eks: image refs are not composed from the injected ECR registry"
has "$TMP/eks.yaml" 'name: prahari-mediamtx-public' \
    "eks: the public media LB is missing"
hasnt "$TMP/eks.yaml" 'nvidia.com/gpu' \
    "eks: a GPU request leaked into the CPU-only profile"
hasnt "$TMP/eks.yaml" 'image: localhost:' \
    "eks: an image ref uses the host-side registry view — pods can't pull it"
hasnt "$TMP/eks.yaml" 'REPLACE_WITH' \
    "eks: the registry placeholder survived — global.imageRegistry was not injected"

# The public LB must expose ONLY the media ports — the control API on the
# internet is a reconfigure-the-restreamer button for anyone who finds it.
grep -A45 'name: prahari-mediamtx-public' "$TMP/eks.yaml" >"$TMP/mtx-public.yaml"
has "$TMP/mtx-public.yaml" 'port: 8554' "eks: public media LB missing rtsp"
has "$TMP/mtx-public.yaml" 'port: 8889' "eks: public media LB missing whep"
hasnt "$TMP/mtx-public.yaml" '9997' "eks: public media LB exposes the control API"
hasnt "$TMP/mtx-public.yaml" '9998' "eks: public media LB exposes metrics"

# The postgres NetworkPolicy must name every *_DATABASE_URL holder — under an
# enforcing CNI an omission here is a database outage that still looks green.
for caller in registry bff match-engine correlation; do
    grep -A25 'name: prahari-postgres-ingress' "$TMP/eks.yaml" \
        | grep -q "app.kubernetes.io/name: $caller" \
        || fail "eks: postgres NetworkPolicy omits $caller — it holds a database_url"
done

echo "all profiles render, and the switch switches"
