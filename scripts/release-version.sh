#!/bin/bash
# 版本发布脚本 - 创建 git tag 并构建对应版本的 Docker 镜像

set -e

VERSION=$1

if [[ -z "$VERSION" ]]; then
    echo "Usage: $0 <version>"
    echo "Example: $0 1.2.3"
    echo ""
    echo "Current tags:"
    git tag -l "v*" | sort -V | tail -10
    exit 1
fi

# 确保版本号格式正确
VERSION=$(echo "$VERSION" | sed 's/^v//')
TAG="v${VERSION}"

echo "=========================================="
echo "Release Version: ${VERSION}"
echo "Git Tag: ${TAG}"
echo "=========================================="
echo ""

# 检查工作区是否干净
if [[ -n $(git status --porcelain) ]]; then
    echo "Error: Working directory is not clean. Please commit changes first."
    git status -s
    exit 1
fi

# 创建 git tag
echo "Creating git tag: ${TAG}"
git tag -a "${TAG}" -m "Release ${TAG}"

echo ""
echo "Tag created successfully!"
echo ""
echo "To push the tag:"
echo "  git push origin ${TAG}"
echo ""
echo "To build Docker image with this version:"
echo "  ./build-docker.sh ${VERSION}"
