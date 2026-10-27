# Langfuse 自托管（Podman）

使用 **podman** 直接运行，适用于 Rocky Linux 9 等 Linux 部署环境。
不依赖 docker-compose，全部逻辑在 `deploy.sh` 里。

## 组件与端口

| 容器 | 镜像 | 端口 | 说明 |
|---|---|---|---|
| `langfuse-web` | langfuse/langfuse:4 | **0.0.0.0:3000** | Web UI / API，对外 |
| `langfuse-worker` | langfuse/langfuse-worker:4 | 127.0.0.1:3030 | 异步 ingestion / 队列消费 |
| `langfuse-postgres` | postgres:17 | 127.0.0.1:5432 | 元数据 |
| `langfuse-clickhouse` | clickhouse-server:25.12 | 127.0.0.1:8123, 9000 | trace 存储 |
| `langfuse-minio` | minio/minio | **0.0.0.0:9090**, 127.0.0.1:9091 | 事件/媒体对象存储 |
| `langfuse-redis` | redis:7 | 127.0.0.1:6379 | BullMQ 队列 |

镜像默认从 `docker.io` 获取；可用 `LANGFUSE_IMAGE_REGISTRY` 指定代理缓存，使用 `LANGFUSE_MINIO_IMAGE` 指定 MinIO 镜像。

首次运行：

```bash
LANGFUSE_HOST_IP=127.0.0.1 bash deploy/langfuse/deploy.sh
```

远程部署时将 `LANGFUSE_HOST_IP` 替换为服务器地址。密钥由脚本自动生成并保存在部署目录中。

## 文件

| 路径 | 说明 |
|---|---|
| `/opt/langfuse/deploy.sh` | 幂等部署/管理脚本 |
| `/opt/langfuse/.env` | 自动生成的密钥（600，勿外传） |
| `/opt/langfuse/common.env` | web/worker 共用环境（由脚本生成） |
| 卷 `langfuse_pgdata` / `langfuse_clickhouse_data` / `langfuse_clickhouse_logs` / `langfuse_minio_data` / `langfuse_redis_data` | 持久化数据 |

## 和 SqliteSaver 的分工（两者不互相替代）

| | `SqliteSaver`（LangGraph） | `Langfuse` |
|---|---|---|
| 存什么 | 每个 super-step 的 **graph state 快照** | 每次 LLM/工具/节点的 **span**（输入输出、token、耗时、成本） |
| 解决什么 | 中断恢复、human-in-the-loop、时间旅行、断点重跑 | 看执行细节、找慢点/错点、对比多次运行、评测/标注 |
| 形态 | 一个 `.sqlite` 文件，代码 API | 6 个容器 + Web UI |
| 能否互换 | 不能。state 是给程序读的，不是给人看的 | 不能。trace 不保存可恢复的图状态 |

结论：**LangGraph 的持久化继续用 SqliteSaver**（`examples/verify_graph.py` 已验证）；
Langfuse 只负责"看得见"。若只想要前者，本项目本来就不需要 Langfuse。

补充：Langfuse v3/v4 **不支持 SQLite**，硬依赖 Postgres（元数据）+ ClickHouse（trace）+
S3 兼容存储（blob）+ Redis（队列），所以没有"Langfuse 跑在 SQLite 上"这种选项。
想要单容器、SQLite/内存后端的轻量方案，可换 Arize Phoenix（见下方对比）。

## 重量对比（实测）

| 方案 | 容器数 | 常驻内存 | 备注 |
|---|---|---|---|
| Langfuse 4（当前） | 6 | **~1.9 GB**（web 627MB / worker 719MB / clickhouse 448MB / minio 91MB / pg 45MB / redis 12MB） | 功能全，自带评测/标注/数据集 |
| Arize Phoenix | 1 | ~300 MB | 单容器，OTel 原生，界面偏调试 |
| 纯 SqliteSaver + 日志 | 0 | 0 | 无 UI，`get_state_history()` 自己打 |

嫌重的话：`/opt/langfuse/deploy.sh --down` 即可停掉（保留数据卷），再按需换方案。

## 常用命令

```bash
ssh your-server
/opt/langfuse/deploy.sh --status     # 容器状态
/opt/langfuse/deploy.sh             # 幂等：缺的建、停的启
/opt/langfuse/deploy.sh --recreate  # 删容器重建（保留数据卷）
/opt/langfuse/deploy.sh --down      # 停并删容器（保留数据卷）
podman logs -f langfuse-web         # 看 web 日志
cat /opt/langfuse/.env              # 看密钥/账号/API key
```

开机自启：脚本已启用 `podman-restart.service`（容器带 `--restart=always`）。

## 接入 LangGraph（本项目）

```bash
pip install langfuse   # 已在 requirements 之外，按需加
```

```python
import os
os.environ["LANGFUSE_HOST"] = "http://127.0.0.1:3000"
os.environ["LANGFUSE_PUBLIC_KEY"] = "pk-lf-..."
os.environ["LANGFUSE_SECRET_KEY"] = "sk-lf-..."

from langfuse.langchain import CallbackHandler
handler = CallbackHandler()

graph.invoke(state, config={"callbacks": [handler]})   # 每个节点/Prompt/工具调用都会出现在 trace 里
```

也可以把环境变量直接写进 `env.sh`（`LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY`），
所有入口自动上报。

## 数据与备份

```bash
# 逻辑备份示例
podman exec langfuse-postgres pg_dump -U postgres postgres > pg-$(date +%F).sql
podman volume export langfuse_minio_data -o minio-$(date +%F).tar
```

## 已知取舍

- 3000/9090 绑定所有网络接口；其余组件仅绑定 127.0.0.1。
- `TELEMETRY_ENABLED=false`，不上报任何遥测。
- 无 TLS：内网 http。若需域名/HTTPS，建议前置 nginx 并改 `NEXTAUTH_URL`。
- SELinux enforcing + 命名卷，无需 `:z` 标签；若改成宿主机目录挂载必须加 `:Z`。
