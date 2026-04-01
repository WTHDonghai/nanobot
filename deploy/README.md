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

多架构构建时请注意：

- `PLATFORM=linux/amd64,linux/arm64` 不能和默认的本地 `--load` 一起使用
- 多架构镜像通常需要 `PUSH=1` 推送到镜像仓库，生成 manifest list
- 如果不想推仓库，可以用 `OUTPUT=type=oci,dest=...` 导出为 OCI 归档

示例：

```bash
# 推荐：直接推送 multi-arch 镜像
PLATFORM=linux/amd64,linux/arm64 \
PUSH=1 \
REGISTRY=registry.example.com/ \
./scripts/build-docker.sh 1.2.3

# 或导出为 OCI 归档
PLATFORM=linux/amd64,linux/arm64 \
OUTPUT=type=oci,dest=openviking-1.2.3.tar \
./scripts/build-docker.sh 1.2.3

# 如果只想在本机加载镜像，请只构建单架构
PLATFORM=linux/amd64 ./scripts/build-docker.sh 1.2.3
```

### 3. 部署

```bash
cd deploy

# 使用 docker-compose 部署（默认同时启动 OpenViking + Vikingbot gateway）
OPENVIKING_VERSION=1.2.3 docker-compose up -d
```

说明：

- `docker-compose.yml` 默认使用 `openviking-server --with-bot`
- Vikingbot gateway 运行在容器内部 `18790` 端口，通过 OpenViking 的 `/bot/v1/*` 代理对外提供服务
- bot 日志会写到持久化目录 `/app/data/bot/logs`
- 当 `ov.conf` 的 `server.host` 是 `0.0.0.0` 时，bot 会自动回连 `http://127.0.0.1:1933`

如果你不用 Compose，也可以直接这样启动单容器联动模式：

```bash
docker run -d \
  --name openviking \
  -p 1933:1933 \
  -e OPENVIKING_CONFIG_FILE=/app/ov.conf \
  -v /absolute/path/ov.conf:/app/ov.conf:ro \
  -v /data/openviking:/app/data \
  --restart unless-stopped \
  openviking:1.2.3 \
  openviking-server --with-bot --bot-log-dir /app/data/bot/logs
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
