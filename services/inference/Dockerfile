# The inference worker image.
#
# Built from the workspace root — see services/registry/Dockerfile for why.
# Also depends on packages/prahari-proto (the gRPC stubs for the
# MetadataIngestService link to the match engine), same as
# services/match-engine/Dockerfile.
#
#   docker build -f services/inference/Dockerfile .
#
# This is the CPU/MPS image used by `profile=local`. The gpu profile swaps the
# base for a CUDA runtime and nothing else; if a cutover ever needs a change in
# the application layer, the switch is wrong.
#
# syntax=docker/dockerfile:1
FROM python:3.12-slim AS build

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY packages/prahari-common/pyproject.toml packages/prahari-common/
COPY packages/prahari-proto/pyproject.toml packages/prahari-proto/
COPY services/inference/pyproject.toml services/inference/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package prahari-inference

COPY packages/prahari-common/ packages/prahari-common/
COPY packages/prahari-proto/ packages/prahari-proto/
COPY services/inference/ services/inference/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package prahari-inference


FROM python:3.12-slim AS runtime

# FFmpeg's shared libraries, which opencv-python-headless links against for the
# RTSP/HLS demuxers. `-headless` deliberately: this worker never opens a window,
# and the GUI build drags in an X stack we would then have to justify.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 prahari
WORKDIR /app

COPY --from=build --chown=prahari:prahari /app/.venv /app/.venv
COPY --from=build --chown=prahari:prahari /app/packages/prahari-common /app/packages/prahari-common
COPY --from=build --chown=prahari:prahari /app/packages/prahari-proto /app/packages/prahari-proto
COPY --from=build --chown=prahari:prahari /app/services/inference /app/services/inference

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # RTSP over TCP, enforced at the FFmpeg layer rather than trusted to a call
    # site. rtsp_env.py sets this too; having it in the image means a process
    # started any other way still gets it. UDP transport drops silently under
    # load and produces corrupt frames that look like detections.
    OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;tcp"

USER prahari

CMD ["python", "-m", "prahari_inference"]
