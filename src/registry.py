"""动态工作流注册中心（WorkflowRegistry）与 LangGraph 状态机编译器。

核心职责：
1. 定义声明式工作流模型 `WorkflowSpec`（包含 `NodeSpec` 与 `EdgeSpec`），既作为后端构图依据，也直接供给前端 React Flow 渲染。
2. 管理工作流持久化存储（默认保存在 `data/workflows/<id>.json`，同时兼容读取 `.yaml`/`.yml`）。
3. 将任意 `WorkflowSpec` 动态编译为支持 `interrupt()` / `Command(resume=...)` / `SqliteSaver` 的 LangGraph `StateGraph`。
"""

from __future__ import annotations

import datetime as dt
import json
import operator
import os
import re
import subprocess
from typing import Annotated, Any, Callable, Literal, Optional, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from src.agents.base import AgentResult, AgentTarget, StubAgent
from src.state import CodingState

NodeKind = Literal["agent", "gate", "shell", "summary"]
SandboxKind = Literal["read-only", "workspace-write"]
CompareOp = Literal["==", "!=", ">=", "<=", ">", "<", "truthy", "falsy"]


class NodePosition(BaseModel):
    x: float = 0.0
    y: float = 0.0


class NodeSpec(BaseModel):
    id: str = Field(..., description="节点唯一标识（图内唯一，不可为 START/END）")
    label: str = Field(..., description="节点显示名称（供 Web 画布展示）")
    kind: NodeKind = Field(..., description="节点类型：agent | gate | shell | summary")
    description: Optional[str] = Field(None, description="节点职责说明")
    position: Optional[NodePosition] = Field(None, description="前端画布坐标（可选，未传时前端自动布局）")

    # ---- agent 节点配置 ----
    prompt_template: Optional[str] = Field(
        None,
        description="agent 节点的提示词模板，支持 {task}、{plan}、{patch}、{feedback}、{test_output}、{attempt} 等 state 变量占位符",
    )
    sandbox: SandboxKind = Field(
        "read-only",
        description="agent 执行沙箱权限：read-only（只读分析）| workspace-write（允许修改代码）",
    )
    output_key: str = Field(
        "plan",
        description="节点输出写入 state 的字段名（如 plan / patch / review / test_output）",
    )
    capture_git_diff: bool = Field(
        False,
        description="是否在 agent 执行后抓取 repo 的 git diff 作为 output_key 的值（常用于 implement 节点）",
    )
    increment_attempt: bool = Field(
        False,
        description="进入节点时是否将 state['attempt'] + 1",
    )
    event_template: Optional[str] = Field(
        None,
        description="写入 events 列表的日志模板，支持 {id}、{attempt}、{ok_or_ng}、{pass_or_fail}、{action}",
    )

    # ---- gate（人工审批）节点配置 ----
    stage: str = Field("approval", description="人审阶段标识，如 plan_approval / final_approval")
    expose_keys: list[str] = Field(
        default_factory=lambda: ["task", "plan"],
        description="interrupt 时暴露给前端审批卡片展示的 state 字段列表",
    )
    options: list[str] = Field(
        default_factory=lambda: ["approve", "revise", "abort"],
        description="允许的人工审批动作列表",
    )

    # ---- shell（命令/测试）节点配置 ----
    cmd_key: str = Field("test_cmd", description="从 state 中读取要执行的 shell 命令字段名")
    default_cmd: Optional[str] = Field(None, description="当 state[cmd_key] 为空时使用的默认命令")
    fallback_prompt_template: str = Field(
        "TEST: 运行测试（第 {attempt} 次）",
        description="未配置 shell 命令时回退调用 agent 的 prompt 模板",
    )
    passed_key: str = Field("passed", description="命令是否成功（退出码 0）写入 state 的布尔字段名")

    # ---- summary（终态汇总）节点配置 ----
    result_template: str = Field(
        "完成：{task}",
        description="终态汇总写入 state['result'] 的文本模板",
    )
    abort_result_template: Optional[str] = Field(
        None,
        description="当 state['action'] == 'abort' 时优先使用的汇总模板",
    )


