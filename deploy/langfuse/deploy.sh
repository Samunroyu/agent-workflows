#!/usr/bin/env bash
# Langfuse 自托管：podman 原生运行（该机无 docker daemon，只有 podman 5.8 模拟 docker CLI）
#
# 用法：
#   ./deploy.sh              # 首次部署 / 幂等重启
#   ./deploy.sh --recreate   # 删除容器重建（保留数据卷）
#   ./deploy.sh --status     # 查看状态
#   ./deploy.sh --down       # 停止并删除容器（保留数据卷）
#
# 环境：Rocky Linux 9 / SELinux enforcing / firewalld
# 端口：3000 = Langfuse Web（对外），9090 = MinIO API（对外，供浏览器读取媒体链接）
#       5432/8123/9000/6379/9091/3030 仅绑定 127.0.0.1
set -euo pipefail

ROOT="${LANGFUSE_ROOT:-/opt/langfuse}"
NET=langfuse
HOST_IP="${LANGFUSE_HOST_IP:-127.0.0.1}"
ENV_FILE="$ROOT/.env"
# 镜像源：默认 Docker Hub；可用 LANGFUSE_IMAGE_REGISTRY 指定代理缓存
IMAGE_REGISTRY="${LANGFUSE_IMAGE_REGISTRY:-docker.io}"
# MinIO 使用 elestio 镜像；可用 LANGFUSE_MINIO_IMAGE 覆盖
# （RELEASE.2025-09，内置 mc，可用于建桶）
MINIO_IMAGE="${LANGFUSE_MINIO_IMAGE:-$IMAGE_REGISTRY/elestio/minio:latest}"
COMMON_ENV="$ROOT/common.env"

log() { printf '\033[1;32m>>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m!!\033[0m %s\n' "$*" >&2; exit 1; }

command -v podman >/dev/null || die "podman 不存在"
command -v openssl >/dev/null || die "openssl 不存在"

mkdir -p "$ROOT"
chmod 700 "$ROOT"
cd "$ROOT"

# ---------------------------------------------------------------- secrets
if [[ ! -f "$ENV_FILE" ]]; then
  log "首次运行：生成密钥 -> $ENV_FILE"
  cat > "$ENV_FILE" <<EOF
HOST_IP=$HOST_IP
# --- 数据库/中间件口令 ---
POSTGRES_PASSWORD=$(openssl rand -hex 16)
CLICKHOUSE_PASSWORD=$(openssl rand -hex 16)
REDIS_AUTH=$(openssl rand -hex 16)
MINIO_ROOT_USER=minio
MINIO_ROOT_PASSWORD=$(openssl rand -hex 16)
# --- Langfuse 应用密钥 ---
SALT=$(openssl rand -hex 24)
ENCRYPTION_KEY=$(openssl rand -hex 32)
NEXTAUTH_SECRET=$(openssl rand -hex 32)
# --- 初始化（首个组织/项目/管理员/API key）---
LANGFUSE_INIT_ORG_ID=default
LANGFUSE_INIT_ORG_NAME=agent-workflows
LANGFUSE_INIT_PROJECT_ID=agent-workflows
LANGFUSE_INIT_PROJECT_NAME=agent-workflows
LANGFUSE_INIT_PROJECT_PUBLIC_KEY=pk-lf-$(openssl rand -hex 12)
LANGFUSE_INIT_PROJECT_SECRET_KEY=sk-lf-$(openssl rand -hex 12)
LANGFUSE_INIT_USER_EMAIL=admin@example.com
LANGFUSE_INIT_USER_NAME=admin
LANGFUSE_INIT_USER_PASSWORD=$(openssl rand -base64 18 | tr -dc 'A-Za-z0-9' | head -c 16)
EOF
  chmod 600 "$ENV_FILE"
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
: "${HOST_IP:?}"

# ---------------------------------------------------------------- 操作命令
case "${1:-}" in
  --status)
    podman ps --filter "label=io.langfuse=1" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
    echo
    echo "URL: http://$HOST_IP:3000"
    exit 0
    ;;
  --down)
    for c in langfuse-web langfuse-worker langfuse-minio langfuse-redis langfuse-clickhouse langfuse-postgres; do
      podman rm -f "$c" >/dev/null 2>&1 || true
    done
    log "容器已删除（数据卷 langfuse_* 保留）"
    exit 0
    ;;
  --recreate)
    for c in langfuse-web langfuse-worker langfuse-minio langfuse-redis langfuse-clickhouse langfuse-postgres; do
      podman rm -f "$c" >/dev/null 2>&1 || true
    done
    log "容器已删除，开始重建"
    ;;
  '' ) ;;
  * ) die "未知参数: $1" ;;
esac

# ---------------------------------------------------------------- 网络/卷
podman network exists "$NET" || { log "创建网络 $NET"; podman network create "$NET"; }
for v in pgdata clickhouse_data clickhouse_logs minio_data redis_data; do
  podman volume exists "langfuse_$v" || podman volume create "langfuse_$v" >/dev/null
