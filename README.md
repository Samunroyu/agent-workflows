# agent-workflows

编排多个工作流，节点可以是在本机或远端跑的 code agent（codex、Claude Code 等）。
当前阶段：coding 工作流、人工审批操作台和真实 Codex CLI 接入已实现；默认使用离线 StubAgent。

## 环境

本地 Python 运行环境位于项目目录；删掉 `.venv/`、`python/`、`cache/` 即可移除该运行环境。

| 组件 | 版本 |
|---|---|
| CPython | 3.12.15（python-build-standalone，系统自带只有 3.9） |
| uv | 0.13.5 |
| langgraph | 1.2.16 |
| langgraph-checkpoint / -sqlite | 4.3.0 / 3.1.2 |
| langchain / langchain-core | 1.4.7 / 1.6.13 |
| langgraph-cli | 0.4.37 |

完整依赖见 `requirements.lock.txt`。

## 目录

```text
agent-workflows/
|-- .venv/                  # 虚拟环境
|-- bin/uv                  # uv 二进制
|-- python/python/          # CPython 3.12.15 standalone
|-- cache/                  # uv 包缓存（~177MB，可删，删后需重下）
|-- src/
|   |-- state.py            # 工作流 state 定义（TypedDict + reducer）
|   |-- run.py              # 本地交互式跑工作流（SQLite 持久化 + 人审）
|   |-- agents/base.py      # AgentTarget 协议 + StubAgent
|   |-- agents/codex.py     # codex CLI 的真实 AgentTarget 实现
|   `-- workflows/coding.py # coding 工作流图
|-- console/
|   |-- app.py              # 操作台后端（FastAPI + SqliteSaver，单进程）
|   `-- index.html          # 操作台前端（单页，无外部依赖）
|-- docs/coding-graph.md    # 工作流结构图（Mermaid）
|-- examples/
|   |-- verify_graph.py     # 验证图：plan -> 人审 gate -> done
|   `-- client_demo.py      # 通过 langgraph dev 的 HTTP API 驱动工作流
|-- langgraph.json          # langgraph dev 入口
|-- deploy/langfuse/        # 自建 Langfuse 可观测性部署脚本
|-- env.example.sh          # 环境变量模板；复制为 env.sh 后使用
|-- bootstrap.sh            # 从零重建
`-- requirements.lock.txt
```

## 用法

```bash
cd agent-workflows
cp env.example.sh env.sh                    # 首次运行；已有本地配置时跳过
source env.sh                              # 必须：PATH / 缓存目录 / 国内镜像

python examples/verify_graph.py            # 验证 checkpoint + interrupt 全链路
langgraph dev --no-browser --port 20245    # 本地 API + Studio，Ctrl-C 退出
```

`langgraph dev` 起来后：

- API: <http://127.0.0.1:20245>（`/ok` 健康检查，`POST /assistants/search` 列 graph）
- Studio 前端 URL 命令行会打印（指向 smith.langchain.com，`baseUrl` 是本机端口）

实测输出：

```text
[ok] 1/3 图执行到 gate 并暂停，checkpoint 已落盘
[ok] 2/3 interrupt 恢复成功，trace = ['plan', 'gate', 'done']
[ok] 3/3 checkpointer 记录了 5 个快照，可回放/时间旅行
```

本地 `env.sh`、密钥文件、SQLite 数据库和运行缓存均由 `.gitignore` 排除。`bootstrap.sh` 提供 macOS arm64 环境初始化；其他平台可自行创建 Python 3.12 虚拟环境并安装 `requirements.lock.txt`。操作台还需安装 `fastapi`，追踪功能需安装 `langfuse`。

## 写一个工作流

一个 LangGraph 工作流就是 5 件事，`src/workflows/coding.py` 是最小完整样例：

| 概念 | 在本项目里 | 要点 |
|---|---|---|
| **State** | `src/state.py` 的 `CodingState` | 只放节点间要传的数据；`Annotated[list, operator.add]` 声明"追加"语义 |
| **Node** | `plan_node` / `implement_node` ... | 普通函数：读 state，返回**增量 dict**，不要原地改 |
| **Edge** | `add_edge` / `add_conditional_edges` | 条件边函数只读 state，返回下一个节点名 |
| **Interrupt** | `plan_gate_node` / `final_gate_node` | `interrupt(payload)` 让图暂停并持久化，人审后用 `Command(resume=...)` 继续 |
| **Checkpointer** | `SqliteSaver` | 有了它才有中断恢复、回放、时间旅行 |

coding 工作流的形状：

```text
START -> plan -> [人审 gate] --approve--> implement -> test --pass--> review -> [人审 gate] -> finish
                    |  ^                      ^            |
                  revise|                    +--fail------+   (attempt < max)
                    |  +------------------------------------+
                  abort -> finish                          attempt >= max -> give_up
```

