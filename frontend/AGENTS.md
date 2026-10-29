# Frontend Agent 规范 v2（仅限 Kimi 维护）

本目录（`frontend/`）为 `agent-workflows` 多 Agent 研发协同编排操作台的前端工程，**100% 由 Kimi 负责编写与维护**。

## 1. 边界约束
- **只允许修改 `frontend/` 目录内的文件**；可只读 `../shared/openapi.json` 与项目规则文件，严禁修改上级目录的 Python 后端代码（`../src/`、`../console/` 等）。
- **接口契约唯一来源**：请读取 `../shared/openapi.json`（OpenAPI `2.0.0`）获取所有 REST & SSE API 路径、请求参数与响应 Schema，严格对齐 `frontend/src/types.ts` 与 `frontend/src/api.ts`，不要猜测字段名。
- 完成后请在项目根目录运行 `python scripts/check_agent_boundaries.py --role frontend` 和 `npm --prefix frontend run build` 自检零报错。

---

## 2. 技术选型与核心架构升级目标

- **框架与构建**：React 18+ + TypeScript + Vite（`vite.config.ts` 将 `/api` 代理到 `http://127.0.0.1:8080`，`build.outDir` 保持为 `dist`）。
- **样式与图标**：Tailwind CSS + 自定义语义 CSS 变量 + `lucide-react`。
- **交互式编排画布**：`@xyflow/react`（React Flow）。
  - **重要改造**：旧版画布写死了只读 `nodesDraggable={false}` / `nodesConnectable={false}` 且详情页写死了 `plan / patch / test / review` 四个固定 Tab。**新版必须彻底移除只读限制与固定四阶段 Tab**，升级为支持**可视化自由增删改节点与连线 + 一句话自然语言改图 + 点击画布任意节点查看实时流式日志/产出/Diff + 增强型人工控场**的完整操作台！

---

## 3. 核心功能与接口对接清单（严格对齐 `../shared/openapi.json` v2.0.0）

### 3.1 顶部状态栏与 CLI Agent 管理弹窗
1. **健康与在线 Agent 状态胶囊**：
   - 调用 `GET /api/health` 与 `GET /api/agents` 展示当前就绪的真实本地 CLI Agent 数量与列表（如 `codex, claude 在线`，注意后端已彻底移除假 `StubAgent`）。
   - 点击顶部「Agent 管理」按钮打开原生 `<dialog>`：
     - 展示 `GET /api/agents` 返回的所有内置预设（`codex` / `claude` / `kimi` / `gemini`）及自定义 CLI 的 `available` 状态与探测到的二进制路径 `resolved_path`。
     - 支持新增或编辑 CLI Agent（调用 `POST /api/agents`，可配置 `id`、`name`、`driver`、`binary`、`model`、`args_template` / `command_template`、`timeout_sec`），以及删除自定义 Agent（`DELETE /api/agents/{agent_id}`）。

### 3.2 可视化工作流编辑器（编辑态）与一句话自然语言改图
当未选中具体运行记录（或用户切换到「编排编辑」模式）时，主工作区进入**可编辑工作流画布模式**：

1. **工作流管理栏**：
   - 下拉切换工作流（`GET /api/workflows`）。
   - **新建工作流**：初始化一个带默认入口节点的可编辑草稿。
   - **复制工作流**：基于当前 `WorkflowSpec` 克隆新 ID。
   - **工作流设置（名称 / 描述 / 自定义启动入参 `WorkflowSpec.inputs`）**：允许用户增删改该工作流的启动表单字段（`WorkflowInputSpec`：`key`、`label`、`kind`、`required`、`default`、`placeholder`；默认仅保留 `task` 必填即可一键开跑）。
   - **保存工作流**：调用 `POST /api/workflows`（或 `PUT /api/workflows/{workflow_id}`），在有未保存改动时显示醒目的“未保存更改”提示与「保存」按钮；保存前或节点变更时可调用 `POST /api/workflows/validate` 实时校验拓扑合法性并获取 `upstream_map`。
   - **删除工作流**：调用 `DELETE /api/workflows/{workflow_id}`（所有工作流均可删除，删除前二次确认）。