class RouteRule(BaseModel):
    field: str = Field(..., description="要判断的 state 字段名，如 action / passed / attempt")
    op: CompareOp = Field("==", description="比较运算符：== | != | >= | <= | > | < | truthy | falsy")
    value: Optional[Any] = Field(None, description="比较常量值（如 'approve'、True、2）")
    value_ref: Optional[str] = Field(None, description="动态引用另一个 state 字段进行比较（如 'max_attempts'）")
    target: str = Field(..., description="命中条件时跳转的目标节点 ID（或 'END'）")
    label: Optional[str] = Field(None, description="条件分支在画布上的连线标签")


class EdgeSpec(BaseModel):
    source: str = Field(..., description="起始节点 ID")
    target: Optional[str] = Field(None, description="直接连线的目标节点 ID（或 'END'）；若为条件边则留空")
    conditions: list[RouteRule] = Field(
        default_factory=list,
        description="条件路由规则列表（按顺序匹配，首个命中的规则生效）",
    )
    default_target: Optional[str] = Field(
        None,
        description="条件边所有规则均未命中时的兜底目标节点 ID（或 'END'）",
    )
    label: Optional[str] = Field(None, description="普通边在画布上的连线标签")


class WorkflowSpec(BaseModel):
    id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]+$", description="工作流唯一 ID（字母、数字、下划线、连字符）")
    name: str = Field(..., description="工作流名称")
    description: str = Field("", description="工作流用途描述")
    builtin: bool = Field(False, description="是否为系统内置工作流")
    entry_point: str = Field(..., description="入口节点 ID（START 指向的第一个节点）")
    nodes: list[NodeSpec] = Field(..., min_length=1, description="工作流节点列表")
    edges: list[EdgeSpec] = Field(..., min_length=1, description="工作流连线与条件路由列表")
    updated_at: Optional[str] = Field(None, description="最近更新时间（ISO-8601）")


# ---------------------------------------------------------------- 默认内置 coding 工作流
def default_coding_workflow_spec() -> WorkflowSpec:
    """与 src/workflows/coding.py 语义 100% 等价的声明式工作流定义。"""
    return WorkflowSpec(
        id="coding",
        name="标准研发工作流 (Coding)",
        description="plan -> 人工审批 -> implement -> test (自动修复循环) -> review -> 人工审批 -> finish",
        builtin=True,
        entry_point="plan",
        updated_at="2026-10-27T00:00:00Z",
        nodes=[
            NodeSpec(
                id="plan",
                label="生成计划 (Plan)",
                kind="agent",
                description="分析任务与人工反馈，输出分步实现计划",
                position=NodePosition(x=80, y=180),
                prompt_template="PLAN: 为以下任务给出分步实现计划\n任务：{task}\n人工反馈：{feedback}",
                sandbox="read-only",
                output_key="plan",
                event_template="plan",
            ),
            NodeSpec(
                id="plan_gate",
                label="计划审批 (Plan Gate)",
                kind="gate",
                description="人工确认计划：通过后进入编码，打回则重新规划",
                position=NodePosition(x=300, y=180),
                stage="plan_approval",
                expose_keys=["task", "plan"],
                options=["approve", "revise", "abort"],
                event_template="plan_gate:{action}",
            ),
            NodeSpec(
                id="implement",
                label="编码实现 (Implement)",
                kind="agent",
                description="按批准的计划与测试反馈修改代码并抓取 git diff",
                position=NodePosition(x=540, y=180),
                prompt_template="IMPLEMENT: 按计划实现改动（第 {attempt} 次）\n计划：{plan}\n上次测试输出：{test_output}",
                sandbox="workspace-write",
                output_key="patch",
                capture_git_diff=True,
                increment_attempt=True,
                event_template="implement#{attempt}",
            ),
            NodeSpec(
                id="test",
                label="运行测试 (Test)",
                kind="shell",
                description="执行测试命令或交由 Agent 验证，决定进入审查还是修复循环",
                position=NodePosition(x=780, y=180),
                cmd_key="test_cmd",
                fallback_prompt_template="TEST: 运行测试（第 {attempt} 次）",
                output_key="test_output",
                passed_key="passed",
                event_template="test#{attempt}:{pass_or_fail}",
            ),
            NodeSpec(
                id="review",
                label="代码审查 (Review)",
                kind="agent",
                description="对代码 diff 进行质量与安全性审查",
                position=NodePosition(x=1020, y=100),
                prompt_template="REVIEW: 审查以下改动\n{patch}",
                sandbox="read-only",
                output_key="review",
                event_template="review:{ok_or_ng}",
            ),
            NodeSpec(
                id="final_gate",
                label="合并审批 (Final Gate)",
                kind="gate",
                description="合并/提 MR 前最终人工确认",
                position=NodePosition(x=1260, y=100),
                stage="final_approval",
                expose_keys=["patch", "test_output", "review"],
                options=["approve", "revise", "abort"],
                event_template="final_gate:{action}",
            ),
            NodeSpec(
                id="finish",
                label="完成 (Finish)",
                kind="summary",
                description="汇总执行结果并结束工作流",
                position=NodePosition(x=1500, y=180),
                result_template="完成：{task} | 尝试 {attempt} 次 | 测试 {test_output} | review {review}",
                abort_result_template="已中止（人工 abort）",
                event_template="finish",
            ),
            NodeSpec(
                id="give_up",
                label="转人工 (Give Up)",
                kind="summary",
                description="达到最大尝试次数仍未通过测试，转人工介入",
                position=NodePosition(x=1020, y=300),
                result_template="测试 {max_attempts} 次仍未通过，转人工：{test_output}",
                event_template="give_up",
            ),
        ],
        edges=[
            EdgeSpec(source="plan", target="plan_gate"),
            EdgeSpec(
                source="plan_gate",
                conditions=[
                    RouteRule(field="action", op="==", value="approve", target="implement", label="approve"),
                    RouteRule(field="action", op="==", value="abort", target="finish", label="abort"),
                ],
                default_target="plan",
                label="revise",
            ),
            EdgeSpec(source="implement", target="test"),
            EdgeSpec(
                source="test",
                conditions=[
                    RouteRule(field="passed", op="==", value=True, target="review", label="pass"),
                    RouteRule(field="attempt", op=">=", value_ref="max_attempts", target="give_up", label="max_attempts"),
                ],
                default_target="implement",
                label="fail (retry)",
            ),
            EdgeSpec(source="review", target="final_gate"),
            EdgeSpec(
                source="final_gate",
                conditions=[
                    RouteRule(field="action", op="==", value="approve", target="finish", label="approve"),
                    RouteRule(field="action", op="==", value="abort", target="finish", label="abort"),
                ],
                default_target="implement",
                label="revise",
            ),
            EdgeSpec(source="finish", target="END"),
            EdgeSpec(source="give_up", target="END"),
        ],
    )


