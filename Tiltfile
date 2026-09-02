# The inner dev loop, against the same k3d cluster and the same Helm chart that
# `make up` installs.
#
# The rule this file exists to protect: Helm and Terraform in infra/ are the ONLY
# source of truth. Tilt does not apply YAML of its own, does not patch a
# Deployment, and does not maintain a parallel "dev" configuration. It renders
# THIS chart with THESE values and swaps in a locally built image. Anything you
# see running under Tilt is something `helm install` produces.
#
#   tilt up          watch, rebuild, live-update
#   tilt down        remove the release
#
# Prerequisite: `make cluster` (k3d, with its local registry on :5555).

load('ext://helm_resource', 'helm_resource')

PROFILE = os.getenv('PROFILE', 'local')
NAMESPACE = 'prahari'
CHART = 'infra/helm/prahari'
REGISTRY = 'localhost:5555'

allow_k8s_contexts('k3d-prahari')

# --- images ------------------------------------------------------------------
#
# Both build from the workspace ROOT, not from the service directory: each
# service depends on packages/prahari-common as a uv workspace member, which
# would sit outside a narrower build context.

docker_build(
    REGISTRY + '/prahari-registry',
    context='.',
    dockerfile='services/registry/Dockerfile',
    only=[
        'pyproject.toml',
        'uv.lock',
        'packages/prahari-common/',
        'services/registry/',
    ],
    # Sync source without a rebuild. The dependency layer is the slow one and it
    # does not change when a handler does.
    live_update=[
        sync('services/registry/src/', '/app/services/registry/src/'),
        sync('packages/prahari-common/src/', '/app/packages/prahari-common/src/'),
        # Migrations are applied on startup under an advisory lock, so a new .sql
        # file needs the process restarted, not just synced. Restarting is the
        # correct response — re-running an already-applied migration is a no-op,
        # and an edited applied migration is rejected by checksum rather than
        # silently ignored.
        run('kill -HUP 1', trigger=['services/registry/migrations/']),
    ],
)

docker_build(
    REGISTRY + '/prahari-inference',
    context='.',
    dockerfile='services/inference/Dockerfile',
    only=[
        'pyproject.toml',
        'uv.lock',
        'packages/prahari-common/',
        'services/inference/',
    ],
    # No live_update. The worker holds open RTSP connections; hot-swapping code
    # underneath them leaves captures pointing at unloaded modules. A rebuild is
    # slower and honest — and every reconnect is exercised, which is the one path
    # that has never met a real disconnect.
)

# --- the release -------------------------------------------------------------

helm_resource(
    'prahari',
    CHART,
    namespace=NAMESPACE,
    flags=[
        '--create-namespace',
        '--values', CHART + '/values-' + PROFILE + '.yaml',
        '--set', 'profile=' + PROFILE,
        '--set', 'global.imageTag=tilt',
    ],
    image_deps=[
        REGISTRY + '/prahari-registry',
        REGISTRY + '/prahari-inference',
    ],
    image_keys=[
        ('global.imageRegistry', 'global.imageTag'),
        ('global.imageRegistry', 'global.imageTag'),
    ],
    port_forwards=[
        # The registry API and its OpenAPI page. The BFF fronts this for the
        # browser; direct access is for curl and for the map before the console
        # exists.
        port_forward(8000, 8000, name='registry'),
    ],
    labels=['platform'],
)

# --- local checks ------------------------------------------------------------
#
# Manual triggers, not auto-run: a test suite that fires on every keystroke
# trains you to ignore it.

local_resource(
    'test',
    cmd='uv run pytest -q',
    deps=['services', 'packages'],
    labels=['checks'],
    trigger_mode=TRIGGER_MODE_MANUAL,
    auto_init=False,
)

local_resource(
    'lint',
    cmd='uv run ruff check . && uv run ruff format --check .',
    deps=['services', 'packages'],
    labels=['checks'],
    trigger_mode=TRIGGER_MODE_MANUAL,
    auto_init=False,
)

local_resource(
    'chart-verify',
    # Renders both profiles. The claim that `profile` is the only difference
    # between the laptop and the cloud is only true while this passes.
    cmd='make verify',
    deps=['infra/helm'],
    labels=['checks'],
    trigger_mode=TRIGGER_MODE_MANUAL,
    auto_init=False,
)

local_resource(
    'sync-catalogue',
    # The demo beat: one call onboards the whole estate. Manual so it is a thing
    # you press, not something that happens while you are explaining it.
    cmd='curl -fsS -X POST localhost:8000/api/v1/sync | head -c 400',
    labels=['ops'],
    trigger_mode=TRIGGER_MODE_MANUAL,
    auto_init=False,
)