done

# ---------------------------------------------------------------- 公共环境
cat > "$COMMON_ENV" <<EOF
NEXTAUTH_URL=http://$HOST_IP:3000
DATABASE_URL=postgresql://postgres:${POSTGRES_PASSWORD}@postgres:5432/postgres
SALT=${SALT}
ENCRYPTION_KEY=${ENCRYPTION_KEY}
TELEMETRY_ENABLED=false
LANGFUSE_ENABLE_EXPERIMENTAL_FEATURES=false
CLICKHOUSE_MIGRATION_URL=clickhouse://clickhouse:9000
CLICKHOUSE_URL=http://clickhouse:8123
CLICKHOUSE_USER=clickhouse
CLICKHOUSE_PASSWORD=${CLICKHOUSE_PASSWORD}
CLICKHOUSE_CLUSTER_ENABLED=false
CLICKHOUSE_CLUSTER_NAME=default
LANGFUSE_S3_EVENT_UPLOAD_BUCKET=langfuse
LANGFUSE_S3_EVENT_UPLOAD_REGION=auto
LANGFUSE_S3_EVENT_UPLOAD_ACCESS_KEY_ID=${MINIO_ROOT_USER}
LANGFUSE_S3_EVENT_UPLOAD_SECRET_ACCESS_KEY=${MINIO_ROOT_PASSWORD}
LANGFUSE_S3_EVENT_UPLOAD_ENDPOINT=http://minio:9000
LANGFUSE_S3_EVENT_UPLOAD_FORCE_PATH_STYLE=true
LANGFUSE_S3_EVENT_UPLOAD_PREFIX=events/
LANGFUSE_S3_MEDIA_UPLOAD_BUCKET=langfuse
LANGFUSE_S3_MEDIA_UPLOAD_REGION=auto
LANGFUSE_S3_MEDIA_UPLOAD_ACCESS_KEY_ID=${MINIO_ROOT_USER}
LANGFUSE_S3_MEDIA_UPLOAD_SECRET_ACCESS_KEY=${MINIO_ROOT_PASSWORD}
LANGFUSE_S3_MEDIA_UPLOAD_ENDPOINT=http://$HOST_IP:9090
LANGFUSE_S3_MEDIA_UPLOAD_INTERNAL_ENDPOINT=http://minio:9000
LANGFUSE_S3_MEDIA_UPLOAD_FORCE_PATH_STYLE=true
LANGFUSE_S3_MEDIA_UPLOAD_PREFIX=media/
LANGFUSE_S3_BATCH_EXPORT_ENABLED=false
REDIS_HOST=redis
REDIS_PORT=6379
REDIS_AUTH=${REDIS_AUTH}
EOF
chmod 600 "$COMMON_ENV"

# ---------------------------------------------------------------- 启动函数
ensure() { # ensure <name> <image> [podman run args...]
  local name=$1
  if podman container exists "$name" 2>/dev/null; then
    if ! podman start "$name" >/dev/null 2>&1; then
      podman ps --all --format '{{.Names}}' | grep -qx "$name" || die "$name 启动失败"
    fi
  else
    log "启动 $name"
    shift
    podman run -d --name "$name" --restart=always --network "$NET" --label io.langfuse=1 "$@" >/dev/null
  fi
}

wait_for() { # wait_for <desc> <seconds> <cmd...>
  local desc=$1 secs=$2; shift 2
  local i end=$((SECONDS + secs))
  while (( SECONDS < end )); do
    if "$@" >/dev/null 2>&1; then log "就绪: $desc"; return 0; fi
    sleep 3
  done
  die "超时: $desc"
}

# 1) postgres
ensure langfuse-postgres \
  --network-alias postgres \
  -p 127.0.0.1:5432:5432 \
  -e POSTGRES_USER=postgres -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" -e POSTGRES_DB=postgres \
  -e TZ=UTC -e PGTZ=UTC \
  --memory=2g \
  -v langfuse_pgdata:/var/lib/postgresql/data \
  "$IMAGE_REGISTRY"/library/postgres:17

# 2) clickhouse
ensure langfuse-clickhouse \
  --network-alias clickhouse \
  --user 101:101 \
  -p 127.0.0.1:8123:8123 -p 127.0.0.1:9000:9000 \
  -e CLICKHOUSE_DB=default \
  -e CLICKHOUSE_USER=clickhouse \
  -e CLICKHOUSE_PASSWORD="$CLICKHOUSE_PASSWORD" \
  --ulimit nofile=262144:262144 \
  --memory=8g \
  -v langfuse_clickhouse_data:/var/lib/clickhouse \
  -v langfuse_clickhouse_logs:/var/log/clickhouse-server \
  "$IMAGE_REGISTRY"/clickhouse/clickhouse-server:25.12