# ---------------------------------------------------------------- 动态构图辅助函数
class _SafeFormatDict(dict):
    """格式化模板时，缺失或为 None 的字段回退为默认值，避免 KeyError。"""

    def __missing__(self, key: str) -> str:
        return "无"


def _render_template(template: str, state: dict[str, Any], extra: Optional[dict[str, Any]] = None) -> str:
    merged: dict[str, Any] = {}
    for k, v in state.items():
        merged[k] = "无" if v is None or v == "" else v
    if extra:
        for k, v in extra.items():
            merged[k] = "无" if v is None or v == "" else v
    return template.format_map(_SafeFormatDict(merged))


def _git_diff(repo: str, limit: int = 20000) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", repo, "diff"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return out.stdout[:limit].strip()
    except Exception:  # noqa: BLE001
        return ""


def _eval_rule(rule: RouteRule, state: dict[str, Any]) -> bool:
    left = state.get(rule.field)
    right = state.get(rule.value_ref) if rule.value_ref else rule.value
    # 特殊默认值：max_attempts 默认 2，attempt 默认 0
    if rule.value_ref == "max_attempts" and right is None:
        right = 2
    if rule.field == "attempt" and left is None:
        left = 0

    op = rule.op
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == "truthy":
        return bool(left)
    if op == "falsy":
        return not bool(left)
    try:
        if op == ">=":
            return left >= right  # type: ignore[operator]
        if op == "<=":
            return left <= right  # type: ignore[operator]
        if op == ">":
            return left > right  # type: ignore[operator]
        if op == "<":
            return left < right  # type: ignore[operator]
    except TypeError:
        return False
    return False


