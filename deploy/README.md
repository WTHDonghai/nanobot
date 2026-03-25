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
