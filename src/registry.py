"""动态工作流注册中心（WorkflowRegistry v2）与 LangGraph 状态机编译器。

核心能力：
1. 四类标准编排节点：
   - agent     : 绑定具体 CLI Agent（codex / claude / kimi / agy / custom）与工作目录、读写沙箱
   - shell     : 执行构建、测试、Git 同步或校验脚本，捕获退出码与标准输出
   - gate      : 增强型人工卡点，支持在线编辑上游节点产出后放行、自选打回到任意上游节点、或中止运行
   - condition : 结构化条件路由与循环控制（支持判断任意上游节点的 ok / exit_code / output / runs 等）
   （同时向后兼容旧版 summary 节点与旧版 EdgeSpec.conditions 格式）
2. 原生支持串行、条件分支、带最大次数保护（max_runs）的循环重试，以及并行分支与屏障汇聚（Fan-out / Join）。
3. 节点级命名空间状态：每个节点产出独立保存在 state['nodes'][node_id]（含 output / diff / stdout / exit_code / runs），
   下游通过 {node_id.output}、{node_id.diff}、{inputs.xxx}、{task}、{feedback} 安全引用。
4. 所有工作流均可自由增删节点、修改拓扑或删除（首次空目录初始化示例模板后不再强行锁死内置流）。
"""

from __future__ import annotations

import datetime as dt
import json
import operator
import os
import re
import subprocess
from collections import deque
from typing import Annotated, Any, Callable, Literal, Optional, Protocol, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field, model_validator

from src.agents.registry import CliExecResult

NodeKind = Literal["agent", "shell", "gate", "condition", "summary"]
SandboxKind = Literal["read-only", "workspace-write"]
JoinMode = Literal["auto", "all", "any"]
CompareOp = Literal[
    "==",
    "!=",
    ">=",
    "<=",
    ">",
    "<",
    "contains",
    "not_contains",
    "regex",
    "truthy",
    "falsy",
]
InputFieldKind = Literal["textarea", "text", "path", "number", "boolean", "select"]


# ---------------------------------------------------------------- State Reducers
def merge_dict_reducer(left: Optional[dict[str, Any]], right: Optional[dict[str, Any]]) -> dict[str, Any]:
    """并发安全的字典合并 Reducer：支持多个并行节点在同一 superstep 写入各自的 key；传 {'__reset__': True} 可重置。"""
    if right and right.get("__reset__"):
        cleaned = {k: v for k, v in right.items() if k != "__reset__"}
        return cleaned
    merged = dict(left or {})
    if right:
        for k, v in right.items():
            if isinstance(v, dict) and isinstance(merged.get(k), dict):
                merged[k] = {**merged[k], **v}
            else:
                merged[k] = v
    return merged


class NodeResultData(TypedDict, total=False):
    node_id: str
    label: str
    kind: str
    status: str  # running | succeeded | failed | approved | revised | aborted | skipped
    ok: bool
    exit_code: int
    output: str
    stdout: str
    diff: str
    runs: int
    worker_id: str
    agent_id: str
    cwd: str
    duration_ms: int
    started_at: str
    finished_at: str
    edited_by_human: bool
    error: str


def last_str_reducer(left: Optional[str], right: Optional[str]) -> str:
    """并发安全字符串覆盖 Reducer：允许多个并行节点在同一 superstep 更新最近输出而不触发 InvalidUpdateError。"""
    return right if right is not None else (left or "")


class WorkflowGraphState(TypedDict, total=False):
    # 工作流启动入参（含 task 及用户自定义字段）
    inputs: Annotated[dict[str, Any], merge_dict_reducer]
    # 顶层快捷字段（全部带并发安全 reducer，避免并行分支写冲突）
    task: Annotated[str, last_str_reducer]
    repo: Annotated[str, last_str_reducer]
    feedback: Annotated[str, last_str_reducer]
    action: Annotated[str, last_str_reducer]
    result: Annotated[str, last_str_reducer]
    # 各节点结构化执行结果命名空间：nodes[node_id] -> NodeResultData
    nodes: Annotated[dict[str, NodeResultData], merge_dict_reducer]
    # 人工打回给特定节点的定向反馈：feedback_by_node[node_id] -> feedback_str
    feedback_by_node: Annotated[dict[str, str], merge_dict_reducer]
    # 自动循环计数器（人工打回时可重置，防止无人值守死循环）
    auto_runs: Annotated[dict[str, int], merge_dict_reducer]
    # 单分支打回时的并行屏障直通标记：noop_passthrough[node_id] -> True
    noop_passthrough: Annotated[dict[str, bool], merge_dict_reducer]
    # 单节点重试时的临时参数覆盖：node_overrides[node_id] -> {prompt_template, command, agent_id, cwd}
    node_overrides: Annotated[dict[str, dict[str, Any]], merge_dict_reducer]
    # 追加式事件流
    events: Annotated[list[str], operator.add]


# ---------------------------------------------------------------- Declarative Schema v2
class NodePosition(BaseModel):
    x: float = 0.0
    y: float = 0.0


class WorkflowInputSpec(BaseModel):
    key: str = Field(..., pattern=r"^[a-zA-Z_][a-zA-Z0-9_]*$", description="入参变量名，模板中通过 {inputs.key} 引用")
    label: str = Field(..., description="启动运行弹窗中的表单标签")
    kind: InputFieldKind = Field("text", description="表单控件类型：textarea | text | path | number | boolean | select")
    required: bool = Field(False, description="是否必填")
    default: Optional[Any] = Field(None, description="默认值")
    placeholder: Optional[str] = Field(None, description="输入占位提示")
    description: Optional[str] = Field(None, description="字段说明")
    options: list[str] = Field(default_factory=list, description="当 kind='select' 时的可选值列表")


class EdgeCondition(BaseModel):
    """绑定在单条连线上的条件表达式（画布上 1 条边对应 1 个条件，直观易编辑）。"""

    field: str = Field(
        ...,
        description="要判断的变量路径，支持 '节点ID.ok'、'节点ID.exit_code'、'节点ID.output'、'节点ID.runs'、'inputs.变量名'",
    )
    op: CompareOp = Field("==", description="运算符：== | != | >= | <= | > | < | contains | not_contains | regex | truthy | falsy")
    value: Optional[Any] = Field(None, description="比较常量值（如 true、0、'PASS'）")
    value_ref: Optional[str] = Field(None, description="可选：引用另一个变量路径进行动态比较")


class RouteRule(BaseModel):
    """兼容多规则列表模式的路由项（包含 target）。"""

    field: str = Field(..., description="要判断的变量路径，如 'test.ok' / 'test.runs' / 'inputs.branch'")
    op: CompareOp = Field("==", description="比较运算符")
    value: Optional[Any] = Field(None, description="比较常量值")
    value_ref: Optional[str] = Field(None, description="动态比较的另一变量路径")
    target: str = Field(..., description="命中时跳转的目标节点 ID（或 'END'）")
    label: Optional[str] = Field(None, description="画布连线标签")


class NodeSpec(BaseModel):
    id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]+$", description="节点唯一 ID（不可为 START/END）")
    label: str = Field(..., description="节点显示名称")
    kind: NodeKind = Field(..., description="节点类型：agent | shell | gate | condition")
    description: Optional[str] = Field(None, description="节点职责备注")
    position: Optional[NodePosition] = Field(None, description="画布坐标")
    join_mode: JoinMode = Field(
        "auto",
        description="多入边汇聚策略：auto（自动识别并行汇聚）| all（等待所有并行前置完成）| any（任一前置到达即执行）",
    )

    # ---- 执行位置与工作区（一期默认 local，二期无缝解锁远端 Worker 与 Worktree） ----
    worker_id: str = Field("local", description="执行主机 Worker ID（默认 'local' 表示操作台所在机器）")
    cwd: Optional[str] = Field(
        None,
        description="节点工作目录（支持绝对路径、相对工作区子目录如 'frontend'，或模板变量如 '{inputs.repo}'；留空使用运行默认仓库目录）",
    )
    use_worktree: bool = Field(False, description="是否启用独立 Git Worktree 隔离（二期能力预留）")
    max_runs: int = Field(8, ge=1, le=50, description="单次运行内该节点自动循环上限（防死循环保护）")
    timeout_sec: int = Field(1800, ge=5, le=14400, description="节点单次执行超时秒数")

    # ---- agent 节点配置 ----
    agent_id: str = Field(
        "auto",
        description="要调用的 CLI Agent ID（如 'codex' / 'claude' / 'kimi' / 'agy' / 自定义 ID；'auto' 自动选用首个就绪 CLI）",
    )
    prompt_template: Optional[str] = Field(
        None,
        description="发给 CLI Agent 的提示词模板，支持 {task}、{inputs.xxx}、{上游节点ID.output}、{上游节点ID.diff}、{feedback}",
    )
    sandbox: SandboxKind = Field(
        "workspace-write",
        description="CLI 沙箱权限：read-only（只读分析/规划/审查）| workspace-write（允许读写代码文件）",
    )
    capture_git_diff: bool = Field(
        True,
        description="执行后是否抓取工作目录的 git diff 存入 {节点ID.diff}",
    )

    # ---- shell 节点配置 ----
    command: Optional[str] = Field(
        None,
        description="Shell 命令模板，支持 {task}、{inputs.xxx}、{上游节点ID.output} 等变量替换",
    )
    continue_on_error: bool = Field(
        False,
        description="退出码非 0 时是否继续流转（当下游连向 condition 节点时会自动允许流转以进入判断分支；独立命令失败则默认停住等待人工重试）",
    )

    # ---- gate（人工卡点）节点配置 ----
    stage: str = Field("approval", description="卡点阶段标识")
    instruction: Optional[str] = Field(
        None,
        description="展示给审批人的提示说明（支持模板变量）",
    )
    review_nodes: list[str] = Field(
        default_factory=list,
        description="在审批卡片中重点展示产出的上游节点 ID 列表（留空则自动展示直接上游节点）",
    )
    allow_edit_output: bool = Field(
        True,
        description="是否允许审批人直接在线编辑修改上游节点的 output 文本后再放行",
    )
    rewind_targets: list[str] = Field(
        default_factory=list,
        description="点击『打回』时可选的目标上游节点 ID 列表（留空则自动计算全部上游祖先节点供选择）",
    )

    # ---- condition / 兼容旧版字段 ----
    result_template: Optional[str] = Field(None, description="（兼容 summary 节点）汇总输出模板")
    abort_result_template: Optional[str] = Field(None, description="（兼容 summary 节点）中止汇总模板")
    output_key: Optional[str] = Field(None, description="（向后兼容 v1）额外镜像写入顶层 state 的字段名")
    cmd_key: Optional[str] = Field(None, description="（向后兼容 v1）从 inputs/state 读取命令的字段名")
    default_cmd: Optional[str] = Field(None, description="（向后兼容 v1）默认 shell 命令")
    expose_keys: list[str] = Field(default_factory=list, description="（向后兼容 v1）暴露的顶层字段")
    options: list[str] = Field(
        default_factory=lambda: ["approve", "revise", "abort"],
        description="允许的审批动作",
    )