# 3) minio（使用 elestio 镜像；桶用其内置 mc 创建）
ensure langfuse-minio \
  --network-alias minio \
  --entrypoint /usr/bin/minio \
  -p 0.0.0.0:9090:9000 -p 127.0.0.1:9091:9001 \
  -e MINIO_ROOT_USER="$MINIO_ROOT_USER" \
  -e MINIO_ROOT_PASSWORD="$MINIO_ROOT_PASSWORD" \
  --memory=1g \
  -v langfuse_minio_data:/data \
  "$MINIO_IMAGE" \
  server --address ":9000" --console-address ":9001" /data

# 4) redis
ensure langfuse-redis \
  --network-alias redis \
  -p 127.0.0.1:6379:6379 \
  --memory=1g \
  -v langfuse_redis_data:/data \
  "$IMAGE_REGISTRY"/library/redis:7 \
  redis-server --requirepass "$REDIS_AUTH" --maxmemory-policy noeviction

# 5) 依赖就绪检查
wait_for postgres 120 podman exec langfuse-postgres pg_isready -U postgres -d postgres
wait_for clickhouse 180 podman exec langfuse-clickhouse wget -q -O /dev/null http://127.0.0.1:8123/ping
wait_for redis 60 podman exec langfuse-redis redis-cli -a "$REDIS_AUTH" ping
wait_for minio 120 curl -sf http://127.0.0.1:9090/minio/health/live

# 5.1) 建桶（Langfuse 要求 langfuse 桶已存在；用镜像自带的 mc）
podman exec langfuse-minio mc alias set local http://127.0.0.1:9000 \
  "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
podman exec langfuse-minio mc mb --ignore-existing local/langfuse >/dev/null

# 6) worker（跑 ClickHouse migration）
ensure langfuse-worker \
  --network-alias langfuse-worker \
  -p 127.0.0.1:3030:3030 \
  --memory=4g \
  --env-file "$COMMON_ENV" \
  "$IMAGE_REGISTRY"/langfuse/langfuse-worker:4

# 7) web
ensure langfuse-web \
  --network-alias langfuse-web \
  -p 0.0.0.0:3000:3000 \
  --memory=3g \
  --env-file "$COMMON_ENV" \
  -e NEXTAUTH_SECRET="$NEXTAUTH_SECRET" \
  -e LANGFUSE_INIT_ORG_ID="$LANGFUSE_INIT_ORG_ID" \
  -e LANGFUSE_INIT_ORG_NAME="$LANGFUSE_INIT_ORG_NAME" \
  -e LANGFUSE_INIT_PROJECT_ID="$LANGFUSE_INIT_PROJECT_ID" \
  -e LANGFUSE_INIT_PROJECT_NAME="$LANGFUSE_INIT_PROJECT_NAME" \
  -e LANGFUSE_INIT_PROJECT_PUBLIC_KEY="$LANGFUSE_INIT_PROJECT_PUBLIC_KEY" \
  -e LANGFUSE_INIT_PROJECT_SECRET_KEY="$LANGFUSE_INIT_PROJECT_SECRET_KEY" \
  -e LANGFUSE_INIT_USER_EMAIL="$LANGFUSE_INIT_USER_EMAIL" \
  -e LANGFUSE_INIT_USER_NAME="$LANGFUSE_INIT_USER_NAME" \
  -e LANGFUSE_INIT_USER_PASSWORD="$LANGFUSE_INIT_USER_PASSWORD" \
  -e LANGFUSE_DISABLE_SIGNUP_ONBOARDING=true \
  "$IMAGE_REGISTRY"/langfuse/langfuse:4

# 8) 防火墙 + 开机自启
if systemctl is-active --quiet firewalld 2>/dev/null; then
  log "放行 firewalld 3000/9090"
  firewall-cmd --permanent --add-port=3000/tcp >/dev/null
  firewall-cmd --permanent --add-port=9090/tcp >/dev/null
  firewall-cmd --reload >/dev/null
fi
systemctl enable --now podman-restart.service >/dev/null 2>&1 || true

# 9) 等 web 起来
wait_for "langfuse-web /api/public/health" 300 curl -fsS http://127.0.0.1:3000/api/public/health

cat <<EOF

==========================================================================
 Langfuse 已部署
==========================================================================
 Web UI     : http://$HOST_IP:3000
 登录账号   : $LANGFUSE_INIT_USER_EMAIL
 登录密码   : $LANGFUSE_INIT_USER_PASSWORD
 组织/项目  : $LANGFUSE_INIT_ORG_NAME / $LANGFUSE_INIT_PROJECT_NAME

 API keys（LangGraph/LangChain 等接入用）
   LANGFUSE_HOST         = http://$HOST_IP:3000
   LANGFUSE_PUBLIC_KEY   = $LANGFUSE_INIT_PROJECT_PUBLIC_KEY
   LANGFUSE_SECRET_KEY   = $LANGFUSE_INIT_PROJECT_SECRET_KEY

 本机 OTLP 上报   : http://$HOST_IP:3000/api/public/otel
 密钥文件         : $ENV_FILE (600)
 管理命令         : $ROOT/deploy.sh --status | --recreate | --down
==========================================================================
EOF