def _build_dynamic_state_type(spec: WorkflowSpec) -> Any:
    """基于 CodingState 动态扩展自定义工作流中用到的额外字段，防止 LangGraph 丢弃未知 key。"""
    annotations: dict[str, Any] = dict(CodingState.__annotations__)
    extra_keys: set[str] = set()
    for node in spec.nodes:
        if node.output_key:
            extra_keys.add(node.output_key)
        if node.passed_key:
            extra_keys.add(node.passed_key)
        if node.cmd_key:
            extra_keys.add(node.cmd_key)
        for k in node.expose_keys:
            extra_keys.add(k)
    for edge in spec.edges:
        for cond in edge.conditions:
            extra_keys.add(cond.field)
            if cond.value_ref:
                extra_keys.add(cond.value_ref)

    for k in extra_keys:
        if k not in annotations and k != "events":
            annotations[k] = Any
    annotations["events"] = Annotated[list[str], operator.add]
    return TypedDict(f"WorkflowState_{ re.sub(r'[^a-zA-Z0-9_]', '_', spec.id) }", annotations, total=False)  # type: ignore[misc]


def _make_node_fn(node: NodeSpec, get_agent: Callable[[], AgentTarget]) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """根据 NodeSpec 生成 LangGraph 节点函数。"""

    if node.kind == "agent":
        def _agent_node(state: dict[str, Any]) -> dict[str, Any]:
            attempt = state.get("attempt", 0)
            if node.increment_attempt:
                attempt = int(attempt or 0) + 1
            extra = {"id": node.id, "attempt": attempt}
            tpl = node.prompt_template or f"PLAN: 执行节点 {node.label}\n任务：{{task}}"
            prompt = _render_template(tpl, state, extra=extra)
            # 若未带标准前缀且配置了 workspace-write，则自动补 IMPLEMENT: 前缀以驱动 CodexAgent 沙箱
            if ":" not in prompt.split("\n", 1)[0]:
                prefix = "IMPLEMENT" if node.sandbox == "workspace-write" else "PLAN"
                prompt = f"{prefix}: {prompt}"

            res: AgentResult = get_agent().run(prompt)
            output_val = res["output"]
            if node.capture_git_diff:
                diff = _git_diff(state.get("repo", "."))
                if diff:
                    output_val = diff

            ev_tpl = node.event_template or node.id
            ev = _render_template(
                ev_tpl,
                state,
                extra={
                    "id": node.id,
                    "attempt": attempt,
                    "ok_or_ng": "ok" if res["ok"] else "ng",
                    "pass_or_fail": "pass" if res["ok"] else "fail",
                },
            )
            update: dict[str, Any] = {
                node.output_key: output_val,
                "events": [ev],
            }
            if node.increment_attempt:
                update["attempt"] = attempt
            return update

        _agent_node.__name__ = f"node_{node.id}"
        return _agent_node

    if node.kind == "gate":
        def _gate_node(state: dict[str, Any]) -> dict[str, Any]:
            payload: dict[str, Any] = {"stage": node.stage}
            for k in node.expose_keys:
                payload[k] = state.get(k)
            payload["options"] = list(node.options)

            decision = interrupt(payload)
            if isinstance(decision, dict):
                action = str(decision.get("action", "approve"))
                feedback = str(decision.get("feedback", ""))
            else:
                action, feedback = str(decision), ""

            ev_tpl = node.event_template or f"{node.id}:{{action}}"
            ev = _render_template(ev_tpl, state, extra={"id": node.id, "action": action})
            return {
                "action": action,
                "feedback": feedback,
                "approved": action == "approve",
                "events": [ev],
            }

        _gate_node.__name__ = f"node_{node.id}"
        return _gate_node

    if node.kind == "shell":
        def _shell_node(state: dict[str, Any]) -> dict[str, Any]:
            cmd = state.get(node.cmd_key) or node.default_cmd
            attempt = state.get("attempt", 0)
            if cmd:
                try:
                    out = subprocess.run(
                        cmd,
                        shell=True,
                        cwd=state.get("repo", "."),
                        capture_output=True,
                        text=True,
                        timeout=1800,
                        check=False,
                    )
                    ok = out.returncode == 0
                    text = (out.stdout + out.stderr)[-6000:]
                except Exception as exc:  # noqa: BLE001
                    ok, text = False, f"测试命令执行失败: {exc}"
            else:
                prompt = _render_template(
                    node.fallback_prompt_template,
                    state,
                    extra={"id": node.id, "attempt": attempt},
                )
                res = get_agent().run(prompt)
                ok, text = res["ok"], res["output"]

            ev_tpl = node.event_template or f"{node.id}#{{attempt}}:{{pass_or_fail}}"
            ev = _render_template(
                ev_tpl,
                state,
                extra={
                    "id": node.id,
                    "attempt": attempt,
                    "pass_or_fail": "pass" if ok else "fail",
                    "ok_or_ng": "ok" if ok else "ng",
                },
            )
            return {
                node.passed_key: ok,
                node.output_key: text,
                "events": [ev],
            }

        _shell_node.__name__ = f"node_{node.id}"
        return _shell_node

    if node.kind == "summary":
        def _summary_node(state: dict[str, Any]) -> dict[str, Any]:
            if node.abort_result_template and state.get("action") == "abort":
                result = _render_template(node.abort_result_template, state, extra={"id": node.id})
            else:
                result = _render_template(node.result_template, state, extra={"id": node.id})
            ev_tpl = node.event_template or node.id
            ev = _render_template(ev_tpl, state, extra={"id": node.id})
            return {"result": result, "events": [ev]}

        _summary_node.__name__ = f"node_{node.id}"
        return _summary_node

    raise ValueError(f"未知的节点类型: {node.kind}")


