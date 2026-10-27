# Frontend Agent 规范（仅限 Kimi 维护）

本目录（`frontend/`）为 `agent-workflows` 操作台的前端工程，**100% 由 Kimi 负责编写与维护**。

## 1. 边界约束
- **只允许在 `frontend/` 目录内读写文件**，严禁修改上级目录的 Python 后端代码（`../src/`、`../console/` 等）。
- **接口契约唯一来源**：请读取 `../shared/openapi.json` 获取所有后端 REST API 路径、请求参数与响应 Schema。不要猜测接口字段。

## 2. 技术选型要求
- **框架与构建**：React 18+ + TypeScript + Vite
- **样式与图标**：Tailwind CSS + `lucide-react`（深色科技感主题，延续操作台紧凑清晰的工程风格）
- **流程图/编排画布**：`@xyflow/react`（React Flow，**动态读取 `GET /api/workflows` 返回的 `WorkflowSpec.nodes` 和 `WorkflowSpec.edges` 渲染任意工作流拓扑图**，严禁在前端写死节点，从而保证外部 Agent 通过 API 或 MCP 生成新工作流后，Web 页面立即可见并可渲染）
- **开发服务器代理**：`vite.config.ts` 中配置 `server.proxy`，将 `/api` 代理到 `http://127.0.0.1:8080`；`build.outDir` 保持为 `dist`。

## 3. 核心功能要求（对齐 `../shared/openapi.json`）
1. **顶部状态栏与工作流切换器**：
   - 调用 `GET /api/health` 显示当前后端 Agent 类型（`stub` / `codex`）及在线状态、实时时钟。
   - 调用 `GET /api/workflows` 获取已注册的工作流列表（如默认的 `coding` 以及外部 Agent 动态生成的任何工作流），支持切换查看不同工作流的拓扑图。
2. **左侧边栏（新建 Run + 运行列表）**：
   - 新建表单：选择 `workflow_id`（下拉选择 `/api/workflows` 中的工作流，默认 `coding`）、填写 `task`（必填）、`repo`（可选）、`test_cmd`（可选）、`max_attempts`（默认 2），调用 `POST /api/runs`，展示返回的 `warnings`（如有）。
   - 运行列表：每 2.5s 轮询 `GET /api/runs`，展示所有 Run 的状态徽章（`waiting` 待审批 / `running` 执行中 / `done` 已完成 / `error` 出错 / `interrupted` 已中断）、所属 `workflow_id`、任务标题与 `thread_id`。
3. **右侧详情区（选中某个 Run 时通过 `GET /api/runs/{tid}` 渲染；未选中 Run 时展示当前所选工作流的画布预览）**：
   - **动态工作流拓扑画布（React Flow）**：根据当前 Run 所属的 `workflow_id` 找到对应的 `WorkflowSpec`，将 `nodes`（区分 `agent` / `gate` / `shell` / `summary` 四种类型样式，若节点自带 `position` 则使用，否则自动横向/分层排布）和 `edges`（普通实线边 `target` + 条件虚线边 `conditions` / `default_target`）渲染为流程图，并根据 `values.events`、`next`、`status` 动态高亮已执行节点与当前停靠节点。
   - **待人工审批卡片（当 `interrupts` 非空且 `status === "waiting"` 时置顶醒目展示）**：
     - 展示 `stage`、`plan`、`patch`（带 `+/-` 语法着色的 Git Diff 视图）、`test_output`、`review` 以及 `interrupts` 中暴露的其他自定义字段。
     - 提供 `feedback` 文本框和审批按钮：`通过 (approve)`、`打回 (revise)`、`中止 (abort)`，点击调用 `POST /api/runs/{tid}/resume`。
   - **事件流标签（`values.events`）**、**最近 Diff 视图（`values.patch`）**、**执行结果（`values.result`）** 与 **错误堆栈（`error`）**。