2. **一句话自然语言生成/修改工作流（AI 改图栏）**：
   - 在画布上方提供显式关联 `<label>` 的自然语言输入栏（例如占位提示：`输入一句话生成或改图，如：在测试后加一个 Claude 审查节点，或改成前后端并行开发…`）+ 可选 CLI 选择器（默认 `auto`）+「AI 构图」按钮。
   - 提交时调用 `POST /api/workflows/copilot`，请求体 `{ instruction, current_workflow, agent_id, auto_save: false }`（新建空白时 `current_workflow` 传 `null`，修改当前图时传当前画布上的 `WorkflowSpec`）。
   - 接口返回 `{ workflow, summary, agent_used }` 后，立即将新 `workflow` 渲染到画布上并标记为待保存草稿（同时支持一键「撤销本次 AI 改图」或点击「保存」落盘），Toast 展示 `summary`。

3. **画布交互式节点增删与并行/条件连线（React Flow）**：
   - 在编辑态开启 `nodesDraggable={true}`、`nodesConnectable={true}`、`elementsSelectable={true}`，同步节点拖拽后的 `position: { x, y }` 到草稿 `WorkflowSpec.nodes`。
   - **节点工具栏（一键添加四类标准节点）**：
     - `+ Agent 节点` (`kind: "agent"`)：绑定 CLI Agent 执行编码、分析或审查。
     - `+ Shell 命令` (`kind: "shell"`)：执行构建、测试、Git 同步等脚本。
     - `+ 人工卡点` (`kind: "gate"`)：暂停等待人工审批、在线改产出或指定节点打回。
     - `+ 条件判断` (`kind: "condition"`)：根据上游 `ok` / `exit_code` / `runs` / `output` 分支路由或循环重试。
     - 画布自动渲染虚拟 `END`（结束）目标节点以便用户将终末节点拖线连向 `END`，并在入口节点（`workflow.entry_point`）上展示醒目的「入口 (START)」徽章。
   - **连线与删除**：
     - 从任意节点拖线连接到另一节点（或 `END`）时新增一条 `EdgeSpec`（**从同一节点拉出多条普通无条件边即构成并行分支 Fan-out**！）。
     - 选中任意节点或连线后，可通过右侧属性面板的「删除节点 / 删除连线」按钮删除（删除节点时一并清理关联的 `edges`，若删的是 `entry_point` 则自动顺延或提示重设入口）。

4. **编辑态右侧属性配置抽屉（选中节点或连线时展示）**：
   - **选中节点（`NodeSpec`）时**：
     - 通用字段：节点 ID、显示名称 `label`、职责描述 `description`、「设为入口节点 (`entry_point`)」按钮、多入边汇聚策略 `join_mode`（`auto` 自动识别并行汇聚 | `all` 等待全部并行分支完成 | `any` 任一到达即触发）、循环上限 `max_runs`。
     - 当 `kind === "agent"` 时：
       - 执行主机 `worker_id`（下拉读取 `GET /api/workers`，一期为 `local`）。
       - CLI Agent `agent_id`（下拉读取 `GET /api/agents`，展示 `auto` 及各 CLI 的在线/未安装状态）。
       - 工作目录 `cwd`（可填子目录如 `frontend`、绝对路径或 `{inputs.repo}`）。
       - 沙箱权限 `sandbox`（`read-only` 只读分析 | `workspace-write` 允许读写代码）与 `capture_git_diff` 开关。
       - `prompt_template` 多行编辑器 + **快捷插入变量胶囊按钮**：提供 `{task}`、`{feedback}`、各 `{inputs.<key>}`，以及根据上游节点自动列出的 **`{<上游节点ID>.output}`**、**`{<上游节点ID>.diff}`**、**`{<上游节点ID>.stdout}`**，点击胶囊直接插入到 Prompt 光标处！
     - 当 `kind === "shell"` 时：
       - 执行主机 `worker_id`、工作目录 `cwd`、`command` 命令模板（同样提供上游变量胶囊快捷插入）、`continue_on_error` 开关、超时时间 `timeout_sec`。
     - 当 `kind === "gate"` 时：
       - 审批提示说明 `instruction`、重点审阅的上游节点多选 `review_nodes`、**`allow_edit_output` 开关（允许审批人直接在线编辑修改上游产出）**、允许打回的目标上游节点多选 `rewind_targets`。
     - 当 `kind === "condition"`（或选中某条出边 `EdgeSpec`）时：
       - 可视化编辑该节点各条出边的条件规则：目标节点 `target`、连线标签 `label`、是否为兜底分支 `is_default`，以及条件表达式 `condition`（`field` 支持下拉选上游节点的 `<node_id>.ok` / `<node_id>.exit_code` / `<node_id>.runs` / `<node_id>.output`，`op` 支持 `== | != | < | <= | > | >= | contains | not_contains | regex | truthy | falsy`，`value` 常量值）。