def validate_workflow_spec(spec: WorkflowSpec) -> None:
    """校验 WorkflowSpec 的拓扑完整性与节点引用合法性。"""
    node_ids = {n.id for n in spec.nodes}
    if len(node_ids) != len(spec.nodes):
        raise ValueError("工作流包含重复的节点 ID")
    for reserved in ("START", "END", "__start__", "__end__"):
        if reserved in node_ids:
            raise ValueError(f"节点 ID 不能使用保留字: {reserved}")
    if spec.entry_point not in node_ids:
        raise ValueError(f"entry_point '{spec.entry_point}' 不在节点列表中: {sorted(node_ids)}")

    valid_targets = node_ids | {"END"}
    for edge in spec.edges:
        if edge.source not in node_ids:
            raise ValueError(f"边的 source '{edge.source}' 不在节点列表中")
        if edge.conditions:
            if not edge.default_target:
                raise ValueError(f"条件边 '{edge.source}' 必须指定 default_target")
            if edge.default_target not in valid_targets:
                raise ValueError(f"条件边 '{edge.source}' 的 default_target '{edge.default_target}' 无效")
            for rule in edge.conditions:
                if rule.target not in valid_targets:
                    raise ValueError(f"条件边 '{edge.source}' 的分支目标 '{rule.target}' 无效")
        else:
            if not edge.target:
                raise ValueError(f"普通边 '{edge.source}' 必须指定 target")
            if edge.target not in valid_targets:
                raise ValueError(f"普通边 '{edge.source}' 的 target '{edge.target}' 无效")


def build_state_graph(
    spec: WorkflowSpec,
    agent: AgentTarget | Callable[[], AgentTarget] | None = None,
) -> StateGraph:
    """将 WorkflowSpec 动态编译为未绑 checkpointer 的 LangGraph StateGraph builder。"""
    validate_workflow_spec(spec)

    if callable(agent):
        get_agent = agent
    elif agent is not None:
        get_agent = lambda: agent  # noqa: E731
    else:
        default_stub = StubAgent()
        get_agent = lambda: default_stub  # noqa: E731

    state_type = _build_dynamic_state_type(spec)
    builder = StateGraph(state_type)

    for node in spec.nodes:
        builder.add_node(node.id, _make_node_fn(node, get_agent))

    builder.add_edge(START, spec.entry_point)

    for edge in spec.edges:
        if edge.conditions:
            rules = list(edge.conditions)
            default_target = edge.default_target or "END"
            path_map: dict[str, Any] = {}
            for r in rules:
                path_map[r.target] = END if r.target == "END" else r.target
            path_map[default_target] = END if default_target == "END" else default_target

            def _make_router(edge_rules: list[RouteRule], fallback: str) -> Callable[[dict[str, Any]], str]:
                def _router(state: dict[str, Any]) -> str:
                    for rule in edge_rules:
                        if _eval_rule(rule, state):
                            return rule.target
                    return fallback

                return _router

            builder.add_conditional_edges(
                edge.source,
                _make_router(rules, default_target),
                path_map,
            )
        else:
            target = END if edge.target == "END" else edge.target
            builder.add_edge(edge.source, target)

    return builder


