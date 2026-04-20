# Docker Deployment

当前部署目录已经切换为双镜像模式：

- `openviking-server` 镜像负责 HTTP API、`/admin` 和 `/guest`
- `vikingbot` 镜像负责 bot gateway
- 两个容器共享同一份 `ov.conf` 和 `/app/data`

## 目录

```text
.
├── Dockerfile
├── deploy/
│   ├── docker-compose.yml
│   ├── ov.conf.example
│   └── README.md
└── scripts/
    └── build-docker.sh
```

## 构建镜像

默认 `Dockerfile` 提供两个运行目标：

- `server-runtime`
- `bot-runtime`

部署到 `linux/amd64` 机器时，依赖镜像和最终运行镜像也都必须是 `linux/amd64`。

- `server-runtime` 依赖 `BUILD_BASE_IMAGE`、`PY_DEPS_IMAGE`、`ADMIN_DEPS_IMAGE`
- `bot-runtime` 依赖 `BUILD_BASE_IMAGE`、`BOT_PY_DEPS_IMAGE`

如果本地没有对应 tag，或者 tag 的架构不是 `linux/amd64`，BuildKit 会尝试去远端拉取这个 tag，通常会报 `pull access denied`。

## 依赖缓存层

当前 `Dockerfile` 保留了几层可复用镜像：

- `build-base`
- `py-deps`
- `bot-py-deps`
- `admin-deps`

### 第一次准备

第一次建议按下面顺序构建依赖镜像。

1. 构建基础工具链镜像 `build-base`
2. 构建 `server-runtime` 需要的 Python 依赖镜像 `py-deps`
3. 构建 `server-runtime` 需要的前端依赖镜像 `admin-deps`
4. 如果还要构建 `vikingbot`，再构建 `bot-py-deps`

```bash
PLATFORM=linux/amd64 \
IMAGE_NAME=openviking-build-base \
BUILD_TARGET=build-base \
./scripts/build-docker.sh 2026.03
```

```bash
PLATFORM=linux/amd64 \
BUILD_BASE_IMAGE=openviking-build-base:2026.03 \
IMAGE_NAME=openviking-py-deps \
BUILD_TARGET=py-deps \
./scripts/build-docker.sh uvlock-server-20260401
```

```bash
PLATFORM=linux/amd64 \
IMAGE_NAME=openviking-admin-deps \
BUILD_TARGET=admin-deps \
./scripts/build-docker.sh npmlock-20260401
```

```bash
PLATFORM=linux/amd64 \
BUILD_BASE_IMAGE=openviking-build-base:2026.03 \
IMAGE_NAME=vikingbot-py-deps \
BUILD_TARGET=bot-py-deps \
./scripts/build-docker.sh uvlock-bot-20260401
```

### 构建 `openviking-server`

```bash
PLATFORM=linux/amd64 \
BUILD_BASE_IMAGE=openviking-build-base:2026.03 \
PY_DEPS_IMAGE=openviking-py-deps:uvlock-server-20260401 \
ADMIN_DEPS_IMAGE=openviking-admin-deps:npmlock-20260401 \
IMAGE_NAME=openviking-server \
BUILD_TARGET=server-runtime \
./scripts/build-docker.sh 1.1.2.beta_2
```

### 构建 `vikingbot`

```bash
PLATFORM=linux/amd64 \
BUILD_BASE_IMAGE=openviking-build-base:2026.03 \
BOT_PY_DEPS_IMAGE=vikingbot-py-deps:uvlock-bot-20260401 \
IMAGE_NAME=vikingbot \
BUILD_TARGET=bot-runtime \
./scripts/build-docker.sh 1.1.2.beta_2
```

### 日常发版

依赖镜像准备好之后，后续发布 `1.0.5`、`1.0.6` 这类新版本时，通常只需要重新构建最终运行镜像，不需要每次都先重建依赖镜像。

什么时候需要重建依赖镜像：

- `build-base`：基础工具链或基础镜像发生变化时
- `py-deps` / `bot-py-deps`：`uv.lock`、`pyproject.toml`、`setup.py`、`Cargo.lock` 或 Python 依赖相关内容变化时
- `admin-deps`：`admin/package-lock.json` 变化时

### 缓存预热

`Dockerfile` 已经给 `py-builder` / `bot-py-builder` 增加了 `uv`、Go、Cargo、CMake 的 BuildKit cache mount。第一次正式构建会顺带把缓存灌满，后续构建会明显更快。

平时不需要每次发版都先跑 `LOAD=0`。只有在下面这些场景，才建议单独预热：

