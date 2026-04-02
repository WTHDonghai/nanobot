# Docker Deployment

当前部署目录已经切换为双镜像模式：

- `openviking-server` 镜像负责 HTTP API 和 `/admin`
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

推荐分别构建成两张镜像：

```bash
IMAGE_NAME=openviking-server BUILD_TARGET=server-runtime ./scripts/build-docker.sh 1.2.3
IMAGE_NAME=vikingbot BUILD_TARGET=bot-runtime ./scripts/build-docker.sh 1.2.3
```

如果使用私有仓库：

```bash
REGISTRY=registry.example.com/ \
IMAGE_NAME=openviking-server \
BUILD_TARGET=server-runtime \
./scripts/build-docker.sh 1.2.3

REGISTRY=registry.example.com/ \
IMAGE_NAME=vikingbot \
BUILD_TARGET=bot-runtime \
./scripts/build-docker.sh 1.2.3
```

## 依赖缓存层

当前 `Dockerfile` 仍然保留可复用的缓存层：

- `build-base`
- `py-deps`
- `bot-py-deps`
- `admin-deps`

可先单独预构建：

```bash
BUILD_TARGET=build-base ./scripts/build-docker.sh 2026.03
BUILD_TARGET=py-deps ./scripts/build-docker.sh uvlock-server-20260401
BUILD_TARGET=bot-py-deps ./scripts/build-docker.sh uvlock-bot-20260401
BUILD_TARGET=admin-deps ./scripts/build-docker.sh npmlock-20260401
```

正式构建时透传：

```bash
BUILD_BASE_IMAGE=registry.example.com/openviking-build-base:2026.03 \
PY_DEPS_IMAGE=registry.example.com/openviking-py-deps:uvlock-server-20260401 \
BOT_PY_DEPS_IMAGE=registry.example.com/vikingbot-py-deps:uvlock-bot-20260401 \
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

## 多架构构建

单机本地测试只建议构建单架构：

```bash
PLATFORM=linux/amd64 \
IMAGE_NAME=openviking-server \
BUILD_TARGET=server-runtime \
./scripts/build-docker.sh 1.2.3
```

多架构发布需要推仓库或导出 OCI：

```bash
PLATFORM=linux/amd64,linux/arm64 \
BUILD_BASE_IMAGE=openviking-build-base:2026.03 \
BOT_PY_DEPS_IMAGE=vikingbot-py-deps:uvlock-bot-20260401 \
IMAGE_NAME=openviking-server \
BUILD_TARGET=server-runtime \
./scripts/build-docker.sh 1.2.3
```

```bash
PLATFORM=linux/amd64,linux/arm64 \
BUILD_BASE_IMAGE=openviking-build-base:2026.03 \
BOT_PY_DEPS_IMAGE=vikingbot-py-deps:uvlock-bot-20260401 \
IMAGE_NAME=vikingbot \
BUILD_TARGET=bot-runtime \
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
