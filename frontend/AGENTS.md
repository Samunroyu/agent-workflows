# Frontend Agent 规范 v2.1（仅限 Kimi 维护）

本目录（`frontend/`）为 `agent-workflows` 多 Agent 研发协同编排操作台的前端工程，**100% 由 Kimi 负责编写与维护**。

## 1. 边界约束
- **只允许修改 `frontend/` 目录内的文件**；可只读 `../shared/openapi.json` 与项目规则文件，严禁修改上级目录的 Python 后端代码（`../src/`、`../console/` 等）。
- **接口契约唯一来源**：请读取 `../shared/openapi.json`（OpenAPI `2.1.0`）获取所有 REST & SSE API 路径、请求参数与响应 Schema，严格对齐 `frontend/src/types.ts` 与 `frontend/src/api.ts`，不要猜测字段名。
- 完成后请在项目根目录运行 `.venv/bin/python scripts/check_agent_boundaries.py --role frontend` 和 `npm --prefix frontend run build` 自检零报错（构建产出 `frontend/dist/` 供 `:8080` 直接托管）。

### 1.1 最新契约变更：内置 Gemini CLI → Antigravity CLI (`agy`)
- **契约说明**：后端已用 Antigravity CLI（`id="agy"`, `name="Antigravity CLI"`, `driver="agy"`, `binary="agy"`）替换原 `gemini` 内置预设（对外 API 接口无增删）。`../shared/openapi.json` 中 `driver` 枚举已更新为 `'codex' | 'claude' | 'kimi' | 'agy' | 'custom'`。
- **硬性要求**：前端**严禁写死 `gemini`**（可选 Agent 列表动态读取 `GET /api/agents`，驱动枚举严格对齐 `agy`）。
- **Kimi 必改文件与校验**：
  1. `frontend/src/types.ts`：将 `AgentDriver` 中的 `'gemini'` 改为 `'agy'`。
  2. `frontend/src/components/Dialogs.tsx`（约第 898 行驱动 `<select>`）：将 `<option value="gemini">gemini</option>` 改为 `<option value="agy">agy</option>`。
  3. 完成后执行 `.venv/bin/python scripts/check_agent_boundaries.py --role frontend` 和 `npm --prefix frontend run build` 校验通过。

---

## 2. 本轮核心交互重构目标（v2.1 必改项，解决第一期试用痛点）

第一期试用暴露了两个核心体验问题，本轮必须彻底重构到位：
1. **解决「无法顺畅新建自定义工作流」问题**：旧版点「新建」弹出的其实是复杂的底层入参设置框，且点「保存设置」只改前端内存根本没调后端落盘（不额外点画布保存就会丢失，也不会出现在运行弹窗里）。**新版必须提供独立的「+ 新建工作流」极简弹窗，对接新增的 `POST /api/workflows/create` 接口一步创建并落盘保存！**
2. **解决「所有内容堆在同一个长页面、编辑节点只能缩在右侧 350px 窄栏里极难操作」问题**：
   - **页面拆分为两个真正的独立全屏一级视图**（「工作流编排页」与「运行监控中心」），彻底废除单页上下滚动大杂烩！
   - **彻底取消右侧常驻 350px 窄属性栏**，让画布 100% 全宽全高铺开；无论是编排态编辑节点/连线，还是运行态查看实时终端日志/Git Diff/人工卡点在线改产出，**一律改为在屏幕正中央弹出大尺寸居中工作台弹窗（宽 ~940px~990px，支持一键最大化）放大到用户面前操作！**

---

## 3. 核心架构、视图拆分与接口对接清单（严格对齐 `../shared/openapi.json` v2.1.0）

### 3.1 两个独立一级全屏视图（废除单页 `scrollIntoView`）

使用显式视图状态 `activeView: 'editor' | 'runs'` 控制左侧导航栏切换：