# ---------------------------------------------------------------- 工作流注册中心
class WorkflowRegistry:
    """负责加载、保存、枚举和动态编译工作流。"""

    def __init__(self, workflows_dir: str) -> None:
        self.workflows_dir = os.path.abspath(workflows_dir)
        os.makedirs(self.workflows_dir, exist_ok=True)
        self._ensure_builtin_seed()

    def _ensure_builtin_seed(self) -> None:
        coding_path = os.path.join(self.workflows_dir, "coding.json")
        if not os.path.isfile(coding_path):
            spec = default_coding_workflow_spec()
            self._write_spec_file(spec)

    def _spec_path(self, workflow_id: str) -> str:
        return os.path.join(self.workflows_dir, f"{workflow_id}.json")

    def _write_spec_file(self, spec: WorkflowSpec) -> str:
        path = self._spec_path(spec.id)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(spec.model_dump(mode="json"), fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        return path

    def list_workflows(self) -> list[WorkflowSpec]:
        specs: dict[str, WorkflowSpec] = {}
        # 始终保底提供内置 coding 工作流
        builtin_coding = default_coding_workflow_spec()
        specs[builtin_coding.id] = builtin_coding

        if os.path.isdir(self.workflows_dir):
            for fname in sorted(os.listdir(self.workflows_dir)):
                fpath = os.path.join(self.workflows_dir, fname)
                if not os.path.isfile(fpath):
                    continue
                try:
                    if fname.endswith(".json"):
                        with open(fpath, encoding="utf-8") as fh:
                            raw = json.load(fh)
                        spec = WorkflowSpec.model_validate(raw)
                        specs[spec.id] = spec
                    elif fname.endswith((".yaml", ".yml")):
                        import yaml

                        with open(fpath, encoding="utf-8") as fh:
                            raw = yaml.safe_load(fh)
                        spec = WorkflowSpec.model_validate(raw)
                        specs[spec.id] = spec
                except Exception as exc:  # noqa: BLE001
                    print(f"[warn] 跳过无效的工作流文件 {fpath}: {exc}", flush=True)

        result = list(specs.values())
        result.sort(key=lambda s: (0 if s.id == "coding" else 1, s.id))
        return result

    def get_workflow(self, workflow_id: str) -> Optional[WorkflowSpec]:
        for suffix in (".json", ".yaml", ".yml"):
            fpath = os.path.join(self.workflows_dir, f"{workflow_id}{suffix}")
            if os.path.isfile(fpath):
                try:
                    if suffix == ".json":
                        with open(fpath, encoding="utf-8") as fh:
                            return WorkflowSpec.model_validate(json.load(fh))
                    import yaml

                    with open(fpath, encoding="utf-8") as fh:
                        return WorkflowSpec.model_validate(yaml.safe_load(fh))
                except Exception:  # noqa: BLE001
                    break
        if workflow_id == "coding":
            return default_coding_workflow_spec()
        return None

    def save_workflow(self, spec: WorkflowSpec) -> WorkflowSpec:
        """校验并保存工作流到磁盘（先通过动态构图验证合法性）。"""
        # 试编译以验证节点、边与状态机合法性
        build_state_graph(spec).compile()
        now_iso = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        updated = spec.model_copy(
            update={
                "builtin": spec.id == "coding",
                "updated_at": now_iso,
            }
        )
        self._write_spec_file(updated)
        return updated

    def delete_workflow(self, workflow_id: str) -> bool:
        if workflow_id == "coding":
            raise ValueError("内置 'coding' 工作流不可删除")
        removed = False
        for suffix in (".json", ".yaml", ".yml"):
            fpath = os.path.join(self.workflows_dir, f"{workflow_id}{suffix}")
            if os.path.isfile(fpath):
                os.unlink(fpath)
                removed = True
        return removed

    def compile_graph(
        self,
        workflow_id: str,
        *,
        agent: AgentTarget | Callable[[], AgentTarget] | None = None,
        checkpointer: Any = None,
    ) -> Any:
        spec = self.get_workflow(workflow_id)
        if spec is None:
            raise KeyError(f"未找到工作流: {workflow_id}")
        builder = build_state_graph(spec, agent=agent)
        return builder.compile(checkpointer=checkpointer)