class EdgeSpec(BaseModel):
    id: Optional[str] = Field(None, description="连线唯一 ID（供前端画布选中/编辑/删除单条连线）")
    source: str = Field(..., description="起始节点 ID")
    target: Optional[str] = Field(None, description="目标节点 ID（或 'END'）")
    label: Optional[str] = Field(None, description="连线显示标签")
    condition: Optional[EdgeCondition] = Field(
        None,
        description="单边条件规则（当起始节点为 condition 或带条件分支时生效）",
    )
    is_default: bool = Field(
        False,
        description="是否为条件分支的兜底（else / default）连线",
    )
    # 兼容 v1 聚合条件边写法
    conditions: list[RouteRule] = Field(
        default_factory=list,
        description="（兼容聚合写法）多分支条件路由列表",
    )
    default_target: Optional[str] = Field(
        None,
        description="（兼容聚合写法）所有条件未命中时的兜底目标节点 ID",
    )


def default_workflow_inputs() -> list[WorkflowInputSpec]:
    return [
        WorkflowInputSpec(
            key="task",
            label="任务描述",
            kind="textarea",
            required=True,
            placeholder="输入本次要完成的需求、问题修复或协同研发目标…",
        ),
        WorkflowInputSpec(
            key="repo",
            label="默认工作目录（可选）",
            kind="path",
            required=False,
            placeholder="留空则使用各节点配置的目录或操作台默认仓库路径",
        ),
    ]


class WorkflowSpec(BaseModel):
    id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]+$", description="工作流唯一 ID（字母、数字、下划线、连字符）")
    name: str = Field(..., description="工作流名称")
    description: str = Field("", description="工作流用途描述")
    builtin: bool = Field(False, description="是否为系统模板（所有工作流均可自由编辑和删除）")
    inputs: list[WorkflowInputSpec] = Field(
        default_factory=default_workflow_inputs,
        description="启动该工作流时的动态表单参数定义（默认只需填 task）",
    )
    entry_point: str = Field(..., description="入口节点 ID（START 指向的首个节点；若存在多个并行入口也可在 edges 中由 START 连出）")
    nodes: list[NodeSpec] = Field(..., min_length=1, description="工作流节点列表")
    edges: list[EdgeSpec] = Field(default_factory=list, description="工作流连线列表")
    updated_at: Optional[str] = Field(None, description="最近更新时间（ISO-8601）")

    @model_validator(mode="after")
    def _ensure_edge_ids_and_task_input(self) -> "WorkflowSpec":
        # 自动为缺失 id 的边补齐稳定 id，方便前端 React Flow 直接按 id 增删改单条边
        seen_ids: set[str] = set()
        for idx, edge in enumerate(self.edges):
            if not edge.id or edge.id in seen_ids:
                tgt = edge.target or edge.default_target or "cond"
                candidate = f"e-{edge.source}-{tgt}-{idx}"
                edge.id = candidate
            seen_ids.add(edge.id)
        # 确保至少存在一个输入项（若为空列表则补默认 task）
        if not self.inputs:
            self.inputs = default_workflow_inputs()
        return self


# ---------------------------------------------------------------- Runtime Hook Protocol
class NodeRuntimeHooks(Protocol):
    """节点运行期外部能力钩子（解耦图编译器与具体本地/远端 Worker 及日志存储）。"""

    default_repo: str

    def execute_agent(
        self,
        *,
        thread_id: str,
        node: NodeSpec,
        prompt: str,
        cwd: str,
    ) -> tuple[str, CliExecResult]:
        """返回 (实际使用的 agent_id, CliExecResult)。"""
        ...

    def execute_shell(
        self,
        *,
        thread_id: str,
        node: NodeSpec,
        command: str,
        cwd: str,
    ) -> CliExecResult:
        ...

    def emit_log(self, thread_id: str, node_id: str, stream: str, line: str) -> None:
        ...

    def on_node_start(self, thread_id: str, node: NodeSpec, cwd: str, run_count: int) -> None:
        ...

    def on_node_end(self, thread_id: str, node: NodeSpec, result: NodeResultData) -> None:
        ...


class NodeExecutionError(RuntimeError):
    """节点执行失败异常（触发后图停在该节点 checkpoint，允许用户在操作台一键原地重试）。"""

    def __init__(self, node_id: str, message: str, result: Optional[NodeResultData] = None) -> None:
        super().__init__(message)
        self.node_id = node_id
        self.result = result


# ---------------------------------------------------------------- 初始示例模板（完全可编辑、可增删节点、可删除）
def seed_iterative_coding_spec() -> WorkflowSpec:
    """示例模板 1：可自由增删节点的单机迭代研发流。"""
    return WorkflowSpec(
        id="iterative-coding",
        name="迭代研发与测试闭环",
        description="规划方案 -> 人工确认(可直接在线改方案) -> 编码实现 -> Shell 测试 -> 条件判断(失败自动循环修复) -> 最终验收",
        builtin=False,
        entry_point="plan",
        inputs=[
            WorkflowInputSpec(
                key="task",
                label="研发任务描述",
                kind="textarea",
                required=True,
                placeholder="例如：为某个模块增加参数校验并补充单元测试",
            ),
            WorkflowInputSpec(
                key="repo",
                label="目标仓库路径",
                kind="path",
                required=False,
                placeholder="留空则使用操作台当前工作区目录",
            ),
            WorkflowInputSpec(
                key="test_cmd",
                label="测试命令",
                kind="text",
                required=False,
                default="pytest -q",
                placeholder="例如：pytest -q 或 npm test",
            ),
        ],
        nodes=[
            NodeSpec(
                id="plan",
                label="需求分析与方案",
                kind="agent",
                description="只读分析代码库并给出可执行的实现步骤",
                position=NodePosition(x=60, y=180),
                agent_id="codex",
                sandbox="read-only",
                capture_git_diff=False,
                prompt_template=(
                    "请分析当前仓库并为以下研发任务制定清晰的分步技术方案（只读分析，不要修改文件）。\n\n"
                    "【任务目标】\n{task}\n\n"
                    "【人工修订反馈】\n{feedback}"
                ),
            ),
            NodeSpec(
                id="plan_gate",
                label="方案确认卡点",
                kind="gate",
                description="审阅或直接手动编辑方案文本，通过后开始改代码，也可打回重新规划",
                position=NodePosition(x=320, y=180),
                stage="plan_approval",
                instruction="请确认方案；你可以直接在下方修改方案内容后放行，或填写意见打回。",
                review_nodes=["plan"],
                allow_edit_output=True,
                rewind_targets=["plan"],
            ),
            NodeSpec(
                id="implement",
                label="编码实现",
                kind="agent",
                description="根据确认后的方案及上次测试输出修改代码",
                position=NodePosition(x=580, y=180),
                agent_id="codex",
                sandbox="workspace-write",
                capture_git_diff=True,
                max_runs=5,
                prompt_template=(
                    "请严格按照以下已批准的技术方案完成代码修改（第 {runs} 次执行）。\n\n"
                    "【任务目标】\n{task}\n\n"
                    "【批准方案】\n{plan.output}\n\n"
                    "【上次测试输出】\n{run_test.stdout}\n\n"
                    "【人工反馈】\n{feedback}"
                ),
            ),
            NodeSpec(
                id="run_test",
                label="执行测试",
                kind="shell",
                description="运行测试脚本并将退出码交由下游条件节点判断",
                position=NodePosition(x=840, y=180),
                command="{inputs.test_cmd}",
                continue_on_error=True,
            ),
            NodeSpec(
                id="check_test",
                label="测试是否通过",
                kind="condition",
                description="退出码为 0 进入最终验收；未通过且编码次数 < 3 自动打回编码修复",
                position=NodePosition(x=1100, y=180),
            ),
            NodeSpec(
                id="final_gate",
                label="合并前验收",
                kind="gate",
                description="查看最终代码 Diff 与测试结果，决定完成或打回任意上游节点",
                position=NodePosition(x=1380, y=180),
                stage="final_approval",
                instruction="请检查代码改动 Diff 与测试输出，确认无误后完成流程。",
                review_nodes=["implement", "run_test"],
                allow_edit_output=False,
                rewind_targets=["implement", "plan"],
            ),
        ],
        edges=[
            EdgeSpec(id="e-plan-gate", source="plan", target="plan_gate"),
            EdgeSpec(id="e-gate-impl", source="plan_gate", target="implement", label="通过"),
            EdgeSpec(id="e-impl-test", source="implement", target="run_test"),
            EdgeSpec(id="e-test-cond", source="run_test", target="check_test"),
            EdgeSpec(
                id="e-cond-pass",
                source="check_test",
                target="final_gate",
                label="测试通过",
                condition=EdgeCondition(field="run_test.ok", op="==", value=True),
            ),
            EdgeSpec(
                id="e-cond-retry",
                source="check_test",
                target="implement",
                label="失败重试 (<3次)",
                condition=EdgeCondition(field="implement.runs", op="<", value=3),
            ),
            EdgeSpec(
                id="e-cond-fallback",
                source="check_test",
                target="final_gate",
                label="达重试上限转人工",
                is_default=True,
            ),
            EdgeSpec(id="e-final-end", source="final_gate", target="END", label="完成"),
        ],
    )


