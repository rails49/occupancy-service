# The occupancy page: node builds rails49/occupancy at OCCUPANCY_COMMIT, which
# compose.yaml names, and nginx serves what came out. The build context is the
# repository root.

FROM node:24-slim AS build

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

ARG OCCUPANCY_COMMIT
WORKDIR /occupancy
RUN git init -q . \
    && git fetch -q --depth 1 https://github.com/rails49/occupancy "$OCCUPANCY_COMMIT" \
    && git checkout -q FETCH_HEAD

# pnpm at the version package.json names.
RUN corepack enable && pnpm install --frozen-lockfile
RUN pnpm --filter @occupancy/ui build

FROM nginx:stable-alpine

# This repository's commit, which compose.yaml passes. It fixes OCCUPANCY_COMMIT.
# Empty for a build that names none.
ARG OCCUPANCY_SERVICE_COMMIT=
LABEL org.opencontainers.image.revision=$OCCUPANCY_SERVICE_COMMIT

COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /occupancy/ui/dist /usr/share/nginx/html
