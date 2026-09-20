# The web console image.
#
# Built from the workspace ROOT, like the service images — not because web is a
# uv workspace member (it is not) but because the generated protobuf TypeScript
# stubs in `gen/ts/` live outside `web/` and are consumed directly by the
# Next.js build (see proto/buf.gen.yaml). `make proto` must run before this
# build on a fresh clone — `make images` already lists it as a prerequisite.
#
#   docker build -f web/Dockerfile .
#
# next.config.ts does NOT set `output: 'standalone'`, so there is no
# self-contained server bundle to copy — the runner stage below carries a
# production `node_modules` and runs `next start` instead. If standalone
# output is ever enabled, swap the runner for a copy of
# .next/standalone + .next/static and this stage gets much smaller.
#
# syntax=docker/dockerfile:1
FROM node:22-alpine AS deps

WORKDIR /app

# package-lock is copied with package.json so `npm ci` is reproducible — the
# lockfile is the contract, same discipline as uv.lock for the services.
COPY web/package.json web/package-lock.json ./
RUN npm ci


FROM node:22-alpine AS build

WORKDIR /app
COPY --from=deps /app/node_modules ./node_modules
COPY web/ ./
RUN npm run build


FROM node:22-alpine AS runner

WORKDIR /app

ENV NODE_ENV=production \
    PORT=3000 \
    HOSTNAME=0.0.0.0

# Production deps only. `npm ci --omit=dev` re-resolves from the lockfile, so
# the runner never sees eslint/typescript/tailwind toolchain packages.
COPY web/package.json web/package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

COPY --from=build /app/.next ./.next
COPY --from=build /app/public ./public
COPY --from=build /app/next.config.ts ./next.config.ts

# node:22-alpine ships a `node` user (uid 1000); the image runs unprivileged
# like the service images' uid-10001 `prahari` user.
USER node
EXPOSE 3000

# Exec form directly on the next binary, not `npm start`: npm would put itself
# between PID 1 and the server and eat SIGTERM, turning `kubectl rollout
# restart` into a 30 s grace-period wait on every pod.
CMD ["node_modules/.bin/next", "start"]
