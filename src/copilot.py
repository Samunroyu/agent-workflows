"""自然语言构图与改图引擎（Workflow Copilot）。

直接复用当前机器已登录的本地 CLI Agent（只读沙箱 + 隔离空目录极速推理，无需额外配置任何 API Key）：
- 支持从零用一句话生成全新的多 Agent 工作流拓扑
- 支持基于当前画布上的 WorkflowSpec 用一句话增删改节点、连线、并行分支或条件循环
- 产出后自动执行拓扑合法性校验与 LangGraph 试编译，以及缺失坐标的自动分层排版
"""

from __future__ import annotations

import json
import re
import tempfile
from collections import deque
from typing import Any, Optional

from pydantic import BaseModel, Field

from src.agents.registry import AgentRegistry
from src.registry import NodePosition, WorkflowRegistry, WorkflowSpec, build_state_graph


class CopilotRequest(BaseModel):
    instruction: str = Field(
        ...,
        min_length=2,
        description="自然语言生成或修改指令，例如：『在测试节点后加一个 Claude 代码审查节点，再连到最终审批』",
    )
    current_workflow: Optional[WorkflowSpec] = Field(
        None,
        description="当前画布正在编辑的工作流定义（传入则在原图基础上增量修改；不传则从零生成新工作流）",
    )
    agent_id: str = Field(
        "auto",
        description="用于执行构图的本地 CLI Agent ID（默认 'auto' 自动选择当前在线的首个 CLI）",
    )
    auto_save: bool = Field(
        False,
        description="生成并校验通过后是否立即落盘保存（默认 false，先返回给前端画布预览，用户确认后点保存）",
    )


class CopilotResponse(BaseModel):
    workflow: WorkflowSpec
    summary: str = Field(..., description="本次拓扑生成或修改的简明摘要")
    agent_used: str = Field(..., description="实际调用的本地 CLI Agent ID")
    saved: bool = Field(False, description="是否已落盘保存")


def _auto_layout_missing_positions(spec: WorkflowSpec) -> WorkflowSpec:
    """对未提供坐标（或坐标全部重叠在原点）的节点按 BFS 层级自动排版，保证画布直接美观可读。"""
    non_zero = sum(
        1
        for n in spec.nodes
        if n.position is not None and (abs(n.position.x) > 1 or abs(n.position.y) > 1)
    )
    need_full_layout = non_zero <= 1 and len(spec.nodes) > 1

    adj: dict[str, list[str]] = {n.id: [] for n in spec.nodes}
    for e in spec.edges:
        if e.source in adj:
            tgts = [r.target for r in e.conditions] if e.conditions else ([e.target] if e.target else [])
            if e.default_target:
                tgts.append(e.default_target)
            for t in tgts:
                if t and t in adj and t not in adj[e.source]:
                    adj[e.source].append(t)

    levels: dict[str, int] = {}
    if spec.entry_point in adj:
        levels[spec.entry_point] = 0
        q: deque[str] = deque([spec.entry_point])
        while q:
            u = q.popleft()
            for v in adj.get(u, []):
                if v not in levels:
                    levels[v] = levels[u] + 1
                    q.append(v)

    max_lvl = max(levels.values(), default=0)
    for n in spec.nodes:
        if n.id not in levels:
            max_lvl += 1
            levels[n.id] = max_lvl

    by_level: dict[int, list[str]] = {}
    for n in spec.nodes:
        by_level.setdefault(levels[n.id], []).append(n.id)

    computed_pos: dict[str, NodePosition] = {}
    for lvl, nids in by_level.items():
        x = 80.0 + lvl * 270.0
        total_h = (len(nids) - 1) * 180.0
        start_y = 200.0 - total_h / 2.0
        for idx, nid in enumerate(nids):
            computed_pos[nid] = NodePosition(x=x, y=max(40.0, start_y + idx * 180.0))

    updated_nodes = []
    for n in spec.nodes:
        if need_full_layout or n.position is None:
            updated_nodes.append(n.model_copy(update={"position": computed_pos[n.id]}))
        else:
            updated_nodes.append(n)
    return spec.model_copy(update={"nodes": updated_nodes})


def extract_json_payload(raw_text: str) -> dict[str, Any]:
    """从 CLI 输出文本中稳健提取 JSON 对象（兼容 ```json 代码块与前后解释文字）。"""
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("CLI 未返回任何内容")

    # 1) 优先匹配 ```json ... ``` 代码块
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    for block in reversed(fenced):
        try:
            obj = json.loads(block)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue

    # 2) 直接整体解析
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # 3) 用括号配对扫描文本中最大的合法 JSON 对象
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch == "{":
            try:
                obj, _ = decoder.raw_decode(text[i:])
                if isinstance(obj, dict) and ("workflow" in obj or "nodes" in obj):
                    return obj
            except json.JSONDecodeError:
                continue

    raise ValueError(f"无法从 Agent 输出中解析有效的 JSON 工作流结构: {text[:300]}")


