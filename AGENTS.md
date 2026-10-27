# Multi-Agent 协作与前后端职责边界 (AGENTS.md)

本仓库采用同仓前后端分离（Monorepo）架构，严格划分不同 Agent 的读写边界：

## 1. 目录所有权（硬性规定）

| 目录 / 文件 | 负责 Agent | 权限说明 |
|---|---|---|
| `frontend/` | **仅限 Kimi (`kimi`)** | 前端工程（React + TypeScript + Vite + Tailwind CSS + React Flow）。**除 Kimi 外的任何后端/通用 Agent 严禁创建或修改 `frontend/` 下的代码文件**（仅允许读取或配置规则说明）。 |
| `src/`、`console/`、`scripts/`、`deploy/`、`examples/` | **后端 Agent（Codex / Claude Code / Antigravity）** | Python 后端与 LangGraph 编排引擎。**Kimi 严禁修改任何后端 Python 代码**。 |
| `shared/openapi.json` | **后端导出 / 前端只读** | 前后端唯一接口契约。后端修改 `console/app.py` 的接口或 Pydantic 模型后，必须执行 `python scripts/export_openapi.py` 更新此文件；前端 Kimi 仅读取此文件对齐类型与接口。 |

## 2. 后端 Python 环境管理规范（硬性规定：使用 `uv`）

- **统一使用 `uv` 管理 Python 解释器、虚拟环境（`.venv/`）与依赖包**，严禁直接使用系统 `pip` 或全局 Python 环境安装依赖。
- **常用命令约定**：
  - 创建虚拟环境：`uv venv --python python3.12 .venv`（或通过 `source env.sh` 使用项目内置 `bin/uv`）
  - 安装依赖：`uv pip install --python .venv/bin/python <pkg>`（或 `-r requirements.lock.txt`）
  - 锁定依赖：新增或升级后端依赖后，必须执行 `uv pip freeze --python .venv/bin/python > requirements.lock.txt` 更新锁文件。
  - 运行脚本与服务：统一使用 `.venv/bin/python` / `.venv/bin/uvicorn`（或 `uv run --python .venv/bin/python ...`）。

## 3. 协作流程

1. **接口变更**：后端 Agent 修改 `console/app.py` -> 运行 `.venv/bin/python scripts/export_openapi.py` 更新 `shared/openapi.json`。
2. **前端开发**：在 `frontend/` 目录下调用 `kimi`（读取 `frontend/AGENTS.md` 与 `../shared/openapi.json`）完成页面、组件与状态联调。
3. **本地联调与部署**：
   - 开发期：后端运行 `.venv/bin/uvicorn console.app:app --port 8080`，前端运行 `npm --prefix frontend run dev`（通过 Vite Proxy 将 `/api` 转发到 `http://127.0.0.1:8080`）。
   - 生产/单进程模式：运行 `npm --prefix frontend run build` 产出 `frontend/dist/`，`console/app.py` 会自动挂载 `frontend/dist` 并在 `GET /` 返回构建后的前端页面。