---

### 3.3 动态启动弹窗与运行期实时控场（运行监控态）

1. **动态「发起新运行」弹窗（`<dialog>`）**：
   - 选择要运行的 `workflow_id` 后，**动态根据该工作流的 `WorkflowSpec.inputs` 数组渲染表单项**（支持 `textarea`、`text`、`path`、`number`、`boolean`、`select`）。
   - 默认工作流只需填一句必填的 `task`（任务描述）即可直接点击「创建并运行」，调用 `POST /api/runs` 传入 `{ workflow_id, task, inputs }`。

2. **运行监控态画布与点击节点看详情（彻底取代写死的旧版 4 个 Tab）**：
   - 选中某条运行记录（`RunStateOut`）时，画布实时高亮各节点状态：
     - `running_nodes` 中的节点：脉冲动画高亮（**支持多个并行节点同时高亮“执行中”**）。
     - `status === "waiting"` / `"error"` / `"cancelled"` 时 `next` 中的节点：醒目停靠高亮（待审批 / 失败断点 / 已强杀断点）。
     - `values.nodes[nodeId]` 中已执行节点：展示状态角标与已执行次数 `runs`（如 `×2`），若 `edited_by_human === true` 显示「人工已修订」标记。
   - **点击画布上的任意节点**，右侧详情面板立即切换展示该节点的运行数据（结合 `values.nodes[selectedNodeId]` 与实时日志）：
     - **实时终端日志 Tab**：通过 `EventSource('/api/runs/' + threadId + '/stream?after_seq=...')` 订阅 SSE 实时日志流（并在历史/切换节点时配合 `GET /api/runs/{tid}/logs?node_id=...`），CLI 执行期间毫秒级逐行滚动显示输出！
     - **节点产出 Tab (`output`)**：展示该节点的结构化输出文本（若被人工在线编辑过则标注）。
     - **代码变更 Tab (`diff`)**：若 `diff` 非空，使用语法着色的 `+/-` Git Diff 视图展示。

