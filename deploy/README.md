# Docker 部署版本控制

本分支包含 Docker 镜像构建和版本控制的相关配置。

## 目录结构

```
.
├── Dockerfile              # 主构建文件
├── deploy/
│   └── docker-compose.yml  # Docker Compose 部署配置
├── scripts/
│   ├── build-docker.sh     # 镜像构建脚本
│   └── release-version.sh  # 版本发布脚本
└── README.md               # 本文件
```

## 版本控制流程

### 1. 创建版本发布

```bash
# 创建新版本（例如 1.2.3）
./scripts/release-version.sh 1.2.3

# 推送 tag
git push origin v1.2.3
```

### 2. 构建镜像

```bash
# 使用当前 git tag 作为版本
./scripts/build-docker.sh

# 指定版本构建
./scripts/build-docker.sh 1.2.3

# 使用私有仓库
REGISTRY=registry.example.com/ ./scripts/build-docker.sh 1.2.3
```

### 2.1 基础镜像与依赖缓存

当前 `Dockerfile` 已拆成可复用的缓存层：

- `build-base`：Go/Rust/uv/系统编译依赖
- `py-deps`：基于 `uv.lock` 的 Python 依赖环境
- `admin-deps`：基于 `admin/package-lock.json` 的前端依赖

可以先单独构建这些层，再在正式构建时复用：

```bash
# 预构建工具链基础镜像
IMAGE_NAME=openviking-build-base BUILD_TARGET=build-base ./scripts/build-docker.sh 2026.03

# 预构建 Python 依赖镜像
IMAGE_NAME=openviking-py-deps BUILD_TARGET=py-deps ./scripts/build-docker.sh uvlock-20260331

# 预构建前端依赖镜像
IMAGE_NAME=openviking-admin-deps BUILD_TARGET=admin-deps ./scripts/build-docker.sh npmlock-20260331
```

正式构建时，可将这些镜像作为基础层透传给 `buildx`：

```bash
BUILD_BASE_IMAGE=registry.example.com/openviking-build-base:2026.03 \
PY_DEPS_IMAGE=registry.example.com/openviking-py-deps:uvlock-20260331 \
ADMIN_DEPS_IMAGE=registry.example.com/openviking-admin-deps:npmlock-20260331 \
REGISTRY=registry.example.com/ \
./scripts/build-docker.sh 1.2.3
```

如果是 CI/CD 多机环境，建议同时开启 BuildKit 远程缓存：

```bash
CACHE_FROM=type=registry,ref=registry.example.com/openviking:buildcache \
CACHE_TO=type=registry,ref=registry.example.com/openviking:buildcache,mode=max \
REGISTRY=registry.example.com/ \
PUSH=1 \
./scripts/build-docker.sh 1.2.3
```

### 3. 部署

```bash
cd deploy

# 使用 docker-compose 部署
OPENVIKING_VERSION=1.2.3 docker-compose up -d
```

## 镜像标签规范

| 标签 | 说明 |
|------|------|
| `latest` | 最新版本（默认）|
| `v1.2.3` | 具体版本号 |
| `v1.2` | 次要版本（指向最新 patch）|
| `dev` | 开发版本（当前分支）|

## CI/CD 集成

GitHub Actions 示例：

```yaml
- name: Build and Push Docker Image
  run: |
    VERSION=${{ github.ref_name }}
    ./scripts/build-docker.sh ${VERSION}
    docker push openviking:${VERSION}
```