1. **视图一：`工作流编排页`（`activeView === 'editor'`，默认首页，沉浸式全屏画布工作台）**
   - **彻底移除**该视图中的「早上好」欢迎横幅、3 个统计卡片、底部的运行记录列表，以及**彻底移除右侧常驻窄属性栏（`WorkflowInspector` 不再作为右侧分栏渲染）**。
   - 主体采用 `100dvh` 沉浸式布局：上方仅保留紧凑的工作流管理栏 + 一句话 AI 改图栏 + 节点添加工具栏，下方 **React Flow 画布横向 100% 铺满、纵向撑满剩余全部屏幕高度**。
   - 点击工具栏「试运行 / 发起运行」并成功创建运行后，自动切换到 `activeView = 'runs'` 并打开该次运行监控。

2. **视图二：`运行监控中心`（`activeView === 'runs'`，独立盯盘与人工审批中心）**
   - 展示紧凑运行统计条、搜索/待审批筛选、左侧紧凑运行记录列表（或顶部运行切换栏）+ **右侧全宽实时运行拓扑画布**（同样不设右侧 350px 常驻窄详情栏）。
   - 顶部面包屑或左栏导航可随时一键切回 `工作流编排页`（保持当前正在编辑的工作流上下文不丢失）。

3. **顶部状态栏与 CLI Agent 管理弹窗（两视图共用精简顶栏）**
   - 展示在线 CLI Agent 状态胶囊（`GET /api/health`、`GET /api/agents`）、「Agent 管理」弹窗按钮（`POST /api/agents`、`DELETE /api/agents/{agent_id}`）、待审批提醒铃铛（点击自动切到 `runs` 视图并打开待审批运行）。

---

### 3.2 全新独立「+ 新建工作流」一键落盘弹窗（对接 `POST /api/workflows/create`）

1. **显著入口与专属弹窗 `CreateWorkflowDialog`（原生 `<dialog>`）**：
   - 在编排页工作流下拉框旁放置醒目的 **`+ 新建工作流`** 按钮，点击打开专属的 **`CreateWorkflowDialog`**（严禁再复用带底层 `inputs` 字段配置的 `WorkflowSettingsDialog`！）。
   - 表单内容极简直观：
     - **工作流名称 (`name`)**：必填，自动聚焦，支持任意中文名称（如 `前后端并行验收流`）。
     - **创建起点 (`mode`)**（三选一）：
       1. `blank`（默认选中）：**空白画布**（含 1 个默认起始节点，进去自由拖拽增删）；
       2. `ai`：**一句话 AI 生成新工作流**（选中时展开 `ai_instruction` 多行输入框 + 可选构图 CLI `agent_id`，默认 `auto`）；
       3. `template`：**复制现有工作流模板**（选中时展开 `template_id` 下拉框选择要克隆的源工作流）。
     - **工作流描述 (`description`)**：选填。
     - **自定义工作流 ID (`id`)**：收进默认折叠的 `<details><summary>高级：自定义工作流 ID（选填，留空自动生成）</summary>` 中，绝不强迫用户手写英文正则 ID！
   - **一步落盘行为**：
     - 点击「立即创建」直接调用 **`POST /api/workflows/create`**，传入 `{ name, id: id || null, description, mode, template_id, ai_instruction, agent_id }`。
     - 后端一次性完成创建/克隆/AI 构图、拓扑校验与写盘保存，返回已落盘的 `WorkflowSpec`。
     - 前端收到返回后立即刷新 `GET /api/workflows` 列表，并将当前画布切换到新返回的 `workflow.id`！新建后无需二次点保存，立即可在「发起新运行」中看到并启动，切换或刷新页面绝不丢失。

2. **「工作流设置」弹窗 (`WorkflowSettingsDialog`) 保存逻辑修正**：
   - 工具栏的「设置」按钮仅用于修改当前工作流的名称、描述与启动表单入参定义（`WorkflowSpec.inputs`）。
   - 在该弹窗点击「保存设置」时，合并到当前 `draft` 后应**直接调用 `PUT /api/workflows/{workflow_id}` 同步落盘保存**（若拓扑校验未通过则明确报错提示），彻底消除“在设置弹窗点了保存却没真正保存”的割裂感。