3. **三大运行期人工控场操作**：
   - **运行中随时强杀中止**：当 `run.status === "running"` 时，右侧面板顶部与运行列表提供醒目的红色「强杀中止」按钮，调用 `POST /api/runs/{tid}/cancel` 立即杀掉后端活跃子进程组。
   - **失败/被中止节点原地重试（支持临时改参）**：当 `run.status` 为 `"error"`、`"cancelled"` 或 `"interrupted"` 时，提供「原地重试该节点」按钮（调用 `POST /api/runs/{tid}/retry`），并支持展开“重试前临时调整参数”（可临时覆盖本次重试节点的 `agent_id`、`cwd`、`prompt_template` 或 `command`），无需从头重跑已完成的上游节点！
   - **增强型人工卡点审批卡片（当 `run.status === "waiting"` 且 `interrupts` 非空时置顶展示）**：
     - 展示 `interrupt.label`、`interrupt.instruction` 以及 `interrupt.upstream_outputs` 中各上游节点的 `output` 与 `diff`。
     - **直接在线修改上游产出**：当 `interrupt.allow_edit_output === true` 时，上游节点的 `output` 旁提供「在线编辑内容」切换按钮，用户可直接修改方案/契约文本；点击「确认并继续 (`approve`)」时，将修改后的内容放入 `POST /api/runs/{tid}/resume` 的 `edited_outputs: { [nodeId]: editedText }` 一并提交！
     - **自选打回到任意上游节点**：提供显式关联 `<label>` 的「打回目标节点 (`target_node_id`)」下拉框（选项来自 `interrupt.rewind_options`，例如在并行汇聚后的验收卡点，可单独选择只打回给 `前端模块实现` 或只打回给 `后端模块实现`）+ `feedback` 意见输入框 +「打回重跑 (`revise`)」按钮 +「中止运行 (`abort`)」按钮。

---

## 4. 现代 Web 研发与无障碍规范（必须继续严格遵守）

在编写或重构 `frontend/` 下的所有新组件（画布工具栏、节点抽屉、AI 改图栏、Agent 管理弹窗、动态启动表单、实时日志终端、增强审批卡片）时，**必须持续遵守以下规范**：

### 4.1 原生浮层与模态框
- 所有模态弹窗（新建运行、Agent 管理、工作流元信息设置、删除确认）**必须使用原生 `<dialog closedby="any" aria-labelledby="...">`** 并通过 `ref.current?.showModal()` / `close()` 控制；**禁止**手写 `<div class="modal-backdrop">` 遮罩或重复添加 `role="dialog"` / `aria-modal`，保留 Safari `pointerdown` + `pointerup` 矩形边界检测回退。
- 保持滚动包含样式：`dialog:modal { overscroll-behavior: contain; }` 与 `dialog::backdrop { overflow: hidden; overscroll-behavior: contain; }`，严禁使用 `body { overflow: hidden }`。
- 错误横幅使用 `role="alert"`，Toast 状态通知使用 `role="status"` / `aria-live="polite"`。

### 4.2 表单语义、校验与输入体验
- **所有 `<input>`、`<textarea>`、`<select>` 必须关联显式的 `<label htmlFor="id">` 并声明 `name` 属性**（包括 AI 改图输入框、节点属性抽屉中的每一项、打回目标下拉框与审批 `feedback` 文本框），严禁仅依赖 `placeholder` 充当标签。
- 搜索区域保持 `<search>` 地标 + `type="search"`；存放路径、命令、ID 或二进制名的输入框（如 `cwd`、`command`、`binary`、`id`、`repo`）必须设置 `autoCorrect="off" autoCapitalize="off" spellCheck={false}`。
- 严禁通过禁用提交按钮来阻止必填项校验（仅在请求进行中 `submitting` 时禁用防重）；`<textarea>` 允许 `resize: vertical`（禁止 `resize: none`）。

### 4.3 排版、无障碍与现代 CSS
- 保留 `<meta name="text-scale" content="scale" />`，**严禁使用 `px` 定义 `font-size`**（统一使用 `rem`），`line-height` 使用无单位数值，表单控件字号不小于 `1rem`。
- 所有纯图标按钮必须具备明确的 `aria-label`，内部图标 `<svg>` 设置 `aria-hidden="true"`；可滚动日志终端与代码块 `<pre>` 设置 `tabIndex={0}` 并包裹 `<code>`。
- Tab 切换组件严格配套 `role="tablist"` / `role="tab"` / `role="tabpanel"`、`aria-controls`、`aria-labelledby` 及左右方向键导航。
- 严禁全局 `*` 重置；按钮 `:hover` 统一写成 `:hover:not(:disabled)`；动效遵守 `@media (prefers-reduced-motion: reduce)`；动态颜色通过 CSS 自定义属性（Design Tokens）传递。