def _build_copilot_prompt(
    instruction: str,
    current_workflow: Optional[WorkflowSpec],
    available_agents: list[dict[str, Any]],
) -> str:
    agents_desc = json.dumps(available_agents, ensure_ascii=False)
    current_json = (
        json.dumps(current_workflow.model_dump(mode="json"), ensure_ascii=False, indent=2)
        if current_workflow
        else "null（请从零创建一个全新的工作流）"
    )

    return f"""你是一个精确的多 Agent 研发工作流拓扑架构师。
注意：当前处于空白临时目录，请【不要调用任何 shell 命令或文件读取工具】，直接根据下方规范输出一个纯 JSON 对象！

### 1. 当前主机已注册的 CLI Agent 列表
{agents_desc}
（如果用户点名了某个 Agent 如 codex / claude / kimi / gemini，请在节点的 agent_id 中填写对应 id；未指定时优先使用 available=true 的 Agent id 或 "auto"。）

### 2. WorkflowSpec v2 核心规则
- `id`: 仅含字母、数字、连字符 `-` 或下划线 `_`（如果是修改现有工作流，保持原 `id` 不变；新建工作流则取语义化英文短横线 id）。
- `name`, `description`: 清晰简洁的中文名称与用途说明。
- `entry_point`: 起始节点 ID（必须在 `nodes` 中存在）。
- `inputs`: 启动表单字段数组（默认至少保留 `key="task", label="任务描述", kind="textarea", required=true`）。
- `nodes`: 支持以下 4 种 `kind`：
  1. `"agent"`: 调用 CLI Agent。关键字段：`id`, `label`, `kind="agent"`, `agent_id`, `cwd`（可选子目录如 `"frontend"` 或 `null`）, `sandbox`（`"read-only"` 用于分析/规划/审查，`"workspace-write"` 用于改代码）, `prompt_template`（支持 `{{task}}`、`{{inputs.xxx}}`、`{{上游节点ID.output}}`、`{{上游节点ID.diff}}`、`{{feedback}}`）, `position: {{"x": number, "y": number}}`。
  2. `"shell"`: 执行命令。关键字段：`id`, `label`, `kind="shell"`, `command`（如 `"pytest -q"` 或 `"npm --prefix frontend run build"`）, `continue_on_error`（下游接 condition 判断测试成败时设为 `true`）, `join_mode`（当作为多个并行分支的汇合点时设为 `"all"`）。
  3. `"gate"`: 人工审批卡点。关键字段：`id`, `label`, `kind="gate"`, `instruction`, `review_nodes`（上游节点 ID 数组）, `allow_edit_output`（布尔值）, `rewind_targets`（打回可选的上游节点 ID 数组）。注意：Gate 节点只需连出“通过后前往的下游节点（或 `"END"`）”，无需为打回单独画边。
  4. `"condition"`: 条件路由/循环判断。关键字段：`id`, `label`, `kind="condition"`。它的每条出边在 `edges` 中通过 `condition: {{"field": "上游节点ID.ok", "op": "==", "value": true}}` 或 `"is_default": true` 区分分支（循环重试可判断 `"field": "编码节点ID.runs", "op": "<", "value": 3`）。
- `edges`: 连线数组，每项为 `{{"id": "e-src-tgt", "source": "起始节点ID", "target": "目标节点ID或END", "label": "可选连线文字", "condition": null或条件对象, "is_default": false或true}}`。
  - 并行执行：从同一个节点（如某个 Gate 或 Agent）连出多条普通无条件边指向不同节点（如 `gate -> backend_dev` 和 `gate -> frontend_dev`），再由它们共同连向一个 `join_mode="all"` 的汇聚节点，即构成并行分支与汇聚！
  - 每个终末节点必须有连向 `"END"` 的边。

### 3. 当前画布上的工作流 JSON
{current_json}

### 4. 用户的自然语言指令
{instruction}

### 5. 输出格式（严格只输出这一个 JSON 对象，不要附加 Markdown 解释）
{{
  "summary": "用一句话总结本次创建或修改了哪些节点与连线",
  "workflow": {{
    "id": "...",
    "name": "...",
    "description": "...",
    "entry_point": "...",
    "inputs": [...],
    "nodes": [...],
    "edges": [...]
  }}
}}
"""


def generate_or_modify_workflow(
    req: CopilotRequest,
    *,
    agent_registry: AgentRegistry,
    workflow_registry: WorkflowRegistry,
) -> CopilotResponse:
    """调用本地已登录 CLI Agent 将自然语言指令编译为经过验证的 WorkflowSpec。"""
    agents_info = [
        {"id": a.id, "name": a.name, "driver": a.driver, "available": a.available}
        for a in agent_registry.list_agents()
    ]
    prompt = _build_copilot_prompt(req.instruction, req.current_workflow, agents_info)

    # 在全新空临时目录以 read-only 模式运行 CLI，避免 Agent 扫描项目大目录耗时，极速返回 JSON
    with tempfile.TemporaryDirectory(prefix="aw-copilot-") as empty_dir:
        profile, res = agent_registry.execute(
            req.agent_id,
            prompt,
            cwd=empty_dir,
            sandbox="read-only",
            timeout_sec=240,
        )

    if not res.ok and not res.output:
        raise RuntimeError(f"调用 CLI Agent ({profile.id}) 构图失败: exit_code={res.exit_code}")

    parsed = extract_json_payload(res.output or res.stdout)
    wf_raw = parsed.get("workflow") if isinstance(parsed.get("workflow"), dict) else parsed
    summary = str(parsed.get("summary") or f"已按指令更新工作流：{req.instruction[:50]}")

    spec = WorkflowSpec.model_validate(wf_raw)
    spec = _auto_layout_missing_positions(spec)
    # 试编译 LangGraph，确保生成的状态机 100% 合法可运行
    build_state_graph(spec).compile()

    saved = False
    if req.auto_save:
        spec = workflow_registry.save_workflow(spec)
        saved = True

    return CopilotResponse(
        workflow=spec,
        summary=summary,
        agent_used=profile.id,
        saved=saved,
    )