---

### 3.3 编排态「节点 / 连线放大编辑工作台弹窗」（放大到面前操作！）

彻底取消右侧窄栏，所有节点与连线的深度编辑都在屏幕正中央的大尺寸原生 `<dialog>` 中完成：

1. **画布点选、拖拽与打开放大弹窗的触发手感（`FlowCanvas.tsx`）**：
   - **开启滚轮/触控板缩放**：React Flow 必须设置 **`zoomOnScroll={true}`**（移除旧版 `zoomOnScroll={false}`）。
   - **固定左右连线端口防乱跳**：所有节点的 `targetPosition` 固定为 `Position.Left`、`sourcePosition` 固定为 `Position.Right`（彻底移除按 `y` 坐标比较动态切到 `Top`/`Bottom` 导致拖拽偏离 1px 连线端口就上下乱跳的旧逻辑）。
   - **单击 vs 双击/编辑按钮**：
     - **单击**节点或连线：仅高亮选中（方便直接拖拽移动位置、拖连接点拉线、按 `Delete` / `Backspace` 键删除）。
     - **节点卡片内嵌「编辑」按钮**：在每个实际节点卡片（非虚拟 `START`/`END`）右上角或底部提供清晰的 `编辑` 小按钮（点击阻止冒泡并打开该节点放大弹窗）。
     - **双击节点（`onNodeDoubleClick`）或点击节点卡片上的「编辑」按钮**：立即在屏幕正中央打开 **`NodeEditorDialog`**！
     - **双击连线（`onEdgeDoubleClick`）**（或在条件节点的分支列表中点击某条出边）：立即在屏幕正中央打开 **`EdgeEditorDialog`**！

2. **大尺寸节点编辑工作台弹窗 `NodeEditorDialog`（原生 `<dialog>`）内部降噪布局**：
   - **弹窗尺寸**：默认宽 `min(58rem, calc(100vw - 2.5rem))`（约 `928px`），最大高 `calc(100dvh - 2.5rem)`；右上角配备**「最大化 / 还原」切换按钮**（最大化时宽 `calc(100vw - 1.5rem)`、高 `calc(100dvh - 1.5rem)`）。
   - **弹窗顶栏**：节点类型徽章 + 节点名称标题 + 快捷操作（「设为入口节点 `START`」、「删除节点」、可选的上下游相邻节点快速切换按钮、「最大化」、「完成关闭」）。
   - **第一区：核心属性区（紧凑横向 2~4 列网格，直观无干扰）**
     - `agent` 节点：`节点名称 (label)`、`CLI Agent (agent_id)`、`工作目录 (cwd)`、`沙箱权限 (sandbox)`。
     - `shell` 节点：`节点名称 (label)`、`工作目录 (cwd)`、`出错后继续 (continue_on_error)`。
     - `gate` 节点：`节点名称 (label)`、`允许审批人在线修改产出 (allow_edit_output)`、`重点审阅的上游节点 (review_nodes)`、`允许打回的上游节点 (rewind_targets)`。
     - `condition` 节点：`节点名称 (label)` + 下方直观展示并可直接增删改该节点的所有出边条件分支（目标节点 `target`、判断字段 `field`、运算符 `op`、比较值 `value`、兜底开关 `is_default`）。
   - **第二区：超大号主编辑区（占据弹窗主体视觉空间！）**
     - 针对 `agent` 的 **`prompt_template`（提示词模板）**、`shell` 的 **`command`（Shell 脚本命令）**、`gate` 的 **`instruction`（审批指引说明）**：
       - 正上方紧贴摆放**一键插入光标处的变量胶囊栏**：`{task}`、`{feedback}`、各 `{inputs.<key>}`，以及由 `upstream_map` 自动推导出的 `{<上游节点ID>.output}`、`{<上游节点ID>.diff}`、`{<上游节点ID>.stdout}`。
       - 编辑器 `<textarea>` 默认高度至少 **`18rem ~ 22rem`（约 15~18 行以上）**，最大化模式下自动撑高，代码/提示词采用清晰等宽或高可读字体，让编写长段多 Agent 协同 Prompt 像在专业编辑器里一样宽敞顺手！
   - **第三区：默认折叠的「高级设置」(`<details>`)**
     - 将低频技术参数收进 `<details>` 折叠面板，保持主界面清爽：
       - **重命名节点 ID**：提供新节点 ID 输入框 +「应用新 ID」按钮，调用新增接口 **`POST /api/workflows/rename-node`**（传入 `{ workflow: draft, old_node_id, new_node_id }`），直接用后端返回的已全图联动更新（含 `entry_point`、所有 `edges`、`review_nodes`、`rewind_targets` 与全部 `{节点ID.xxx}` 模板引用）的 `WorkflowSpec` 更新画布草稿！
       - 执行主机 `worker_id`（读取 `GET /api/workers`）、多入边汇聚策略 `join_mode`（`auto | all | any`）、自动循环上限 `max_runs`、超时秒数 `timeout_sec`、抓取 Git Diff `capture_git_diff`、节点职责描述 `description`。

