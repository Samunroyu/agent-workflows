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

## 4. 现代 Web 研发与无障碍规范（对齐 `modern-web-guidance`）

在编写或修改 `frontend/` 下的 HTML、CSS 与 React/TS 代码时，**必须严格遵守以下现代 Web 规范**：

### 4.1 原生浮层与模态框（`dialog` / `light-dismiss-a-dialog` / `prevent-page-scroll-under-modal-dialog` / `persistent-toast-notifications`）
- **模态框必须使用原生 `<dialog>`**：
  - 严禁使用 `<div class="modal-backdrop"> + <section role="dialog" aria-modal="true">` 手写遮罩与 `Escape` 监听。
  - 必须使用 `<dialog closedby="any" aria-labelledby="...">` 并通过 `ref.current?.showModal()` 打开（浏览器自动接管 Top Layer、焦点陷阱、背景 `inert`、`Esc` 关闭与焦点归还，**禁止在原生 `<dialog>` 上重复添加 `role="dialog"` 或 `aria-modal`**）。
  - 为不支持 `closedby` 的浏览器（如 Safari）提供 `if (!('closedBy' in HTMLDialogElement.prototype))` 的 `pointerdown` + `pointerup` 坐标边界检测回退。
  - **禁止使用 `body { overflow: hidden }` 锁定滚动**；必须同时在 `dialog:modal` 和 `dialog::backdrop` 上配置滚动包含：
    ```css
    dialog:modal { overscroll-behavior: contain; }
    dialog::backdrop { overflow: hidden; overscroll-behavior: contain; }
    ```
- **Toast 与动态状态提示**：
  - 顶部 Toast 通知推荐使用 `popover="manual"` 置入 Top Layer（并提供 `@oddbird/popover-polyfill` 按需特性检测或优雅降级），关闭按钮必须具备显式可访问名称。
  - 错误横幅（Banner）与动态状态提示必须声明 `role="alert"`（紧急错误）或 `aria-live="polite"`（一般状态通知）。

### 4.2 表单语义、校验与输入体验（`forms` / `required-field-feedback` / `accessibility`）
- **显式 `<label>` 与 `name` 关联**：
  - 所有 `<input>`、`<textarea>`、`<select>` 必须关联显式的 `<label htmlFor="id">`（或使用 React `useId()` 生成唯一 ID）并设置 `name` 属性；**严禁仅依赖 `placeholder` 充当标签**（包括审批区的 `feedback` 文本框）。
  - 搜索区域必须使用语义化 `<search>` 地标包裹，输入框使用 `type="search"`。
  - 存放路径、命令或标识符的输入框（如 `repo`、`test_cmd`、搜索框）必须关闭自动纠错：`autoCorrect="off" autoCapitalize="off" spellCheck={false}`。
- **校验时机与提交按钮状态**：
  - **严禁通过禁用提交按钮来阻止表单校验**（例如禁止写 `disabled={!form.task.trim()}`），应保持提交按钮可点击（仅在 `submitting` 请求进行中禁用以防重复提交），让浏览器原生触发必填校验并聚焦首个错误字段。
  - 使用 `:user-invalid` 伪类（避免页面初始加载即标红）配合非颜色视觉提示展示必填错误，并通过 `onBlur` / `onSubmit` / `onInput` 同步 `aria-invalid="true"`。
- **输入框可调整性与触控尺寸**：
  - 严禁对 `<textarea>` 设置 `resize: none`（允许 `resize: vertical`）。
  - 表单控件字号在移动端至少为 `1rem`（16px）以防止 iOS Safari 自动放大页面；粗指针设备（`@media (pointer: coarse)`）交互目标尺寸不小于 `48px`（常规可点击元素最小 `24x24px`）。

### 4.3 排版与系统级字号缩放（`typography` / `respect-os-text-scale`）
- **接入系统字号缩放**：在 `index.html` 的 `<head>` 中声明 `<meta name="text-scale" content="scale" />`，且**严禁在 `:root` 或 `html` 上硬编码覆盖 `font-size`（如 `font-size: 16px`）**。
- **使用相对单位与无单位行高**：
  - **严禁使用 `px` 定义 `font-size`**，必须统一使用 `rem`（或上下文相关的 `em` / `clamp()`），确保用户调整浏览器或系统字号至 200% 时可读、无截断。
  - `line-height` 必须使用无单位数值（如 `1.5`），代码块使用 `overflow-wrap: anywhere` 或 `break-word`。
  - 响应式断点优先使用容器查询或基于 `em`/`rem` 的媒体查询，避免固定 `height` / `max-height` 导致大字号下文本溢出。

### 4.4 无障碍与语义化 HTML（`html` / `accessibility` / `spinner`）
- **严禁使用 `title` 作为唯一可访问名称**：
  - 所有纯图标按钮（侧边栏导航、通知铃铛、关闭详情、关闭弹窗、关闭 Toast 等）必须提供 `aria-label` 或 `.visually-hidden` 文本；内部装饰性 `<svg>` 图标应设置 `aria-hidden="true"`。
  - 响应式隐藏按钮文字时（如窄屏下的“发起新运行”按钮），**严禁使用 `font-size: 0`**，必须使用标准 `.visually-hidden` 类（`clip-path: inset(50%)` + `1px` 宽高）。
- **语义化标签与键盘可达性**：
  - 凡是触发动作的交互项（如运行记录行）必须使用 `<button type="button">`；可滚动的代码块 `<pre>` 必须设置 `tabIndex={0}` 且内部包裹 `<code>` 元素。
  - 使用 `role="tablist"` / `role="tab"` 时，内容面板必须配套 `role="tabpanel"` 与 `aria-labelledby`，并支持左右方向键切换 Tab。
- **加载指示器与动效偏好**：
  - 加载状态优先结合语义化 `<progress aria-label="正在加载…" />`，且所有旋转/过渡动画必须遵守 `@media (prefers-reduced-motion: reduce)`（降速或关闭高频旋转）。

### 4.5 现代 CSS 架构（`css`）
- **禁止全局通配符重置**：避免在 `*` 上编写全局重置样式（如 `* { box-sizing: border-box; }`），应按具体元素或 `@layer` 分层声明。
- **避免选择器过度匹配（Overmatching）**：按钮的 `:hover` 样式必须排除禁用态（使用 `button:hover:not(:disabled)`），防止禁用按钮在鼠标悬停时触发背景色或位移变化。
- **使用 CSS 自定义属性（Design Tokens）传递动态状态**：避免在 JSX 中硬编码内联静态颜色对象（如 `style={{ color: '#...', background: '#...' }}`），优先通过 CSS 变量或语义类名驱动主题与状态色，并及时清理未使用的原型演示样式（如废弃的 `.demo-a` / `.demo-c`）。