三种运行方式：

```bash
source env.sh

# 1) 本地交互（真实人审，SQLite 落盘到 ./checkpoints.sqlite）
python -m src.run "给 verify_graph.py 加 --verbose"

# 2) 作为服务 + 客户端驱动（未来 UI 走这条）
langgraph dev --no-browser --port 20245 &
python examples/client_demo.py

# 3) Studio 里点着看（浏览器，见下方）
```

换真实 agent 只改一处：实现 `src/agents/base.py` 里的 `AgentTarget`（Local/SSH/HTTP），
然后 `build(agent=LocalAgent(...))`，节点逻辑不用动。

## 操作台（Web UI）

启动操作台后，浏览器打开 <http://127.0.0.1:8080>：

- 左侧：所有 run 的列表（状态点：待审批 / 执行中 / 已完成 / 出错）
- 右侧：当前 run 详情 —— 待审 gate 直接展示 **计划 / diff / 测试输出 / review**
- 按钮：**通过 approve**、**打回 revise**（可填反馈）、**中止 abort**
- 顶部：新建 run（任务描述 + repo 路径 + 测试命令）

一次真实运行（codex 真改代码，实测）：

```text
任务: 修好 calc.py 的 add 函数
plan(codex ~160s) → [人审 approve] → implement#1 → test#1:pass → review:ok → [人审 approve] → finish

final_gate 页面上的 diff:
  --- a/calc.py
  +++ b/calc.py
  -    return a - b
  +    return a + b
```

架构：**单进程**，不依赖 `langgraph dev`。`console/app.py` 自己持有编译好的图和
`SqliteSaver`，每个 run 在后台线程 invoke；`interrupt()` 时线程结束、state 落盘到
`data/checkpoints.sqlite`；前端每 2.5s 轮询。

| 接口 | 作用 |
|---|---|
| `GET /api/runs` | 列表 + 每个 run 的状态/事件/待审 payload |
| `POST /api/runs` | `{"task","repo","test_cmd","max_attempts"}` 启动 |
| `GET /api/runs/{tid}` | 单个 run 详情（state + interrupts） |
| `POST /api/runs/{tid}/resume` | `{"action":"approve|revise|abort","feedback":"..."}` |

本地跑（Mac）：

```bash
source env.sh
CONSOLE_AGENT=stub  .venv/bin/uvicorn console.app:app --port 8080   # stub 快，调 UI 用
CONSOLE_AGENT=codex .venv/bin/uvicorn console.app:app --port 8080   # 真跑 codex
```

## 真实 agent：codex

`src/agents/codex.py` 实现 `AgentTarget`，把节点调用翻译成 `codex exec`：

| 节点 prompt 前缀 | codex 沙箱 | 说明 |
|---|---|---|
| `PLAN:` / `REVIEW:` / `TEST:` | `read-only` | 只读分析 |
| `IMPLEMENT:` | `workspace-write` | 允许改文件 |

要点：

- `-c approval_policy="never"` 非交互不弹审批；`--sandbox` 与 `--approve-for-me` **互斥**
- `-o <file>` 只取最后一条消息作为节点输出
- implement 的 `patch` 不是 agent 的文本，而是 **`git -C repo diff`** 抓的真实改动
- test 节点走 `test_cmd`（真实命令 + 退出码），没配才退回 agent

运行前需安装并配置 codex CLI 的认证和模型服务；工作流复用执行环境中的 CLI 配置。

## 部署示例（Linux 服务器）

```bash
# 从 Mac 同步（排除本机 venv/缓存）
rsync -az --exclude '.venv' --exclude 'python' --exclude 'cache' \
  --exclude '.langgraph_api' --exclude '__pycache__' --exclude 'checkpoints.sqlite' \
  ./ your-server:/opt/agent-workflows/

systemctl status  agent-workflows-console
systemctl restart agent-workflows-console
journalctl -u agent-workflows-console -f
```

| 项 | 值 |
|---|---|
| 代码 | `/opt/agent-workflows` |
| venv | `/opt/agent-workflows/.venv`（必须用 `/usr/bin/python3.12` 建） |
| 环境变量 | `/opt/agent-workflows/console.env` |
| checkpoint | `/opt/agent-workflows/data/checkpoints.sqlite` |
| 服务 | `agent-workflows-console.service` → 0.0.0.0:8080 |
| codex | `/usr/local/bin/codex`（从 `/root/.local/bin` 复制而来） |

## 可观测性（Langfuse，可选）

可使用 `deploy/langfuse/` 自建 Langfuse。以下以本机服务为例，远程部署时替换服务地址：

- Web UI / API：<http://127.0.0.1:3000>
- 上报入口：`http://127.0.0.1:3000/api/public/otel`（OTLP）/ SDK 用 `LANGFUSE_HOST`
- 部署与管理脚本：`deploy/langfuse/`（podman 原生，无 docker-compose 依赖）