def seed_multi_agent_parallel_spec() -> WorkflowSpec:
    """示例模板 2：多 Agent 并行协同流（方案审批 -> 前后端并行开发 -> 汇聚校验 -> 最终验收）。"""
    return WorkflowSpec(
        id="multi-agent-parallel",
        name="多 Agent 前后端并行协同流",
        description="架构与接口契约设计 -> 人工审批 -> 并行触发[后端 Agent || 前端 Agent] -> 汇聚联调校验 -> 最终人工验收",
        builtin=False,
        entry_point="arch_design",
        inputs=[
            WorkflowInputSpec(
                key="task",
                label="协同开发需求",
                kind="textarea",
                required=True,
                placeholder="描述需要前后端或多模块协同实现的功能需求…",
            ),
            WorkflowInputSpec(
                key="repo",
                label="项目根目录",
                kind="path",
                required=False,
                placeholder="留空则使用操作台当前工作区目录",
            ),
        ],
        nodes=[
            NodeSpec(
                id="arch_design",
                label="架构与接口契约设计",
                kind="agent",
                description="制定前后端/多模块职责拆分与接口契约",
                position=NodePosition(x=60, y=200),
                agent_id="auto",
                sandbox="read-only",
                capture_git_diff=False,
                prompt_template=(
                    "请为以下协同需求输出清晰的模块边界与接口契约方案（只读分析，不改代码）：\n\n"
                    "【需求】\n{task}\n\n"
                    "【人工反馈】\n{feedback}"
                ),
            ),
            NodeSpec(
                id="contract_gate",
                label="契约确认卡点",
                kind="gate",
                description="人工确认或微调接口契约，放行后自动并行启动前后端两个 Agent 节点",
                position=NodePosition(x=320, y=200),
                stage="contract_approval",
                instruction="确认接口契约后将同时并行启动下游多个研发 Agent 节点。",
                review_nodes=["arch_design"],
                allow_edit_output=True,
                rewind_targets=["arch_design"],
            ),
            NodeSpec(
                id="backend_dev",
                label="后端模块实现",
                kind="agent",
                description="负责后端逻辑与接口实现（可在右侧切换为 Codex / Claude 等）",
                position=NodePosition(x=620, y=90),
                agent_id="codex",
                sandbox="workspace-write",
                capture_git_diff=True,
                prompt_template=(
                    "请依据以下已确认的架构契约完成【后端模块】代码实现（不要修改 frontend/ 目录）：\n\n"
                    "【需求】\n{task}\n\n"
                    "【接口契约】\n{arch_design.output}\n\n"
                    "【人工反馈】\n{feedback}"
                ),
            ),
            NodeSpec(
                id="frontend_dev",
                label="前端模块实现",
                kind="agent",
                description="负责前端界面实现（可在右侧切换为 Kimi / Claude / Codex，并可指定 frontend 子目录）",
                position=NodePosition(x=620, y=310),
                agent_id="auto",
                cwd="frontend",
                sandbox="workspace-write",
                capture_git_diff=True,
                prompt_template=(
                    "请依据以下已确认的架构契约，在当前前端目录内完成【前端模块】代码实现：\n\n"
                    "【需求】\n{task}\n\n"
                    "【接口契约】\n{arch_design.output}\n\n"
                    "【人工反馈】\n{feedback}"
                ),
            ),
            NodeSpec(
                id="verify_join",
                label="汇聚联调构建",
                kind="shell",
                description="等待前后端两个并行分支全部完成后自动触发联调校验命令",
                position=NodePosition(x=930, y=200),
                join_mode="all",
                command="git status -s",
                continue_on_error=False,
            ),
            NodeSpec(
                id="accept_gate",
                label="最终协同验收",
                kind="gate",
                description="审阅前后端各自产出与变更，可单独选择打回给后端或前端节点重改",
                position=NodePosition(x=1210, y=200),
                stage="final_acceptance",
                instruction="若某一方需要调整，可在『打回目标节点』中单独选择后端或前端节点重跑。",
                review_nodes=["backend_dev", "frontend_dev", "verify_join"],
                allow_edit_output=False,
                rewind_targets=["backend_dev", "frontend_dev", "arch_design"],
            ),
        ],
        edges=[
            EdgeSpec(id="e-arch-gate", source="arch_design", target="contract_gate"),
            EdgeSpec(id="e-gate-back", source="contract_gate", target="backend_dev", label="并行: 后端"),
            EdgeSpec(id="e-gate-front", source="contract_gate", target="frontend_dev", label="并行: 前端"),
            EdgeSpec(id="e-back-join", source="backend_dev", target="verify_join"),
            EdgeSpec(id="e-front-join", source="frontend_dev", target="verify_join"),
            EdgeSpec(id="e-join-accept", source="verify_join", target="accept_gate"),
            EdgeSpec(id="e-accept-end", source="accept_gate", target="END", label="验收完成"),
        ],
    )


def default_coding_workflow_spec() -> WorkflowSpec:
    """保留兼容入口函数名。"""
    return seed_iterative_coding_spec()


# ---------------------------------------------------------------- 模板变量与条件求值引擎
# 仅匹配 {var} 或 {ns.prop}（且点号后不允许以 _ 开头，彻底杜绝 __class__ 等越权属性访问，也不误伤 JSON 花括号）
_TEMPLATE_VAR_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z][a-zA-Z0-9_]*)?)\}")


def resolve_state_path(path: str, state: dict[str, Any], extra: Optional[dict[str, Any]] = None) -> Any:
    """解析变量路径，支持：
    - 'task' / 'feedback' / 'runs' / extra 中的局部变量
    - 'inputs.xxx'
    - '<node_id>.output' / '<node_id>.diff' / '<node_id>.stdout' / '<node_id>.exit_code' / '<node_id>.ok' / '<node_id>.runs'
    - '<node_id>'（快捷等价于 '<node_id>.output'）
    """
    if extra and path in extra:
        return extra[path]

    inputs = state.get("inputs") or {}
    nodes = state.get("nodes") or {}

    if "." in path:
        head, tail = path.split(".", 1)
        if head == "inputs":
            if tail in inputs:
                return inputs[tail]
            return state.get(tail)
        if head in nodes and isinstance(nodes[head], dict):
            node_obj = nodes[head]
            if tail in node_obj:
                return node_obj[tail]
            if tail == "runs":
                return 0
            return ""
        # 未知节点尚未执行时返回安全空默认值（例如首次进入 implement 引用 {run_test.stdout}）
        if tail in ("runs",):
            return 0
        if tail in ("ok",):
            return False
        if tail in ("exit_code",):
            return None
        return ""

    if path in inputs:
        return inputs[path]
    if path in state:
        return state[path]
    if path in nodes and isinstance(nodes[path], dict):
        return nodes[path].get("output", "")
    return None


def _render_template(template: str, state: dict[str, Any], extra: Optional[dict[str, Any]] = None) -> str:
    """安全渲染占位符模板。"""
    if not template:
        return ""

    def _replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        val = resolve_state_path(expr, state, extra=extra)
        if val is None or val == "":
            return "无" if "." not in expr or not expr.startswith("inputs.") else ""
        return str(val)

    return _TEMPLATE_VAR_RE.sub(_replace, template)


def _render_raw_template(template: str, state: dict[str, Any], extra: Optional[dict[str, Any]] = None) -> str:
    """用于路径与 Shell 命令的模板替换：空值替换为空字符串而非中文“无”，避免拼出非法路径或命令。"""
    if not template:
        return ""

    def _replace(match: re.Match[str]) -> str:
        expr = match.group(1)
        val = resolve_state_path(expr, state, extra=extra)
        if val is None:
            return ""
        return str(val)

    return _TEMPLATE_VAR_RE.sub(_replace, template)


