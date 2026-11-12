# RavenAIService 快速开始

本文档是本项目唯一推荐的发布、打包、容器启动入口。默认使用 Docker Compose 一键启动前端、统一后端、异步任务、Redis 和独立数据容器。

## 目录规范

- `scripts/`：所有运维脚本统一放在这里。
- `docker-compose.yml`：本地和服务器 Docker 编排入口。
- `Dockerfile`：FastAPI 后端、Raven 包管理接口、Celery Worker、Celery Beat 共用镜像。
- `frontend/Dockerfile`：前端构建并通过 Nginx 提供访问入口。
- `data/`、`logs/`、`temp/`：仅保留本地占位文件，容器运行数据默认写入 Docker volumes。

## 容器划分

当前 Compose 会启动以下容器：

- `raven-frontend`：前端静态站点和统一反向代理，对外暴露 `HTTP_PORT`，默认 `8085`。
- `raven-backend`：FastAPI 统一后端服务，包含日志、AI、发布、Raven 包管理接口。
- `raven-worker`：Celery 异步任务 Worker。
- `raven-beat`：Celery 定时任务。
- `redis`：任务队列和结果后端。
- `raven-data-store`：独立数据容器，集中挂载应用数据、日志、临时目录和包管理数据 volumes。

## 第一次启动

```bash
./scripts/docker-start.sh
```

脚本会自动从 `.env.example` 创建 `.env`。请把模型 Key、生产数据库密码等只写入 `.env`，不要提交到代码库。SSH 私钥使用下文的独立运行时目录，不写入 `.env` 或代码仓库。

启动后访问：

- 前端控制台：http://localhost:8085
- 后端健康检查：http://localhost:8085/health
- Raven 包管理：http://localhost:8085/raven/

接口文档默认关闭。Docker Nginx 入口始终对 `/openapi.json`、`/docs`、`/redoc`
及其子路径返回 404，业务 API 不受影响。本地开发需要文档时，在本地 `.env` 中设置
`ENVIRONMENT=development` 和 `API_DOCS_ENABLED=true`，启动仅绑定本机的开发后端：

```bash
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

然后访问 http://127.0.0.1:8000/docs。`production` 环境始终关闭文档。
关闭文档只需保持 `API_DOCS_ENABLED=false`，无需切换会影响数据库等配置的 `ENVIRONMENT`。

如需修改宿主机端口：

```bash
HTTP_PORT=18085 ./scripts/docker-start.sh
```

## SSH 私有仓库

当项目仓库只能通过 SSH 克隆时，使用 `docker-compose.ssh.yml` 覆盖层。它通过独立的 `raven-ssh-agent` 容器加载私钥；`raven-backend`、`raven-worker` 和 `raven-worker-bugfix` 只挂载 SSH agent socket 与公开的 `known_hosts`，不会直接挂载私钥。

先在宿主机准备专用目录。私钥应优先使用只具备目标仓库只读权限的 GitLab Deploy Key；父目录保持 `root` 专有，私钥文件使用 Raven 容器内 `appuser` 的固定 UID/GID（`1000:1000`），且只会挂载到隔离的 SSH agent 容器：

```bash
sudo install -d -m 0700 -o root -g root /etc/raven-ai/ssh
sudo install -m 0600 -o 1000 -g 1000 /secure/source/id_ed25519 /etc/raven-ai/ssh/id_ed25519
sudo install -m 0644 -o root -g root /secure/source/known_hosts /etc/raven-ai/ssh/known_hosts
```

然后用基础编排和 SSH 覆盖层一起启动：

```bash
docker compose -f docker-compose.yml -f docker-compose.ssh.yml up -d --build
```

如需使用其他宿主机路径，在 `.env` 中设置 `CODE_REPO_SSH_PRIVATE_KEY_PATH` 和 `CODE_REPO_SSH_KNOWN_HOSTS_PATH`。这两个变量只保存路径，不保存密钥内容。验证 SSH agent 已加载且目标仓库可读：

```bash
docker compose -f docker-compose.yml -f docker-compose.ssh.yml exec backend ssh-add -l
docker compose -f docker-compose.yml -f docker-compose.ssh.yml exec backend \
  git ls-remote --symref 'ssh://git@example.internal:2222/group/project.git' HEAD
```

## 常用命令

启动或重新构建：

```bash
./scripts/docker-start.sh
```

重启全部容器：

```bash
./scripts/docker-restart.sh
```

查看全部日志：

```bash
./scripts/docker-logs.sh
```

只看某个服务日志：

```bash
./scripts/docker-logs.sh backend
./scripts/docker-logs.sh frontend
./scripts/docker-logs.sh worker
```

停止容器但保留数据：

```bash
./scripts/docker-stop.sh
```

停止并删除所有项目 volumes：

```bash
./scripts/docker-stop.sh --volumes
```

强制清理本项目容器、volumes 和悬空镜像：

```bash
./scripts/docker-clean.sh --force
```

## DockerHub 发布

先登录 DockerHub：

```bash
docker login
```

发布到 DockerHub：

```bash
./scripts/docker-publish.sh <dockerhub_namespace> <tag>
```

示例：

```bash
./scripts/docker-publish.sh colingg v1.0.0
```

会构建并推送以下镜像：

- `<dockerhub_namespace>/raven-backend:<tag>`
- `<dockerhub_namespace>/raven-frontend:<tag>`

默认也会推送 `latest`。如果不想推送 `latest`：

```bash
PUSH_LATEST=false ./scripts/docker-publish.sh colingg v1.0.0
```

## 数据持久化

数据不再写入业务容器层，统一存放在 Docker volumes：

- `raven-ai-service_app_data`：后端 SQLite、设备链接缓存等应用数据。
- `raven-ai-service_app_logs`：后端日志。
- `raven-ai-service_app_temp`：日志处理临时文件。
- `raven-ai-service_raven_data`：Raven 包元数据、上传包、向量索引。
- `raven-ai-service_redis_data`：Redis AOF 数据。

`raven-data-store` 会挂载这些 volumes，业务容器只消费对应路径，避免数据散落在多个镜像层或代码目录中。

## 数据库管理

数据库辅助脚本已移动到 `scripts/`：

```bash
python scripts/manage-db.py info
python scripts/manage-db.py migrate
```

容器内执行迁移：

```bash
docker compose exec backend python -m alembic upgrade head
```

## 注意事项

- 不再使用根目录旧脚本，所有 Docker 工作流都通过 `scripts/docker-*.sh` 执行。
- Raven 包管理服务已统一进 `raven-backend`，不再单独维护独立包服务容器或镜像。
- 删除 volumes 会删除上传包、向量索引、SQLite 数据库和 Redis 数据，请先确认备份。