3. **连线编辑弹窗 `EdgeEditorDialog`（原生 `<dialog>`）条件表单降噪**：
   - 当连线的来源节点 **不是** `condition` 节点（且该边原本没有非空 `condition.field`）时：**默认只展示简洁的「来源节点 -> 目标节点 `target`、连线标签 `label`、删除连线」**（可通过「高级：按条件路由此连线」开关按需展开条件配置），确保普通边和并行边的 `condition` 严格保持 `null`。
   - 当来源节点是 `condition` 节点时：展示完整的条件路由配置；**一旦勾选 `兜底分支 (is_default = true)`，立即将 `condition` 置为 `null` 并隐藏条件字段输入**，杜绝残留空的 `{ field: "" }`。

---

### 3.4 运行监控中心与「运行详情 / 人工审批放大工作台弹窗」

在 `activeView === 'runs'`（运行监控中心）中，同样遵循**全宽画布 + 放大到面前操作**的一致体验：

1. **全宽运行监控画布 + 顶部醒目状态横幅**：
   - 画布全宽展示实时运行进度：`running_nodes` 脉冲高亮（支持多并行分支同时高亮）、已完成节点展示执行次数 `runs` 与 `人工已修订` 标记、`waiting` / `error` / `cancelled` 停靠节点醒目高亮。
   - 每个运行节点卡片内提供直观小按钮（如待审批卡点显示「立即审批」，失败节点显示「重试 / 日志」，普通节点显示「查看日志」）。
   - 当 `run.status === 'waiting'`（人工卡点待审批）或 `'error' | 'cancelled' | 'interrupted'`（异常/强杀断点）时，画布上方显示醒目控场提示条，提供一键「打开人工审批工作台」/「打开断点重试工作台」按钮（运行中 `running` 则在顶栏提供醒目红色「强杀中止」按钮调用 `POST /api/runs/{tid}/cancel`）。

