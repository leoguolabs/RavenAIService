#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
COMPOSE_FILE="${PROJECT_ROOT}/docker-compose.yml"

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "未找到 Docker Compose。请安装 Docker Desktop 或 docker compose 插件。" >&2
  exit 1
fi

compose() {
  "${COMPOSE[@]}" -f "${COMPOSE_FILE}" "$@"
}

require_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "未找到 Docker。请先安装 Docker Desktop 或 Docker Engine。" >&2
    exit 1
  fi

  if ! docker info >/dev/null 2>&1; then
    echo "Docker daemon 未运行，请先启动 Docker。" >&2
    exit 1
  fi
}

ensure_env_file() {
  if [ ! -f "${PROJECT_ROOT}/.env" ] && [ -f "${PROJECT_ROOT}/.env.example" ]; then
    cp "${PROJECT_ROOT}/.env.example" "${PROJECT_ROOT}/.env"
    echo "已从 .env.example 创建 .env，请按需补充私有配置。"
  fi
}

print_endpoints() {
  local port="${HTTP_PORT:-8085}"
  echo
  echo "访问入口："
  echo "  前端控制台: http://localhost:${port}"
  echo "  后端健康检查: http://localhost:${port}/health"
  echo "  API 文档: Docker 入口禁用（本地开发开启方式见 QUICKSTART.md）"
  echo "  Raven 包管理: http://localhost:${port}/raven/"
}
