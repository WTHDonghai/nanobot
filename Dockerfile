# syntax=docker/dockerfile:1.9

ARG GO_TOOLCHAIN_IMAGE=golang:1.26-trixie
ARG RUST_TOOLCHAIN_IMAGE=rust:1.88-trixie
ARG NODE_BUILD_IMAGE=node:22-slim
ARG UV_BUILD_IMAGE=ghcr.io/astral-sh/uv:python3.13-trixie-slim
ARG PYTHON_RUNTIME_IMAGE=python:3.13-slim-trixie
ARG BUILD_BASE_IMAGE=build-base-source
ARG PY_DEPS_IMAGE=py-deps-source
ARG BOT_PY_DEPS_IMAGE=bot-py-deps-source
ARG ADMIN_DEPS_IMAGE=admin-deps-source

# Stage 1: provide Go toolchain (required by setup.py -> build_agfs_artifacts -> make build)
FROM ${GO_TOOLCHAIN_IMAGE} AS go-toolchain

# Stage 2: provide Rust toolchain (required by setup.py -> build_ov_cli_artifact -> cargo build)
FROM ${RUST_TOOLCHAIN_IMAGE} AS rust-toolchain

# Stage 3: install Admin Panel dependencies from lockfile only.
FROM ${NODE_BUILD_IMAGE} AS admin-deps-source

ARG TARGETPLATFORM

WORKDIR /admin

COPY admin/package.json admin/package-lock.json ./

RUN --mount=type=cache,target=/root/.npm,id=npm-${TARGETPLATFORM} \
    npm ci

# Stage 4: optionally allow a prebuilt Admin dependency image.
FROM ${ADMIN_DEPS_IMAGE} AS admin-deps

# Stage 5: build Admin Panel assets from source.
FROM admin-deps AS admin-builder

COPY admin/index.html admin/tsconfig.json admin/vite.config.ts ./
COPY admin/src ./src

RUN npm run build

# Stage 6: provide Python build toolchain and system dependencies.
FROM ${UV_BUILD_IMAGE} AS build-base-source

# Reuse Go toolchain from stage 1 so setup.py can compile agfs-server in-place.
COPY --from=go-toolchain /usr/local/go /usr/local/go
# Reuse Rust toolchain from stage 2 so setup.py can compile ov CLI in-place.
COPY --from=rust-toolchain /usr/local/cargo /usr/local/cargo
COPY --from=rust-toolchain /usr/local/rustup /usr/local/rustup
ENV CARGO_HOME=/usr/local/cargo
ENV RUSTUP_HOME=/usr/local/rustup
ENV PATH="/usr/local/cargo/bin:/usr/local/go/bin:${PATH}"
ARG OPENVIKING_VERSION=0.0.0
ARG TARGETPLATFORM
ARG UV_LOCK_STRATEGY=auto
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_OPENVIKING=${OPENVIKING_VERSION}

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    cmake \
    git \
 && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV UV_NO_DEV=1
WORKDIR /app

# Stage 7: optionally allow a prebuilt build base image with toolchains.
FROM ${BUILD_BASE_IMAGE} AS build-base

ENV CARGO_HOME=/usr/local/cargo
ENV RUSTUP_HOME=/usr/local/rustup
ENV PATH="/usr/local/cargo/bin:/usr/local/go/bin:${PATH}"
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV UV_NO_DEV=1
WORKDIR /app

# Stage 8: install OpenViking server dependencies from lockfile without copying application code.
FROM build-base AS py-deps-source

ARG TARGETPLATFORM

COPY Cargo.toml Cargo.lock ./
COPY pyproject.toml uv.lock setup.py README.md ./
COPY third_party/agfs/agfs-sdk/python third_party/agfs/agfs-sdk/python

RUN --mount=type=cache,target=/root/.cache/uv,id=uv-${TARGETPLATFORM} \
    uv sync --locked --no-install-project --no-editable

# Stage 9: optionally allow a prebuilt OpenViking server dependency image.
FROM ${PY_DEPS_IMAGE} AS py-deps

# Stage 10: install Vikingbot dependencies from lockfile without copying application code.
FROM build-base AS bot-py-deps-source

ARG TARGETPLATFORM