2. **居中大尺寸运行节点与审批工作台弹窗 `RunNodeWorkbenchDialog`（原生 `<dialog>`）**：
   - **触发方式**：双击运行画布上的任意节点、点击节点卡片上的「查看日志 / 立即审批 / 重试」按钮，或点击画布顶部的待审批/重试横幅按钮（当刚进入一个处于 `waiting` 状态的人工卡点运行时，也可直接支持一键弹出）。
   - **弹窗尺寸**：默认宽 `min(62rem, calc(100vw - 2.5rem))`（约 `992px`），同样配备右上角**「最大化 / 还原」按钮**，并支持在弹窗顶部快捷切换查看同一次运行的其它节点。
   - **宽屏舒适排布**：
     - **若当前处于人工卡点待审批（`run.status === 'waiting'` 且 `interrupts` 非空）**：
       - 在大窗口内宽敞展示审批说明 `interrupt.instruction` 与各上游节点产出快照（`interrupt.upstream_outputs`）；
       - 点击「在线编辑内容」可在宽大的多行编辑器（`min-height: 14rem`）中直接修订上游方案/契约文本，点击「确认并继续 (`approve`)」通过 `POST /api/runs/{tid}/resume` 的 `edited_outputs` 提交；
       - 同屏清晰提供「打回目标节点 (`target_node_id`)」下拉框（来自 `interrupt.rewind_options`）+ `feedback` 意见框 +「打回重跑 (`revise`)」与「中止运行 (`abort`)」。
     - **若当前处于失败/强杀断点（`error | cancelled | interrupted`）**：
       - 提供「原地重试该节点」（`POST /api/runs/{tid}/retry`）及宽敞的临时参数覆盖编辑器（可临时改 `agent_id`、`cwd`、`prompt_template` 或 `command` 后原地重跑）。
     - **三大运行详情 Tab（`实时日志` | `节点产出` | `Git Diff`）**：
       - `实时日志`：高可视高度（至少 `20rem ~ 26rem`）的深色终端 `<pre tabIndex={0}><code>`，通过 `EventSource('/api/runs/' + tid + '/stream?after_seq=...')` + `GET /api/runs/{tid}/logs?node_id=...` 毫秒级流式滚动输出；
       - `节点产出`：宽幅清晰展示完整结构化 `output`；
       - `Git Diff`：宽幅 `+/-` 着色代码差异视图，不再缩在 350px 窄栏里反复横滑。

---

## 4. 现代 Web 研发与无障碍规范（必须继续严格遵守）

1. **原生浮层与模态框**：
   - 所有弹窗（`CreateWorkflowDialog`、`NodeEditorDialog`、`EdgeEditorDialog`、`RunNodeWorkbenchDialog`、`NewRunDialog`、`WorkflowSettingsDialog`、`AgentManagerDialog`、`ConfirmDialog`）**必须全部使用原生 `<dialog closedby="any" aria-labelledby="...">`**（复用 `NativeDialog` 封装），通过 `showModal()` / `close()` 控制，保留 Safari `pointerdown` + `pointerup` 外部点击关闭回退；保持 `dialog:modal { overscroll-behavior: contain; }`，严禁手写遮罩 `<div>` 或 `body { overflow: hidden }`。
   - 注意：当在 `NodeEditorDialog` 内嵌套使用「删除确认」或直接删除节点时，妥善处理双层 `<dialog>` 或弹窗内二次确认按钮，避免焦点冲突。
2. **表单语义、校验与输入体验**：
   - 所有 `<input>`、`<textarea>`、`<select>` 必须关联显式的 `<label htmlFor="id">` 并声明 `name` 属性，严禁仅依赖 `placeholder` 充当标签。
   - 存放路径、命令、ID 或二进制名的输入框（如 `cwd`、`command`、`binary`、`id`、`new_node_id`、`repo`）必须设置 `autoCorrect="off" autoCapitalize="off" spellCheck={false}`。
   - 严禁通过禁用提交按钮阻止必填校验（仅在请求提交中 `submitting` 时禁用防重）；`<textarea>` 保持 `resize: vertical`（禁止 `resize: none`）。
3. **排版、无障碍与现代 CSS**：
   - 保留 `<meta name="text-scale" content="scale" />`，**严禁使用 `px` 定义 `font-size`**（统一使用 `rem`），`line-height` 使用无单位数值，表单控件字号不小于 `1rem`。
   - 所有纯图标按钮（包括「最大化 / 还原」、关闭、节点卡片内嵌编辑按钮）必须具备明确的 `aria-label`，内部图标 `<svg>` 设置 `aria-hidden="true"`；可滚动日志终端与代码块 `<pre>` 设置 `tabIndex={0}` 并包裹 `<code>`。
   - Tab 切换组件严格配套 `role="tablist"` / `role="tab"` / `role="tabpanel"`、`aria-controls`、`aria-labelledby` 及左右方向键导航。
   - 严禁全局 `*` 重置；按钮 `:hover` 统一写成 `:hover:not(:disabled)`；动效遵守 `@media (prefers-reduced-motion: reduce)`。
