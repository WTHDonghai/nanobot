#!/bin/bash
# Docker 镜像构建脚本 - 支持版本控制

set -euo pipefail

# 默认版本：从 git tag 获取，如果没有 tag 则使用 commit hash
VERSION=${1:-$(git describe --tags --always --dirty 2>/dev/null || echo "dev")}
IMAGE_NAME=${IMAGE_NAME:-"openviking"}
REGISTRY=${REGISTRY:-""}  # 可选：私有镜像仓库前缀
DOCKERFILE=${DOCKERFILE:-"Dockerfile"}
BUILD_CONTEXT=${BUILD_CONTEXT:-"."}
BUILD_TARGET=${BUILD_TARGET:-""}
PLATFORM=${PLATFORM:-""}
CACHE_FROM=${CACHE_FROM:-""}
CACHE_TO=${CACHE_TO:-""}
BUILD_BASE_IMAGE=${BUILD_BASE_IMAGE:-""}
PY_DEPS_IMAGE=${PY_DEPS_IMAGE:-""}
ADMIN_DEPS_IMAGE=${ADMIN_DEPS_IMAGE:-""}
PUSH=${PUSH:-0}
LOAD=${LOAD:-""}

if [[ -z "$LOAD" ]]; then
    if [[ "$PUSH" == "1" ]]; then
        LOAD=0
    else
        LOAD=1
    fi
fi

VERSION_TAG="${REGISTRY}${IMAGE_NAME}:${VERSION}"
LATEST_TAG="${REGISTRY}${IMAGE_NAME}:latest"

echo "=========================================="
echo "Building Docker Image"
echo "Version: $VERSION"
echo "Image: ${VERSION_TAG}"
if [[ -n "$BUILD_TARGET" ]]; then
    echo "Target: ${BUILD_TARGET}"
fi
if [[ -n "$PLATFORM" ]]; then
    echo "Platform: ${PLATFORM}"
fi
if [[ -n "$BUILD_BASE_IMAGE" ]]; then
    echo "Build base image: ${BUILD_BASE_IMAGE}"
fi
if [[ -n "$PY_DEPS_IMAGE" ]]; then
    echo "Python deps image: ${PY_DEPS_IMAGE}"
fi
if [[ -n "$ADMIN_DEPS_IMAGE" ]]; then
    echo "Admin deps image: ${ADMIN_DEPS_IMAGE}"
fi
if [[ -n "$CACHE_FROM" ]]; then
    echo "Cache from: ${CACHE_FROM}"
fi
if [[ -n "$CACHE_TO" ]]; then
    echo "Cache to: ${CACHE_TO}"
fi
echo "=========================================="

build_cmd=(
    docker buildx build
    --file "${DOCKERFILE}"
    --build-arg "OPENVIKING_VERSION=${VERSION}"
    -t "${VERSION_TAG}"
    -t "${LATEST_TAG}"
)

if [[ -n "$BUILD_TARGET" ]]; then
    build_cmd+=(--target "${BUILD_TARGET}")
fi

if [[ -n "$PLATFORM" ]]; then
    build_cmd+=(--platform "${PLATFORM}")
fi

if [[ -n "$CACHE_FROM" ]]; then
    build_cmd+=(--cache-from "${CACHE_FROM}")
fi

if [[ -n "$CACHE_TO" ]]; then
    build_cmd+=(--cache-to "${CACHE_TO}")
fi

if [[ -n "$BUILD_BASE_IMAGE" ]]; then
    build_cmd+=(--build-arg "BUILD_BASE_IMAGE=${BUILD_BASE_IMAGE}")
fi

if [[ -n "$PY_DEPS_IMAGE" ]]; then
    build_cmd+=(--build-arg "PY_DEPS_IMAGE=${PY_DEPS_IMAGE}")
fi

if [[ -n "$ADMIN_DEPS_IMAGE" ]]; then
    build_cmd+=(--build-arg "ADMIN_DEPS_IMAGE=${ADMIN_DEPS_IMAGE}")
fi

if [[ "$PUSH" == "1" ]]; then
    build_cmd+=(--push)
elif [[ "$LOAD" == "1" ]]; then
    build_cmd+=(--load)
fi

build_cmd+=("${BUILD_CONTEXT}")

"${build_cmd[@]}"

echo ""
echo "Build complete!"
echo ""
if [[ "$PUSH" == "1" ]]; then
    echo "Images pushed:"
else
    echo "To push to registry:"
    echo "  docker push ${VERSION_TAG}"
    echo "  docker push ${LATEST_TAG}"
fi
echo "  ${VERSION_TAG}"
echo "  ${LATEST_TAG}"
