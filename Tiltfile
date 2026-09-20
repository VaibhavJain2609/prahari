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

# (service values key, image name, build deps that change the image, live_update)
SERVICES = [
    ('registry', 'prahari-registry', 'services/registry/'),
    ('matchEngine', 'prahari-match-engine', 'services/match-engine/'),
    ('correlation', 'prahari-correlation', 'services/correlation/'),
    ('bff', 'prahari-bff', 'services/bff/'),
    ('inference', 'prahari-inference', 'services/inference/'),
    ('web', 'prahari-web', 'web/'),
]

# Every first-party service gets a real image — not just the two that existed
# when this file was written. A service enabled in the values but unbuilt here
# ImagePullBackOffs against `dev`-tagged images that were never pushed.
for svc_key, image, src in SERVICES:
    deps = ['pyproject.toml', 'uv.lock', src]
    if svc_key != 'web':
        deps.append('packages/prahari-common/')
    if svc_key in ('match-engine', 'matchEngine', 'inference'):
        deps.append('packages/prahari-proto/')
    live = []
    if svc_key == 'registry':
        # Sync source without a rebuild — the dependency layer is the slow part
        # and does not change when a handler does. Migrations restart the
        # process (applied under advisory lock; re-running is a no-op, an edited
        # applied file is rejected by checksum).
        live = [
            sync('services/registry/src/', '/app/services/registry/src/'),
            sync('packages/prahari-common/src/', '/app/packages/prahari-common/src/'),
            run('kill -HUP 1', trigger=['services/registry/migrations/']),
        ]
    # inference gets no live_update on purpose: the worker holds open RTSP
    # connections and hot-swapping code under them leaves captures pointing at
    # unloaded modules. web rebuilds: Next dev-in-docker is slower than the
    # image build.
    docker_build(
        REGISTRY + '/' + image,
        context='.',
        dockerfile='services/{}/Dockerfile'.format(src.rstrip('/').split('/')[-1])
            if svc_key != 'web' else 'web/Dockerfile',
        only=deps,
        live_update=live,
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
    ],
    # The chart composes {registry}/prahari-{name}:{tag} from ONE shared pair —
    # a tuple-keyed image_keys mapping cannot express six different refs into
    # two keys (the last write wins). services.<key>.image is a full-ref
    # override in the chart for exactly this; image_json_paths sets each built
    # image's generated ref into its own service's key.
    image_deps=[REGISTRY + '/' + image for _, image, _ in SERVICES],
    image_json_paths=[
        '{.services.%s.image}' % svc_key for svc_key, _, _ in SERVICES
    ],
    port_forwards=[
        # Direct pod access for curl/debugging. Browser reachability comes from
        # the LoadBalancer service types the local profile sets (k3d's port
        # maps hit them); these forwards are the fallback that always works.
        port_forward(8000, 8000, name='registry'),
        port_forward(8080, 8080, name='bff'),
        port_forward(3000, 3000, name='web'),
        port_forward(8889, 8889, name='mediamtx-whep'),
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