COPY Cargo.toml Cargo.lock ./
COPY pyproject.toml uv.lock setup.py README.md ./
COPY third_party/agfs/agfs-sdk/python third_party/agfs/agfs-sdk/python

RUN --mount=type=cache,target=/root/.cache/uv,id=uv-${TARGETPLATFORM} \
    uv sync --locked --no-install-project --no-editable --extra bot --extra bot-dingtalk

# Stage 11: optionally allow a prebuilt Vikingbot dependency image.
FROM ${BOT_PY_DEPS_IMAGE} AS bot-py-deps

# Stage 12: install OpenViking server project code on top of the cached dependency environment.
FROM build-base AS py-builder

ARG OPENVIKING_VERSION=0.0.0
ARG TARGETPLATFORM

ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_OPENVIKING=${OPENVIKING_VERSION}

COPY --from=py-deps /app/.venv /app/.venv

# Copy source required for setup.py artifact builds and native extension build.
COPY Cargo.toml Cargo.lock ./
COPY pyproject.toml uv.lock setup.py README.md ./
COPY build_support/ build_support/
COPY bot/ bot/
COPY crates/ crates/
COPY openviking/ openviking/
COPY openviking_cli/ openviking_cli/
COPY src/ src/
COPY third_party/ third_party/
COPY bot/ bot/

# Install project and dependencies (triggers setup.py artifact builds + build_extension).
# Default to auto-refreshing uv.lock inside the ephemeral build context when it is
# stale, so Docker builds stay unblocked after dependency changes. Set
# UV_LOCK_STRATEGY=locked to keep fail-fast reproducibility checks.
RUN --mount=type=cache,target=/root/.cache/uv,id=uv-${TARGETPLATFORM} \
    case "${UV_LOCK_STRATEGY}" in \
        locked) \
            uv sync --locked --no-editable --extra bot \
            ;; \
        auto) \
            if ! uv lock --check; then \
                uv lock; \
            fi; \
            uv sync --locked --no-editable --extra bot \
            ;; \
        *) \
            echo "Unsupported UV_LOCK_STRATEGY: ${UV_LOCK_STRATEGY}" >&2; \
            exit 2 \
            ;; \
    esac

# Stage 13: install Vikingbot project code on top of the cached bot dependency environment.
FROM build-base AS bot-py-builder

ARG OPENVIKING_VERSION=0.0.0
ARG TARGETPLATFORM

ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_OPENVIKING=${OPENVIKING_VERSION}

COPY --from=bot-py-deps /app/.venv /app/.venv

COPY Cargo.toml Cargo.lock ./
COPY pyproject.toml uv.lock setup.py README.md ./
COPY build_support/ build_support/
COPY crates/ crates/
COPY openviking/ openviking/
COPY openviking_cli/ openviking_cli/
COPY src/ src/
COPY third_party/ third_party/
COPY bot/ bot/

RUN --mount=type=cache,target=/root/.cache/uv,id=uv-${TARGETPLATFORM} \
    uv sync --locked --no-editable --extra bot --extra bot-dingtalk

# Stage 14: shared runtime base
FROM ${PYTHON_RUNTIME_IMAGE} AS runtime-base

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libstdc++6 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

ENV PATH="/app/.venv/bin:$PATH"
ENV OPENVIKING_CONFIG_FILE="/app/ov.conf"

# Stage 15: Vikingbot runtime image
FROM runtime-base AS bot-runtime

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:18790/bot/v1/health || exit 1

COPY --from=bot-py-builder /app/.venv /app/.venv

EXPOSE 18790

CMD ["vikingbot", "gateway", "--config", "/app/ov.conf"]

# Stage 16: OpenViking server runtime image (default final target)
FROM runtime-base AS server-runtime

COPY --from=py-builder /app/.venv /app/.venv
COPY --from=admin-builder /admin/dist /app/admin/dist
COPY docker/openviking-console-entrypoint.sh /usr/local/bin/openviking-console-entrypoint
RUN chmod +x /usr/local/bin/openviking-console-entrypoint

EXPOSE 1933 8020

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS http://127.0.0.1:1933/health || exit 1

# Default runs server + console; override command to run CLI, e.g.:
# docker run --rm <image> -v "$HOME/.openviking/ovcli.conf:/root/.openviking/ovcli.conf" openviking --help
ENTRYPOINT ["openviking-console-entrypoint"]