- 想先灌缓存，但暂时不需要导入最终镜像
- 怀疑 builder 缓存已经丢失
- CI 里想把“预热构建”和“正式导出镜像”拆开

示例：

```bash
LOAD=0 \
PLATFORM=linux/amd64 \
BUILD_BASE_IMAGE=openviking-build-base:2026.03 \
PY_DEPS_IMAGE=openviking-py-deps:uvlock-server-20260401 \
ADMIN_DEPS_IMAGE=openviking-admin-deps:npmlock-20260401 \
IMAGE_NAME=openviking-server \
BUILD_TARGET=server-runtime \
./scripts/build-docker.sh 1.0.4
```

### 私有仓库

如果你使用远端 registry，可以把依赖镜像先 push 到仓库，再在正式构建时传完整镜像地址。

```bash
BUILD_BASE_IMAGE=registry.example.com/openviking-build-base:2026.03 \
PY_DEPS_IMAGE=registry.example.com/openviking-py-deps:uvlock-server-20260401 \
ADMIN_DEPS_IMAGE=registry.example.com/openviking-admin-deps:npmlock-20260401 \
IMAGE_NAME=openviking-server \
BUILD_TARGET=server-runtime \
REGISTRY=registry.example.com/ \
./scripts/build-docker.sh 1.2.3
```

```bash
BUILD_BASE_IMAGE=registry.example.com/openviking-build-base:2026.03 \
BOT_PY_DEPS_IMAGE=registry.example.com/vikingbot-py-deps:uvlock-bot-20260401 \
IMAGE_NAME=vikingbot \
BUILD_TARGET=bot-runtime \
REGISTRY=registry.example.com/ \
./scripts/build-docker.sh 1.2.3
```

### 多架构发布

本地为 `linux/amd64` 机器准备部署镜像时，建议只构建单架构：

```bash
PLATFORM=linux/amd64 \
IMAGE_NAME=openviking-server \
BUILD_TARGET=server-runtime \
./scripts/build-docker.sh 1.2.3
```

如果要正式发布多架构镜像，需要推仓库或导出 OCI，不能用默认的 `--load`：

```bash
PUSH=1 \
PLATFORM=linux/amd64,linux/arm64 \
REGISTRY=registry.example.com/ \
IMAGE_NAME=openviking-server \
BUILD_TARGET=server-runtime \
./scripts/build-docker.sh 1.2.3
```

## 配置文件

先准备 `deploy/ov.conf`，可以直接从 [ov.conf.example](/Users/daniel-wu/brain/OpenViking-xr-support-bot/deploy/ov.conf.example) 复制。

双容器模式有两个关键点：

- `server.with_bot` 要设为 `true`
- `server.bot_api_url` 要指向 `http://vikingbot:18790`
- `bot.ov_server.server_url` 不能写 `127.0.0.1`，要写 `http://openviking:1933`

如果省略 `bot.ov_server.server_url`，bot 会从根层 `server.host` 自动推导；当 `server.host=0.0.0.0` 时，它会回退到 `127.0.0.1`，这只适合同机单容器，不适合双容器。

## 用 Compose 部署

```bash
cd deploy
cp ov.conf.example ov.conf

OPENVIKING_VERSION=1.2.3 docker compose up -d
```

说明：

- `openviking` 服务会监听宿主机 `1933`
- `vikingbot` 只在 Docker network 内暴露 `18790`
- 如果需要从宿主机直连 bot，可自行给 `vikingbot` 服务增加端口映射
- 如果前面还有自定义 nginx / ingress 路由白名单，除了 `/admin/*` 之外，还需要放行 `/guest/*` 和 `/assets/*`

检查状态：

```bash
docker compose ps
docker compose logs -f openviking
docker compose logs -f vikingbot
curl http://127.0.0.1:1933/health
```

## 不用 Compose

```bash
docker network create ov-net
docker volume create openviking-data
```

```bash
docker run -d \
  --name openviking \
  --network ov-net \
  -p 1933:1933 \
  -e OPENVIKING_CONFIG_FILE=/app/ov.conf \
  -v ./deploy/ov.conf:/app/ov.conf:ro \
  -v openviking-data:/app/data \
  openviking-server:1.2.3 \
  openviking-server --with-bot --bot-url http://vikingbot:18790
```

```bash
docker run -d \
  --name vikingbot \
  --network ov-net \
  -e OPENVIKING_CONFIG_FILE=/app/ov.conf \
  -v ./deploy/ov.conf:/app/ov.conf:ro \
  -v openviking-data:/app/data \
  vikingbot:1.2.3
```
