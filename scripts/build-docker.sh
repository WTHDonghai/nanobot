#!/bin/bash
# Docker 镜像构建脚本 - 支持版本控制

set -e

# 默认版本：从 git tag 获取，如果没有 tag 则使用 commit hash
VERSION=${1:-$(git describe --tags --always --dirty 2>/dev/null || echo "dev")}
IMAGE_NAME="openviking"
REGISTRY=${REGISTRY:-""}  # 可选：私有镜像仓库前缀

echo "=========================================="
echo "Building Docker Image"
echo "Version: $VERSION"
echo "Image: ${REGISTRY}${IMAGE_NAME}:${VERSION}"
echo "=========================================="

# 构建镜像
docker build \
    --build-arg OPENVIKING_VERSION="${VERSION}" \
    -t "${IMAGE_NAME}:${VERSION}" \
    -t "${IMAGE_NAME}:latest" \
    .

# 如果有配置仓库，添加仓库标签
if [[ -n "$REGISTRY" ]]; then
    docker tag "${IMAGE_NAME}:${VERSION}" "${REGISTRY}${IMAGE_NAME}:${VERSION}"
    docker tag "${IMAGE_NAME}:latest" "${REGISTRY}${IMAGE_NAME}:latest"
    echo ""
    echo "Tagged for registry:"
    echo "  ${REGISTRY}${IMAGE_NAME}:${VERSION}"
    echo "  ${REGISTRY}${IMAGE_NAME}:latest"
fi

echo ""
echo "Build complete!"
echo ""
echo "To push to registry:"
echo "  docker push ${REGISTRY}${IMAGE_NAME}:${VERSION}"
echo "  docker push ${REGISTRY}${IMAGE_NAME}:latest"