按需安装 `langfuse` SDK，在本地 `env.sh` 中设置 `LANGFUSE_HOST/PUBLIC_KEY/SECRET_KEY`；命令行和操作台入口已接入可选追踪：

```python
from langfuse.langchain import CallbackHandler
from langfuse.types import TraceContext

tid = hashlib.md5(thread_id.encode()).hexdigest()   # 一次工作流 = 一条 trace
config = {
    "configurable": {"thread_id": thread_id},
    "metadata": {
        "langfuse_trace_name": f"coding: {task[:60]}",
        "langfuse_session_id": thread_id,
    },
    "callbacks": [CallbackHandler(trace_context=TraceContext(trace_id=tid))],
}
graph.invoke(state, config)
```

两个坑（都已在 `src/run.py` 处理）：

1. **必须显式传 `trace_context`**：否则 interrupt → resume 每次都会新开一条 trace（一次工作流拆成 3 条）。
2. **`langfuse_trace_name` / `langfuse_session_id`** 走 config metadata，UI 里直接可读；
   不传的话 trace 名就叫 `LangGraph`，很难找。

看数据：登录 <http://127.0.0.1:3000> → 项目 `agent-workflows` → Tracing → Traces。

注意：Langfuse 的 trace 是**运行观测**，和 `SqliteSaver` 的**状态持久化**是两件事，
互不替代（详见 `deploy/langfuse/README.md`）。

## 已知坑

1. **`UV_CACHE_DIR` 必须可写**：默认写 `$HOME/.cache/uv`，受限环境直接报
   `Failed to create directory`。`env.sh` 已处理。
2. **`SqliteSaver` 要传 `check_same_thread=False`**：LangGraph 在 worker 线程写 checkpoint。
3. **`langgraph.json` 必须有 `dependencies`**，否则 CLI 报 `No dependencies found in config`。
4. **NJU 镜像比 uv 内置清单滞后一天**：uv 要 `20261021`、镜像最新 `20261020`，
   `uv python install` 会 404，所以解释器是手动取包解压的（`bootstrap.sh` 固化了 tag）。
5. **服务器上的 venv 应使用稳定路径中的 Python 3.12 创建**：`/root/.local/bin/python3.12`
   指向 `/tmp/xdfs-schema-python-*/` 下的临时 CPython，systemd 下 exec 直接 `Permission denied`
   （SELinux 上下文 + /tmp 会被清理）。
6. **systemd 的 PATH 不含 `/root/.local/bin`**：服务里调 `codex` 报
   `FileNotFoundError: 'codex'`。把真实二进制 `cp -L` 到 `/usr/local/bin/codex` 并在
   unit 里显式设 `Environment=PATH=/usr/local/bin:/usr/bin:/bin`。
7. **codex 的沙箱依赖两个东西**：只把 `codex` 复制到 `/usr/local/bin` 不够，
   ① 同目录的 `codex-code-mode-host` 也要一起复制，否则所有 shell 命令报
   "运行环境缺少 codex-code-mode-host"；② read-only 沙箱需要 `bwrap`（`dnf install bubblewrap`），
   否则报 "运行环境缺少沙箱组件 bwrap"。注意 workspace-write 下 `apply_patch` 不经过 shell，
   所以即使缺 bwrap 也能改文件——别被"implement 成功但 plan 读不到文件"骗了。
8. **Langfuse web 容器 listen ≠ ready**：`0.0.0.0:3000` 已在监听但连接堆积时，
   SDK 上报会报 `Failed to export spans batch due to timeout`。等它日志出现
   `MCP feature registered` 后再重试，或 `podman restart langfuse-web` 后等 60s。

## 网络（本机实测）

| 源 | 速度 | 用途 |
|---|---|---|
| PyPI 清华镜像 | 快 | Python 包 |
| NJU github-release 镜像 | ~1MB/s | python-build-standalone |
| GitHub 直连 / ghproxy | ~18KB/s 或 403 | 不可用 |

## 下一步

已完成：图编排 / 操作台 UI / codex 真实 agent / 可选 Langfuse 上报 / Linux 部署脚本。

接下来按优先级：

- **产物落盘**：现在是直接改工作区，应该先 `git worktree` 隔离，approve 后再合回 / 提 MR
- **并发**：`_async_lock` 全局串行，改为 per-thread 锁 + 队列，允许多个 run 同时跑
- **agent 扩展**：`SSHAgent`（跑在开发机，codex/claude-code），以及 `HTTPAgent`（远端服务）
- **registry**：`src/registry.py` 用 YAML 描述节点/边/权限，免改代码加新流程
- **编排画布**：`console/` 加 React Flow（本地打包，不依赖 CDN），编辑 YAML → 动态构图