def capture_repo_git_diff(cwd: str, limit: int = 30000) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "diff"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        diff_text = (out.stdout or "").strip()
        if diff_text:
            return diff_text[:limit]
        # 若全是未跟踪新文件，附加简短 git status --short 提示
        st = subprocess.run(
            ["git", "-C", cwd, "status", "--short"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        st_text = (st.stdout or "").strip()
        return f"# git status --short\n{st_text}"[:limit] if st_text else ""
    except Exception:  # noqa: BLE001
        return ""


def _coerce_compare_types(left: Any, right: Any) -> tuple[Any, Any]:
    """容忍前端表单输入的字符串与布尔/数字类型比较（例如 'true' == True、'0' == 0）。"""
    if isinstance(left, bool) and isinstance(right, str):
        low = right.strip().lower()
        if low in ("true", "1", "yes"):
            return left, True
        if low in ("false", "0", "no"):
            return left, False
    if isinstance(left, (int, float)) and not isinstance(left, bool) and isinstance(right, str):
        try:
            return left, int(right) if "." not in right else float(right)
        except ValueError:
            pass
    return left, right


def eval_condition(cond: EdgeCondition | RouteRule, state: dict[str, Any]) -> bool:
    left = resolve_state_path(cond.field, state)
    right = resolve_state_path(cond.value_ref, state) if cond.value_ref else cond.value

    if cond.value_ref == "max_attempts" and right is None:
        right = 2
    if cond.field.endswith(".runs") and left is None:
        left = 0

    op = cond.op
    if op == "truthy":
        return bool(left)
    if op == "falsy":
        return not bool(left)

    left, right = _coerce_compare_types(left, right)

    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == "contains":
        return str(right or "") in str(left or "")
    if op == "not_contains":
        return str(right or "") not in str(left or "")
    if op == "regex":
        try:
            return re.search(str(right or ""), str(left or "")) is not None
        except re.error:
            return False

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


# ---------------------------------------------------------------- 拓扑分析（上游祖先推导 + 并行汇聚自动识别）
def _has_nonempty_condition(edge: EdgeSpec) -> bool:
    return edge.condition is not None and bool(edge.condition.field.strip())


def _normalized_outgoing_rules(
    node: NodeSpec,
    out_edges: list[EdgeSpec],
) -> tuple[bool, list[RouteRule], Optional[str], list[str]]:
    """分析节点的出边模式。

    返回:
        (is_conditional, ordered_rules, default_target, unconditional_targets)
    """
    # 1) 兼容 v1 聚合条件边
    for edge in out_edges:
        if edge.conditions:
            return True, list(edge.conditions), edge.default_target or "END", []

    # 2) v2 单边条件或起始节点本身就是 condition 节点（忽略非 condition 节点上未填 field 的空条件残留）
    has_cond_edge = any(_has_nonempty_condition(e) or e.is_default for e in out_edges)
    if node.kind == "condition" or has_cond_edge:
        rules: list[RouteRule] = []
        default_target: Optional[str] = None
        plain_fallbacks: list[str] = []
        for edge in out_edges:
            tgt = edge.target or "END"
            if edge.is_default:
                if default_target is None:
                    default_target = tgt
            elif _has_nonempty_condition(edge) and edge.condition is not None:
                rules.append(
                    RouteRule(
                        field=edge.condition.field.strip(),
                        op=edge.condition.op,
                        value=edge.condition.value,
                        value_ref=edge.condition.value_ref.strip() if edge.condition.value_ref else None,
                        target=tgt,
                        label=edge.label,
                    )
                )
            else:
                plain_fallbacks.append(tgt)
        if default_target is None:
            default_target = plain_fallbacks[0] if plain_fallbacks else "END"
        return True, rules, default_target, []

    # 3) 普通无条件出边（>=2 条即为并行 Fan-out！）
    targets: list[str] = []
    for edge in out_edges:
        tgt = edge.target or "END"
        if tgt not in targets:
            targets.append(tgt)
    return False, [], None, targets


def _build_adjacency(spec: WorkflowSpec) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """构建前向邻接表与反向入边表（不含 END）。"""
    node_ids = {n.id for n in spec.nodes}
    adj: dict[str, set[str]] = {nid: set() for nid in node_ids}
    rev: dict[str, set[str]] = {nid: set() for nid in node_ids}

    for edge in spec.edges:
        if edge.source not in node_ids:
            continue
        targets: list[str] = []
        if edge.conditions:
            targets.extend(r.target for r in edge.conditions)
            if edge.default_target:
                targets.append(edge.default_target)
        elif edge.target:
            targets.append(edge.target)
        for t in targets:
            if t in node_ids:
                adj[edge.source].add(t)
                rev[t].add(edge.source)
    return adj, rev


def compute_upstream_ancestors(spec: WorkflowSpec, target_node_id: str) -> list[NodeSpec]:
    """按拓扑顺序返回 target_node_id 的所有上游可执行节点（agent / shell），供 Gate 默认审阅与打回选择。"""
    _, rev = _build_adjacency(spec)
    visited: set[str] = set()
    queue: deque[str] = deque(rev.get(target_node_id, set()))
    while queue:
        cur = queue.popleft()
        if cur in visited or cur == target_node_id:
            continue
        visited.add(cur)
        for parent in rev.get(cur, set()):
            if parent not in visited:
                queue.append(parent)

    # 保持在 spec.nodes 中的声明顺序，仅筛出实际干活的 agent / shell 节点
    return [n for n in spec.nodes if n.id in visited and n.kind in ("agent", "shell")]


def _detect_back_edges(spec: WorkflowSpec, adj: dict[str, set[str]]) -> set[tuple[str, str]]:
    """从 entry_point 做 DFS 识别环路回边 (u -> v)，避免并行汇聚把循环回边误判为屏障等待。"""
    back_edges: set[tuple[str, str]] = set()
    state: dict[str, int] = {}  # 0/absent: unvisited, 1: visiting, 2: visited

    def _dfs(u: str) -> None:
        state[u] = 1
        for v in adj.get(u, set()):
            st = state.get(v, 0)
            if st == 1:
                back_edges.add((u, v))
            elif st == 0:
                _dfs(v)
        state[u] = 2

    if spec.entry_point in adj:
        _dfs(spec.entry_point)
    for nid in adj:
        if state.get(nid, 0) == 0:
            _dfs(nid)
    return back_edges


def _detect_parallel_join_sources(
    spec: WorkflowSpec,
    unconditional_in_edges: dict[str, list[str]],
    parallel_split_nodes: dict[str, list[str]],
) -> dict[str, list[str]]:
    """当节点 join_mode='auto' 且有多条无条件非回边入边时，若这些入边源自同一个并行分叉节点的多个分支，则自动聚合为屏障等待。"""
    adj, _ = _build_adjacency(spec)
    back_edges = _detect_back_edges(spec, adj)
    dag_adj: dict[str, set[str]] = {
        u: {v for v in vs if (u, v) not in back_edges}
        for u, vs in adj.items()
    }

    def _reachable(start: str) -> set[str]:
        seen = {start}
        q = deque([start])
        while q:
            cur = q.popleft()
            for nxt in dag_adj.get(cur, set()):
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
        return seen

    # 预计算每个并行分叉节点的各个直接子分支可达集合
    branch_reach: dict[str, list[set[str]]] = {}
    for split_id, children in parallel_split_nodes.items():
        branch_reach[split_id] = [_reachable(ch) for ch in children]

    node_map = {n.id: n for n in spec.nodes}
    barrier_joins: dict[str, list[str]] = {}

    for target_id, sources in unconditional_in_edges.items():
        forward_sources = [s for s in sources if (s, target_id) not in back_edges]
        if len(forward_sources) < 2:
            continue
        mode = node_map[target_id].join_mode
        if mode == "any":
            continue
        if mode == "all":
            barrier_joins[target_id] = forward_sources
            continue
        # mode == "auto": 检查 forward_sources 是否分别落在某个并行分叉节点的不同分支上
        is_parallel_join = False
        for _, reaches in branch_reach.items():
            matched_branches = {
                idx
                for s in forward_sources
                for idx, rset in enumerate(reaches)
                if s in rset
            }
            if len(matched_branches) >= 2:
                is_parallel_join = True
                break
        if is_parallel_join:
            barrier_joins[target_id] = forward_sources

    return barrier_joins


# ---------------------------------------------------------------- 默认离线/测试兜底 Hooks
class _DryRunHooks:
    """仅供 validate / 离线单元测试使用的轻量默认执行钩子（生产操作台始终注入真实 RuntimeHooks）。"""

    def __init__(self, default_repo: str = ".") -> None:
        self.default_repo = os.path.abspath(default_repo)

    def execute_agent(
        self,
        *,
        thread_id: str,
        node: NodeSpec,
        prompt: str,
        cwd: str,
    ) -> tuple[str, CliExecResult]:
        return (
            node.agent_id,
            CliExecResult(
                ok=True,
                exit_code=0,
                output=f"[{node.id}] dry-run output for: {prompt[:160]}",
                stdout=f"[{node.id}] dry-run output",
                duration_ms=5,
            ),
        )

    def execute_shell(
        self,
        *,
        thread_id: str,
        node: NodeSpec,
        command: str,
        cwd: str,
    ) -> CliExecResult:
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=min(node.timeout_sec, 300),
                check=False,
            )
            combined = ((proc.stdout or "") + (proc.stderr or "")).strip()[-10000:]
            return CliExecResult(
                ok=proc.returncode == 0,
                exit_code=proc.returncode,
                output=combined,
                stdout=combined,
                command_display=command,
            )
        except Exception as exc:  # noqa: BLE001
            return CliExecResult(ok=False, exit_code=-1, output=str(exc), stdout=str(exc), command_display=command)

    def emit_log(self, thread_id: str, node_id: str, stream: str, line: str) -> None:
        pass

    def on_node_start(self, thread_id: str, node: NodeSpec, cwd: str, run_count: int) -> None:
        pass

    def on_node_end(self, thread_id: str, node: NodeSpec, result: NodeResultData) -> None:
        pass


# ---------------------------------------------------------------- 节点函数构建与图编译
def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _resolve_node_cwd(node: NodeSpec, state: dict[str, Any], default_repo: str) -> str:
    overrides = (state.get("node_overrides") or {}).get(node.id) or {}
    raw_cwd = overrides.get("cwd") or node.cwd
    inputs = state.get("inputs") or {}
    base_repo = str(inputs.get("repo") or state.get("repo") or default_repo or ".").strip() or "."
    base_abs = os.path.abspath(os.path.expanduser(base_repo))

    if not raw_cwd:
        return base_abs

    rendered = _render_raw_template(str(raw_cwd).strip(), state).strip()
    if not rendered:
        return base_abs
    expanded = os.path.expanduser(rendered)
    if os.path.isabs(expanded):
        return os.path.abspath(expanded)
    return os.path.abspath(os.path.join(base_abs, expanded))


_RESERVED_NODE_IDS = ("START", "END", "__start__", "__end__", "inputs", "nodes", "task", "repo", "feedback")
_VALID_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def validate_workflow_spec(spec: WorkflowSpec) -> None:
    """严格校验 WorkflowSpec 的节点 ID、入口与所有连线引用合法性。"""
    node_map = {n.id: n for n in spec.nodes}
    node_ids = set(node_map.keys())
    if len(node_ids) != len(spec.nodes):
        raise ValueError("工作流包含重复的节点 ID")
    for reserved in ("START", "END", "__start__", "__end__", "inputs", "nodes"):
        if reserved in node_ids:
            raise ValueError(f"节点 ID 不能使用保留字: {reserved}")
    if spec.entry_point not in node_ids:
        raise ValueError(f"入口节点 entry_point '{spec.entry_point}' 不在节点列表中: {sorted(node_ids)}")

    valid_targets = node_ids | {"END"}
    cond_unconfigured_by_src: dict[str, list[str]] = {}
    for edge in spec.edges:
        if edge.source not in node_ids:
            raise ValueError(f"连线起始节点 '{edge.source}' 不存在")
        if edge.conditions:
            if not edge.default_target or edge.default_target not in valid_targets:
                raise ValueError(f"条件连线 '{edge.source}' 的 default_target '{edge.default_target}' 无效")
            for rule in edge.conditions:
                if rule.target not in valid_targets:
                    raise ValueError(f"条件连线 '{edge.source}' 的目标节点 '{rule.target}' 无效")
        else:
            if not edge.target or edge.target not in valid_targets:
                raise ValueError(f"连线 '{edge.source} -> {edge.target}' 的目标节点无效")
            src_node = node_map[edge.source]
            if src_node.kind == "condition" and not edge.is_default and not _has_nonempty_condition(edge):
                cond_unconfigured_by_src.setdefault(src_node.id, []).append(edge.target)

    # 条件判断节点允许最多 1 条未填条件的出边自动充当隐式兜底（else），若 >=2 条均未配置条件也未勾选兜底则明确提示
    for src_id, unconf_targets in cond_unconfigured_by_src.items():
        has_explicit_default = any(
            e.source == src_id and e.is_default for e in spec.edges
        )
        if has_explicit_default or len(unconf_targets) >= 2:
            src_label = node_map[src_id].label
            raise ValueError(
                f"条件节点「{src_label}」连向「{unconf_targets[0]}」的分支尚未选择判断字段，请双击该连线配置条件或勾选为兜底分支"
            )


def generate_unique_workflow_id(
    name: str,
    existing_ids: set[str],
    preferred_id: Optional[str] = None,
) -> str:
    """根据工作流名称（支持纯中文）自动生成唯一合法的 workflow_id，或校验用户指定的 preferred_id。"""
    if preferred_id is not None and preferred_id.strip():
        candidate = preferred_id.strip()
        if not _VALID_ID_RE.match(candidate):
            raise ValueError("工作流 ID 仅允许包含字母、数字、连字符 (-) 或下划线 (_)")
        if candidate in existing_ids:
            raise FileExistsError(f"工作流 ID '{candidate}' 已存在，请更换其它 ID 或留空自动生成")
        return candidate

    slug = re.sub(r"[^a-zA-Z0-9]+", "-", (name or "").strip()).strip("-").lower()
    if slug:
        base = slug[:36].strip("-") or "workflow"
        if not base[0].isalpha():
            base = f"wf-{base}"
        if base not in existing_ids:
            return base
        idx = 2
        while f"{base}-{idx}" in existing_ids:
            idx += 1
        return f"{base}-{idx}"

    idx = 1
    while f"workflow-{idx}" in existing_ids:
        idx += 1
    return f"workflow-{idx}"


def build_blank_workflow_spec(
    workflow_id: str,
    name: str,
    description: str = "",
) -> WorkflowSpec:
    """创建一张干净、立即可保存可运行的空白自定义工作流（含 1 个起始 Agent 节点连向 END）。"""
    clean_name = (name or "").strip() or "自定义工作流"
    clean_desc = (description or "").strip() or "自定义多 Agent 研发协同工作流"
    return WorkflowSpec(
        id=workflow_id,
        name=clean_name,
        description=clean_desc,
        builtin=False,
        inputs=default_workflow_inputs(),
        entry_point="step_1",
        nodes=[
            NodeSpec(
                id="step_1",
                label="任务执行",
                kind="agent",
                description="默认起始节点，双击可在弹窗中放大编辑 Prompt、切换 CLI Agent 或继续增删下游节点",
                agent_id="auto",
                sandbox="workspace-write",
                capture_git_diff=True,
                prompt_template="请围绕以下任务完成分析与实现：\n\n{task}",
                position=NodePosition(x=120, y=160),
            )
        ],
        edges=[
            EdgeSpec(id="e-step_1-END", source="step_1", target="END", label="完成"),
        ],
    )


def clone_workflow_spec(
    source: WorkflowSpec,
    workflow_id: str,
    name: str,
    description: Optional[str] = None,
) -> WorkflowSpec:
    """基于现有工作流模板克隆一份全新的独立自定义工作流。"""
    cloned = WorkflowSpec.model_validate(source.model_dump(mode="json"))
    clean_name = (name or "").strip() or f"{source.name} 副本"
    clean_desc = (
        description.strip()
        if description is not None and description.strip()
        else source.description
    )
    return cloned.model_copy(
        update={
            "id": workflow_id,
            "name": clean_name,
            "description": clean_desc,
            "builtin": False,
            "updated_at": None,
        }
    )


def rename_workflow_node(
    spec: WorkflowSpec,
    old_node_id: str,
    new_node_id: str,
) -> WorkflowSpec:
    """重命名节点 ID 并全自动联动更新入口、连线、审批审阅/打回列表、条件表达式与所有 {node_id.xxx} 模板引用。"""
    old_id = (old_node_id or "").strip()
    new_id = (new_node_id or "").strip()
    if not old_id or not new_id:
        raise ValueError("原节点 ID 与新节点 ID 均不能为空")
    if old_id == new_id:
        return spec

    existing_ids = {n.id for n in spec.nodes}
    if old_id not in existing_ids:
        raise KeyError(f"未找到要重命名的节点: {old_id}")
    if not _VALID_ID_RE.match(new_id):
        raise ValueError("新节点 ID 仅允许包含字母、数字、连字符 (-) 或下划线 (_)")
    if new_id in _RESERVED_NODE_IDS:
        raise ValueError(f"新节点 ID 不能使用保留字: {new_id}")
    if new_id in existing_ids:
        raise ValueError(f"节点 ID '{new_id}' 已存在，请换一个唯一 ID")

    tpl_re = re.compile(r"\{\s*" + re.escape(old_id) + r"(\.[a-zA-Z0-9_]+)?\s*\}")

    def _rewrite_tpl(val: Optional[str]) -> Optional[str]:
        if not val:
            return val
        return tpl_re.sub(lambda m: "{" + new_id + (m.group(1) or "") + "}", val)

    def _rewrite_path(path: Optional[str]) -> Optional[str]:
        if not path:
            return path
        stripped = path.strip()
        if stripped == old_id:
            return new_id
        if stripped.startswith(f"{old_id}."):
            return f"{new_id}.{stripped[len(old_id) + 1:]}"
        return path

    updated_nodes: list[NodeSpec] = []
    for n in spec.nodes:
        updated_nodes.append(
            n.model_copy(
                update={
                    "id": new_id if n.id == old_id else n.id,
                    "prompt_template": _rewrite_tpl(n.prompt_template),
                    "command": _rewrite_tpl(n.command),
                    "instruction": _rewrite_tpl(n.instruction),
                    "cwd": _rewrite_tpl(n.cwd),
                    "result_template": _rewrite_tpl(n.result_template),
                    "abort_result_template": _rewrite_tpl(n.abort_result_template),
                    "review_nodes": [new_id if x == old_id else x for x in n.review_nodes],
                    "rewind_targets": [new_id if x == old_id else x for x in n.rewind_targets],
                }
            )
        )

    updated_edges: list[EdgeSpec] = []
    for idx, e in enumerate(spec.edges):
        new_src = new_id if e.source == old_id else e.source
        new_tgt = new_id if e.target == old_id else e.target
        new_def = new_id if e.default_target == old_id else e.default_target
        new_cond = None
        if e.condition is not None:
            new_cond = e.condition.model_copy(
                update={
                    "field": _rewrite_path(e.condition.field) or "",
                    "value_ref": _rewrite_path(e.condition.value_ref),
                }
            )
        new_rules = [
            r.model_copy(
                update={
                    "field": _rewrite_path(r.field) or "",
                    "value_ref": _rewrite_path(r.value_ref),
                    "target": new_id if r.target == old_id else r.target,
                }
            )
            for r in e.conditions
        ]
        # 若原边 ID 包含自动生成的旧节点名前缀，一并平滑更新，防止产生令人困惑的陈旧 edge id
        new_edge_id = e.id
        if new_edge_id and old_id in new_edge_id:
            new_edge_id = f"e-{new_src}-{new_tgt or new_def or 'cond'}-{idx}"
        updated_edges.append(
            e.model_copy(
                update={
                    "id": new_edge_id,
                    "source": new_src,
                    "target": new_tgt,
                    "default_target": new_def,
                    "condition": new_cond,
                    "conditions": new_rules,
                }
            )
        )

    renamed = spec.model_copy(
        update={
            "entry_point": new_id if spec.entry_point == old_id else spec.entry_point,
            "nodes": updated_nodes,
            "edges": updated_edges,
        }
    )
    validate_workflow_spec(renamed)
    return renamed


def build_state_graph(
    spec: WorkflowSpec,
    hooks: Optional[NodeRuntimeHooks] = None,
) -> StateGraph:
    """将 WorkflowSpec v2 动态编译为支持并发/并行、条件循环、在线编辑产出与任意打回的 LangGraph StateGraph。"""
    validate_workflow_spec(spec)
    rt: NodeRuntimeHooks = hooks if hooks is not None else _DryRunHooks()

    node_map: dict[str, NodeSpec] = {n.id: n for n in spec.nodes}
    out_edges_map: dict[str, list[EdgeSpec]] = {n.id: [] for n in spec.nodes}
    for edge in spec.edges:
        out_edges_map[edge.source].append(edge)

    # 预解析每个节点的出边模式
    routing_meta: dict[str, tuple[bool, list[RouteRule], Optional[str], list[str]]] = {}
    parallel_splits: dict[str, list[str]] = {}
    unconditional_in: dict[str, list[str]] = {n.id: [] for n in spec.nodes}

    for node in spec.nodes:
        is_cond, rules, def_tgt, plain_tgts = _normalized_outgoing_rules(node, out_edges_map[node.id])
        routing_meta[node.id] = (is_cond, rules, def_tgt, plain_tgts)
        # Gate 节点通过 Command(goto=...) 动态路由（天然支持放行到并行下游、打回到任意上游、或中止到 END）
        if node.kind == "gate":
            real_children = [t for t in plain_tgts if t != "END"]
            if len(real_children) >= 2:
                parallel_splits[node.id] = real_children
            continue
        if not is_cond:
            real_children = [t for t in plain_tgts if t != "END"]
            if len(real_children) >= 2:
                parallel_splits[node.id] = real_children
            for t in real_children:
                if node.id not in unconditional_in[t]:
                    unconditional_in[t].append(node.id)

    barrier_joins = _detect_parallel_join_sources(spec, unconditional_in, parallel_splits)

    # 判断某节点的直接下游是否包含 condition 节点（用于 shell 节点自动把非零退出码交给条件判断）
    feeds_into_condition: set[str] = set()
    for node in spec.nodes:
        is_cond, _, _, plain_tgts = routing_meta[node.id]
        if is_cond:
            feeds_into_condition.add(node.id)
        for t in plain_tgts:
            if t in node_map and node_map[t].kind == "condition":
                feeds_into_condition.add(node.id)

    builder = StateGraph(WorkflowGraphState)

    for node in spec.nodes:
        builder.add_node(
            node.id,
            _make_v2_node_fn(spec, node, rt, routing_meta, feeds_into_condition, barrier_joins),
        )

    builder.add_edge(START, spec.entry_point)

    # 注册非 Gate 节点的出边（包含并行屏障汇聚边、普通边、条件路由边）
    barrier_consumed_pairs: set[tuple[str, str]] = set()
    for join_target, join_sources in barrier_joins.items():
        builder.add_edge(join_sources, join_target)
        for s in join_sources:
            barrier_consumed_pairs.add((s, join_target))

    for node in spec.nodes:
        if node.kind == "gate":
            # Gate 内部使用 Command(goto=...) 动态分发，无需静态绑死出边，从而允许打回到任意上游节点！
            continue
        is_cond, rules, def_tgt, plain_tgts = routing_meta[node.id]
        if is_cond:
            fallback = def_tgt or "END"
            path_map: dict[str, Any] = {}
            for r in rules:
                path_map[r.target] = END if r.target == "END" else r.target
            path_map[fallback] = END if fallback == "END" else fallback

            def _make_router(edge_rules: list[RouteRule], fb: str) -> Callable[[dict[str, Any]], str]:
                def _router(state: dict[str, Any]) -> str:
                    for rule in edge_rules:
                        if eval_condition(rule, state):
                            return rule.target
                    return fb

                return _router

            builder.add_conditional_edges(node.id, _make_router(list(rules), fallback), path_map)
        else:
            if not plain_tgts:
                builder.add_edge(node.id, END)
            else:
                for tgt in plain_tgts:
                    if (node.id, tgt) in barrier_consumed_pairs:
                        continue
                    builder.add_edge(node.id, END if tgt == "END" else tgt)

    return builder


def _extract_thread_id(config: Optional[RunnableConfig]) -> str:
    if not config:
        return "local-thread"
    configurable = config.get("configurable") or {}
    return str(configurable.get("thread_id") or "local-thread")


def _reachable_forward_nodes(spec: WorkflowSpec, start_id: str) -> set[str]:
    adj, _ = _build_adjacency(spec)
    back_edges = _detect_back_edges(spec, adj)
    seen = {start_id}
    q: deque[str] = deque([start_id])
    while q:
        u = q.popleft()
        for v in adj.get(u, set()):
            if (u, v) not in back_edges and v not in seen:
                seen.add(v)
                q.append(v)
    return seen


def _make_v2_node_fn(
    spec: WorkflowSpec,
    node: NodeSpec,
    rt: NodeRuntimeHooks,
    routing_meta: dict[str, tuple[bool, list[RouteRule], Optional[str], list[str]]],
    feeds_into_condition: set[str],
    barrier_joins: dict[str, list[str]],
) -> Callable[..., Any]:
    """根据四类标准节点构建 LangGraph 执行函数。"""

    node_map = {n.id: n for n in spec.nodes}

    if node.kind == "agent":

        def _agent_fn(state: WorkflowGraphState, config: RunnableConfig) -> dict[str, Any]:
            if (state.get("noop_passthrough") or {}).get(node.id):
                return {"noop_passthrough": {node.id: False}}
            tid = _extract_thread_id(config)
            prev_res = (state.get("nodes") or {}).get(node.id) or {}
            total_runs = int(prev_res.get("runs") or 0) + 1
            auto_count = int((state.get("auto_runs") or {}).get(node.id) or 0) + 1

            if auto_count > node.max_runs:
                raise NodeExecutionError(
                    node.id,
                    f"节点 '{node.label}' ({node.id}) 连续自动循环已达上限 ({node.max_runs} 次)，已暂停以防死循环。",
                )

            cwd = _resolve_node_cwd(node, state, rt.default_repo)
            overrides = (state.get("node_overrides") or {}).get(node.id) or {}
            effective_node = node.model_copy(
                update={
                    k: v
                    for k, v in overrides.items()
                    if k in ("agent_id", "prompt_template", "sandbox", "cwd") and v is not None
                }
            )

            targeted_fb = (state.get("feedback_by_node") or {}).get(node.id) or state.get("feedback") or ""
            extra = {
                "id": node.id,
                "runs": total_runs,
                "attempt": total_runs,
                "feedback": targeted_fb or "无",
            }
            tpl = effective_node.prompt_template or "请完成以下任务：\n{task}\n\n人工反馈：{feedback}"
            prompt = _render_template(tpl, state, extra=extra)

            started_at = _now_iso()
            rt.on_node_start(tid, effective_node, cwd, total_runs)
            rt.emit_log(
                tid,
                node.id,
                "system",
                f"▶ 开始执行 Agent 节点 [{node.label}] (第 {total_runs} 次, agent={effective_node.agent_id}, sandbox={effective_node.sandbox})",
            )

            used_agent_id, exec_res = rt.execute_agent(
                thread_id=tid,
                node=effective_node,
                prompt=prompt,
                cwd=cwd,
            )

            diff_text = ""
            if effective_node.capture_git_diff:
                diff_text = capture_repo_git_diff(cwd)

            finished_at = _now_iso()
            node_data: NodeResultData = {
                "node_id": node.id,
                "label": node.label,
                "kind": "agent",
                "status": "cancelled" if exec_res.cancelled else ("succeeded" if exec_res.ok else "failed"),
                "ok": exec_res.ok,
                "exit_code": exec_res.exit_code,
                "output": exec_res.output,
                "stdout": exec_res.stdout,
                "diff": diff_text,
                "runs": total_runs,
                "worker_id": effective_node.worker_id,
                "agent_id": used_agent_id,
                "cwd": cwd,
                "duration_ms": exec_res.duration_ms,
                "started_at": started_at,
                "finished_at": finished_at,
                "edited_by_human": False,
            }
            rt.on_node_end(tid, effective_node, node_data)

            if exec_res.cancelled:
                raise NodeExecutionError(node.id, f"节点 [{node.label}] 已被人工强杀中止", result=node_data)
            if not exec_res.ok and not effective_node.continue_on_error and node.id not in feeds_into_condition:
                raise NodeExecutionError(
                    node.id,
                    f"Agent 节点 [{node.label}] 执行失败 (exit={exec_res.exit_code}): {exec_res.output[:400]}",
                    result=node_data,
                )

            update: dict[str, Any] = {
                "nodes": {node.id: node_data},
                "auto_runs": {node.id: auto_count},
                "result": exec_res.output,
                "events": [f"{node.id}#{total_runs}:{'ok' if exec_res.ok else 'fail'}"],
            }
            if effective_node.output_key:
                update[effective_node.output_key] = diff_text if (effective_node.output_key == "patch" and diff_text) else exec_res.output
            return update

        _agent_fn.__name__ = f"node_{node.id}"
        return _agent_fn

    if node.kind == "shell":

        def _shell_fn(state: WorkflowGraphState, config: RunnableConfig) -> dict[str, Any]:
            if (state.get("noop_passthrough") or {}).get(node.id):
                return {"noop_passthrough": {node.id: False}}
            tid = _extract_thread_id(config)
            prev_res = (state.get("nodes") or {}).get(node.id) or {}
            total_runs = int(prev_res.get("runs") or 0) + 1
            auto_count = int((state.get("auto_runs") or {}).get(node.id) or 0) + 1
            if auto_count > node.max_runs:
                raise NodeExecutionError(
                    node.id,
                    f"Shell 节点 '{node.label}' ({node.id}) 连续自动执行已达上限 ({node.max_runs} 次)。",
                )

            cwd = _resolve_node_cwd(node, state, rt.default_repo)
            overrides = (state.get("node_overrides") or {}).get(node.id) or {}
            raw_cmd = overrides.get("command") or node.command
            if not raw_cmd and node.cmd_key:
                raw_cmd = str(resolve_state_path(f"inputs.{node.cmd_key}", state) or state.get(node.cmd_key) or node.default_cmd or "")
            cmd_str = _render_raw_template(str(raw_cmd or "").strip(), state, extra={"id": node.id, "runs": total_runs}).strip()
            if not cmd_str:
                cmd_str = "echo '[skip] 未配置 Shell 命令'"

            started_at = _now_iso()
            rt.on_node_start(tid, node, cwd, total_runs)
            rt.emit_log(tid, node.id, "system", f"▶ 开始执行 Shell 节点 [{node.label}] (第 {total_runs} 次): {cmd_str}")

            exec_res = rt.execute_shell(
                thread_id=tid,
                node=node,
                command=cmd_str,
                cwd=cwd,
            )
            diff_text = capture_repo_git_diff(cwd) if node.capture_git_diff else ""
            finished_at = _now_iso()

            node_data: NodeResultData = {
                "node_id": node.id,
                "label": node.label,
                "kind": "shell",
                "status": "cancelled" if exec_res.cancelled else ("succeeded" if exec_res.ok else "failed"),
                "ok": exec_res.ok,
                "exit_code": exec_res.exit_code,
                "output": exec_res.output,
                "stdout": exec_res.stdout,
                "diff": diff_text,
                "runs": total_runs,
                "worker_id": node.worker_id,
                "agent_id": "shell",
                "cwd": cwd,
                "duration_ms": exec_res.duration_ms,
                "started_at": started_at,
                "finished_at": finished_at,
                "edited_by_human": False,
            }
            rt.on_node_end(tid, node, node_data)

            if exec_res.cancelled:
                raise NodeExecutionError(node.id, f"Shell 节点 [{node.label}] 已被人工强杀中止", result=node_data)

            allow_non_zero = node.continue_on_error or (node.id in feeds_into_condition)
            if not exec_res.ok and not allow_non_zero:
                raise NodeExecutionError(
                    node.id,
                    f"Shell 节点 [{node.label}] 退出码非零 ({exec_res.exit_code}): {exec_res.output[:400]}",
                    result=node_data,
                )

            update: dict[str, Any] = {
                "nodes": {node.id: node_data},
                "auto_runs": {node.id: auto_count},
                "events": [f"{node.id}#{total_runs}:{'pass' if exec_res.ok else 'fail'}"],
            }
            if node.output_key:
                update[node.output_key] = exec_res.output
            return update

        _shell_fn.__name__ = f"node_{node.id}"
        return _shell_fn

    if node.kind == "gate":
        ancestors = compute_upstream_ancestors(spec, node.id)
        default_review_ids = node.review_nodes or [a.id for a in ancestors[-3:]]
        allowed_rewind_ids = node.rewind_targets or [a.id for a in ancestors]

        def _gate_fn(state: WorkflowGraphState, config: RunnableConfig) -> Command:
            tid = _extract_thread_id(config)
            nodes_state = state.get("nodes") or {}

            upstream_outputs: dict[str, dict[str, Any]] = {}
            for uid in default_review_ids:
                u_spec = node_map.get(uid)
                u_res = nodes_state.get(uid) or {}
                upstream_outputs[uid] = {
                    "node_id": uid,
                    "label": u_spec.label if u_spec else uid,
                    "kind": u_spec.kind if u_spec else "agent",
                    "ok": u_res.get("ok", True),
                    "exit_code": u_res.get("exit_code", 0),
                    "output": u_res.get("output", ""),
                    "diff": u_res.get("diff", ""),
                    "stdout": u_res.get("stdout", ""),
                    "edited_by_human": u_res.get("edited_by_human", False),
                }

            rewind_options = [
                {"id": uid, "label": node_map[uid].label if uid in node_map else uid}
                for uid in allowed_rewind_ids
                if uid in node_map
            ]
            if not rewind_options and spec.entry_point != node.id and spec.entry_point in node_map:
                ep = node_map[spec.entry_point]
                rewind_options = [{"id": ep.id, "label": ep.label}]

            payload: dict[str, Any] = {
                "node_id": node.id,
                "label": node.label,
                "stage": node.stage or node.id,
                "instruction": _render_template(node.instruction or node.description or "请确认当前阶段产出并决定下一步流向。", state),
                "allow_edit_output": node.allow_edit_output,
                "upstream_outputs": upstream_outputs,
                "rewind_options": rewind_options,
                "options": list(node.options),
            }
            for k in node.expose_keys:
                payload[k] = resolve_state_path(k, state)

            rt.emit_log(tid, node.id, "system", f"⏸ 到达人工卡点 [{node.label}]，等待审批决策…")
            decision = interrupt(payload)

            if isinstance(decision, dict):
                action = str(decision.get("action") or "approve")
                feedback = str(decision.get("feedback") or "")
                edited_outputs = decision.get("edited_outputs") or {}
                target_node_id = decision.get("target_node_id")
            else:
                action, feedback, edited_outputs, target_node_id = str(decision), "", {}, None

            nodes_patch: dict[str, NodeResultData] = {}
            if isinstance(edited_outputs, dict) and edited_outputs:
                for edit_nid, new_text in edited_outputs.items():
                    if edit_nid in node_map and isinstance(new_text, str):
                        existing = dict(nodes_state.get(edit_nid) or {})
                        existing.update(
                            {
                                "node_id": edit_nid,
                                "label": node_map[edit_nid].label,
                                "output": new_text,
                                "edited_by_human": True,
                            }
                        )
                        nodes_patch[edit_nid] = existing  # type: ignore[assignment]
                        rt.emit_log(tid, node.id, "system", f"✎ 人工在线修订了节点 [{node_map[edit_nid].label}] 的产出文本")

            now_str = _now_iso()
            nodes_patch[node.id] = {
                "node_id": node.id,
                "label": node.label,
                "kind": "gate",
                "status": action,
                "ok": action == "approve",
                "exit_code": 0,
                "output": feedback or f"人工决策: {action}",
                "stdout": "",
                "diff": "",
                "runs": int((nodes_state.get(node.id) or {}).get("runs") or 0) + 1,
                "started_at": now_str,
                "finished_at": now_str,
            }

            is_cond, rules, def_tgt, plain_tgts = routing_meta[node.id]

            if action == "abort":
                rt.emit_log(tid, node.id, "system", f"⏹ 人工在卡点 [{node.label}] 中止了本次运行")
                return Command(
                    update={
                        "action": "abort",
                        "feedback": feedback,
                        "result": "已中止（人工在卡点中止运行）",
                        "nodes": nodes_patch,
                        "events": [f"{node.id}:abort"],
                    },
                    goto=END,
                )

            if action == "revise":
                chosen_target = str(target_node_id or "").strip()
                if not chosen_target or chosen_target not in node_map:
                    if is_cond and def_tgt and def_tgt in node_map:
                        chosen_target = def_tgt
                    elif rewind_options:
                        chosen_target = rewind_options[0]["id"]
                    else:
                        chosen_target = spec.entry_point
                rt.emit_log(
                    tid,
                    node.id,
                    "system",
                    f"↩ 人工打回至节点 [{node_map[chosen_target].label}]，反馈：{feedback or '无'}",
                )
                # 若只打回到并行分支中的某一条，对同一屏障汇聚点下已完成且不在本次重跑路径上的兄弟末端节点标记 0 开销直通，使下游屏障汇聚能正常再次触发
                reachable_from_target = _reachable_forward_nodes(spec, chosen_target)
                passthrough_patch: dict[str, bool] = {}
                goto_list: list[str] = [chosen_target]
                for join_id, join_sources in barrier_joins.items():
                    if join_id in reachable_from_target:
                        active_siblings = [s for s in join_sources if s in reachable_from_target]
                        idle_siblings = [
                            s for s in join_sources
                            if s not in reachable_from_target and s in nodes_state
                        ]
                        if active_siblings and idle_siblings:
                            for s in idle_siblings:
                                passthrough_patch[s] = True
                                if s not in goto_list:
                                    goto_list.append(s)

                return Command(
                    update={
                        "action": "revise",
                        "feedback": feedback,
                        "feedback_by_node": {chosen_target: feedback},
                        # 人工显式打回时重置自动循环计数，避免人工多轮正常打磨触发死循环上限
                        "auto_runs": {"__reset__": True},
                        "noop_passthrough": passthrough_patch,
                        "nodes": nodes_patch,
                        "events": [f"{node.id}:revise->{chosen_target}"],
                    },
                    goto=goto_list if len(goto_list) > 1 else chosen_target,
                )

            # action == "approve"
            rt.emit_log(tid, node.id, "system", f"✓ 人工卡点 [{node.label}] 审批通过")
            state_with_action = {**state, "action": "approve", "feedback": feedback}
            goto_targets: Any
            if is_cond:
                matched_tgt: Optional[str] = None
                for r in rules:
                    if eval_condition(r, state_with_action):
                        matched_tgt = r.target
                        break
                final_t = matched_tgt or def_tgt or "END"
                goto_targets = END if final_t == "END" else final_t
            else:
                if not plain_tgts or plain_tgts == ["END"]:
                    goto_targets = END
                else:
                    real_next = [t for t in plain_tgts if t != "END"]
                    goto_targets = real_next if len(real_next) > 1 else (real_next[0] if real_next else END)

            return Command(
                update={
                    "action": "approve",
                    "feedback": feedback,
                    "nodes": nodes_patch,
                    "events": [f"{node.id}:approve"],
                },
                goto=goto_targets,
            )

        _gate_fn.__name__ = f"node_{node.id}"
        return _gate_fn

    if node.kind == "condition":

        def _condition_fn(state: WorkflowGraphState, config: RunnableConfig) -> dict[str, Any]:
            tid = _extract_thread_id(config)
            prev_res = (state.get("nodes") or {}).get(node.id) or {}
            total_runs = int(prev_res.get("runs") or 0) + 1
            _, rules, def_tgt, _ = routing_meta[node.id]
            chosen = def_tgt or "END"
            matched_desc = f"默认分支 -> {chosen}"
            for r in rules:
                if eval_condition(r, state):
                    chosen = r.target
                    matched_desc = f"命中 ({r.field} {r.op} {r.value if r.value_ref is None else r.value_ref}) -> {chosen}"
                    break

            rt.emit_log(tid, node.id, "system", f"⑂ 条件判断 [{node.label}]: {matched_desc}")
            now_str = _now_iso()
            node_data: NodeResultData = {
                "node_id": node.id,
                "label": node.label,
                "kind": "condition",
                "status": "succeeded",
                "ok": True,
                "exit_code": 0,
                "output": matched_desc,
                "stdout": matched_desc,
                "diff": "",
                "runs": total_runs,
                "started_at": now_str,
                "finished_at": now_str,
            }
            return {
                "nodes": {node.id: node_data},
                "events": [f"{node.id}->{chosen}"],
            }

        _condition_fn.__name__ = f"node_{node.id}"
        return _condition_fn

    if node.kind == "summary":

        def _summary_fn(state: WorkflowGraphState, config: RunnableConfig) -> dict[str, Any]:
            if node.abort_result_template and state.get("action") == "abort":
                res_text = _render_template(node.abort_result_template, state, extra={"id": node.id})
            else:
                res_text = _render_template(node.result_template or "流程已完成：{task}", state, extra={"id": node.id})
            now_str = _now_iso()
            node_data: NodeResultData = {
                "node_id": node.id,
                "label": node.label,
                "kind": "summary",
                "status": "succeeded",
                "ok": True,
                "exit_code": 0,
                "output": res_text,
                "stdout": res_text,
                "diff": "",
                "runs": 1,
                "started_at": now_str,
                "finished_at": now_str,
            }
            return {
                "result": res_text,
                "nodes": {node.id: node_data},
                "events": [node.id],
            }

        _summary_fn.__name__ = f"node_{node.id}"
        return _summary_fn

    raise ValueError(f"未知的节点类型: {node.kind}")


# ---------------------------------------------------------------- 工作流注册中心
class WorkflowRegistry:
    """管理工作流持久化（data/workflows/*.json | *.yaml）、自由编辑删除与动态编译。"""

    def __init__(self, workflows_dir: str) -> None:
        self.workflows_dir = os.path.abspath(workflows_dir)
        os.makedirs(self.workflows_dir, exist_ok=True)
        self._ensure_initial_templates()

    def _ensure_initial_templates(self) -> None:
        """仅在工作流目录首次初始化（且无 .seeded 标记文件）时写入两套可自由编辑/删除的示例工作流。"""
        marker = os.path.join(self.workflows_dir, ".seeded_v2")
        existing_files = [
            f for f in os.listdir(self.workflows_dir)
            if f.endswith((".json", ".yaml", ".yml"))
        ]
        if os.path.isfile(marker):
            return
        # 若旧版曾自动生成过锁死的 builtin coding.json，将其替换为可自由编辑的 v2 模板；用户自定义的其它文件一律保留
        legacy_coding = os.path.join(self.workflows_dir, "coding.json")
        if os.path.isfile(legacy_coding):
            try:
                with open(legacy_coding, encoding="utf-8") as fh:
                    raw = json.load(fh)
                if raw.get("builtin") is True and raw.get("id") == "coding":
                    os.unlink(legacy_coding)
                    existing_files = [f for f in existing_files if f != "coding.json"]
            except Exception:  # noqa: BLE001
                pass

        if not existing_files:
            now_iso = _now_iso()
            for tpl in (seed_multi_agent_parallel_spec(), seed_iterative_coding_spec()):
                self._write_spec_file(tpl.model_copy(update={"updated_at": now_iso}))

        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(_now_iso() + "\n")

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
        if os.path.isdir(self.workflows_dir):
            for fname in sorted(os.listdir(self.workflows_dir)):
                fpath = os.path.join(self.workflows_dir, fname)
                if not os.path.isfile(fpath) or fname.startswith("."):
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
        result.sort(key=lambda s: (s.updated_at or "", s.id), reverse=True)
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
                except Exception as exc:  # noqa: BLE001
                    print(f"[warn] 读取工作流失败 {fpath}: {exc}", flush=True)
                    break
        return None

    def existing_ids(self) -> set[str]:
        return {s.id for s in self.list_workflows()}

    def save_workflow(self, spec: WorkflowSpec) -> WorkflowSpec:
        """校验拓扑与 LangGraph 编译合法性后落盘保存（支持新建与原地更新节点/连线）。"""
        build_state_graph(spec).compile()
        updated = spec.model_copy(
            update={
                "builtin": False,
                "updated_at": _now_iso(),
            }
        )
        self._write_spec_file(updated)
        return updated

    def create_blank_workflow(
        self,
        *,
        name: str,
        preferred_id: Optional[str] = None,
        description: str = "",
    ) -> WorkflowSpec:
        """一键创建并落盘保存一张全新的空白自定义工作流。"""
        wf_id = generate_unique_workflow_id(name, self.existing_ids(), preferred_id=preferred_id)
        spec = build_blank_workflow_spec(wf_id, name=name, description=description)
        return self.save_workflow(spec)

    def create_from_template(
        self,
        *,
        template_id: str,
        name: str,
        preferred_id: Optional[str] = None,
        description: str = "",
    ) -> WorkflowSpec:
        """基于已有工作流模板一键克隆并落盘保存为全新的自定义工作流。"""
        source = self.get_workflow(template_id)
        if source is None:
            raise KeyError(f"未找到源模板工作流: {template_id}")
        wf_id = generate_unique_workflow_id(name, self.existing_ids(), preferred_id=preferred_id)
        spec = clone_workflow_spec(source, wf_id, name=name, description=description)
        return self.save_workflow(spec)

    def delete_workflow(self, workflow_id: str) -> bool:
        """删除任意指定工作流（不再锁定禁止删除）。"""
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
        hooks: Optional[NodeRuntimeHooks] = None,
        checkpointer: Any = None,
    ) -> Any:
        spec = self.get_workflow(workflow_id)
        if spec is None:
            raise KeyError(f"未找到工作流: {workflow_id}")
        builder = build_state_graph(spec, hooks=hooks)
        return builder.compile(checkpointer=checkpointer)
