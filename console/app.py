"""工作流操作台后端 API v2：多 CLI Agent 注册 + 可编辑工作流引擎 + 自然语言构图 + 实时流式控场。

核心设计：
- 默认彻底关闭假 StubAgent，通过 AgentRegistry 自动探测并调用本机已登录的真实 CLI（codex / claude / kimi / gemini / 自定义）
- 通过 WorkflowRegistry v2 管理四类核心节点（agent / shell / gate / condition）、并行汇聚与条件循环工作流
- 通过 WorkflowRuntimeManager 提供 per-run 并发、SSE 实时日志流、随时强杀中止、失败节点原地重试、
  人工卡点在线编辑上游产出与自选打回任意上游节点
- 前后端唯一接口契约导出至 shared/openapi.json

启动：
    .venv/bin/uvicorn console.app:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import os
import platform
import socket
from typing import Any, Callable, Literal, Optional

from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from src.agents.registry import AgentProfileOut, AgentProfileSpec, AgentRegistry
from src.copilot import CopilotRequest, CopilotResponse, generate_or_modify_workflow
from src.registry import (
    WorkflowRegistry,
    WorkflowSpec,
    build_state_graph,
    compute_upstream_ancestors,
)
from src.runtime import RunLogEntry, RunStatus, WorkflowRuntimeManager

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DATA_DIR = os.environ.get("CONSOLE_DATA_DIR", os.path.join(ROOT, "data"))
DB_PATH = os.environ.get("CONSOLE_DB", os.path.join(DATA_DIR, "checkpoints.sqlite"))
WORKFLOWS_DIR = os.environ.get("CONSOLE_WORKFLOWS_DIR", os.path.join(DATA_DIR, "workflows"))
AGENTS_CONFIG_PATH = os.environ.get("CONSOLE_AGENTS_FILE", os.path.join(DATA_DIR, "agents.json"))
FRONTEND_DIST = os.environ.get("CONSOLE_FRONTEND_DIST", os.path.join(ROOT, "frontend", "dist"))
REPO_DEFAULT = os.environ.get("CONSOLE_REPO_DEFAULT", ROOT)

os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
os.makedirs(os.path.abspath(WORKFLOWS_DIR), exist_ok=True)

_agent_registry = AgentRegistry(AGENTS_CONFIG_PATH)
_registry = WorkflowRegistry(WORKFLOWS_DIR)
_runtime = WorkflowRuntimeManager(
    db_path=DB_PATH,
    workflow_registry=_registry,
    agent_registry=_agent_registry,
    default_repo=REPO_DEFAULT,
)

app = FastAPI(
    title="agent-workflows console API",
    version="2.0.0",
    description=(
        "多 Agent 研发协同编排操作台后端 API v2：提供 CLI Agent 管理（/api/agents）、Worker 节点状态（/api/workers）、"
        "工作流可视化拓扑增删改与自然语言改图（/api/workflows, /api/workflows/copilot）、"
        "以及运行期实时日志流、强杀中止、单节点重试与增强人工卡点（/api/runs/*）。"
        "前后端唯一契约文件位于 shared/openapi.json。"
    ),
)

_DEFAULT_CORS_ORIGINS = (
    "http://127.0.0.1:5173,http://localhost:5173,"
    "http://127.0.0.1:8080,http://localhost:8080,"
    "http://127.0.0.1:18080,http://localhost:18080"
)
CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CONSOLE_CORS_ORIGINS", _DEFAULT_CORS_ORIGINS).split(",")
    if origin.strip() and origin.strip() != "*"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "Accept"],
)


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next: Callable[[Request], Any]) -> Any:
    """强制执行 Fetch Metadata 跨站资源隔离与现代 Web 安全/隐私响应头。"""
    site = (request.headers.get("sec-fetch-site") or "").lower()
    mode = (request.headers.get("sec-fetch-mode") or "").lower()
    dest = (request.headers.get("sec-fetch-dest") or "").lower()
    if site and site not in ("same-origin", "same-site", "none"):
        is_safe_top_nav = (
            site == "cross-site"
            and mode == "navigate"
            and request.method == "GET"
            and dest not in ("object", "embed", "iframe", "frame")
        )
        if not is_safe_top_nav:
            resp = JSONResponse({"error": "Forbidden cross-site request"}, status_code=403)
            resp.headers["Vary"] = "Sec-Fetch-Dest, Sec-Fetch-Mode, Sec-Fetch-Site"
            return resp

    response = await call_next(request)
    response.headers["Vary"] = "Sec-Fetch-Dest, Sec-Fetch-Mode, Sec-Fetch-Site"
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault(
        "Permissions-Policy",
        "camera=(), geolocation=(), microphone=(), focus-without-user-activation=()",
    )
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin-allow-popups")
    response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
    response.headers.setdefault(
        "Content-Security-Policy",
        "base-uri 'none'; object-src 'none'; frame-ancestors 'self'",
    )
    return response


# ---------------------------------------------------------------- Helpers (供内部与 MCP 共用)
def get_registry() -> WorkflowRegistry:
    return _registry


def get_agent_registry() -> AgentRegistry:
    return _agent_registry


def get_runtime() -> WorkflowRuntimeManager:
    return _runtime


def _run_state(tid: str) -> dict[str, Any]:
    return _runtime.get_run_state(tid)


def start_run_service(
    *,
    task: str = "",
    workflow_id: str = "iterative-coding",
    inputs: Optional[dict[str, Any]] = None,
    repo: Optional[str] = None,
    max_attempts: int = 2,
    test_cmd: Optional[str] = None,
) -> dict[str, Any]:
    return _runtime.start_run(
        workflow_id=workflow_id,
        task=task,
        inputs=inputs,
        repo=repo,
        test_cmd=test_cmd,
        max_attempts=max_attempts,
    )


# ---------------------------------------------------------------- API Models (Contract)
ResumeAction = Literal["approve", "revise", "abort"]


class HealthOut(BaseModel):
    ok: bool
    agent: str = Field(..., description="当前主机在线的 CLI Agent 摘要（如 'codex' 或 'codex,claude'；均未安装时为 'none'）")
    online_agents: list[str] = Field(default_factory=list, description="当前就绪可调用的 CLI Agent ID 列表")
    workers: list[str] = Field(default_factory=lambda: ["local"], description="当前在线的 Worker 主机 ID 列表")
    default_repo: str = Field(..., description="操作台默认工作区路径")


class AgentListOut(BaseModel):
    agents: list[AgentProfileOut]


class DeleteAgentOut(BaseModel):
    ok: bool
    agent_id: str


class WorkerInfoOut(BaseModel):
    id: str = Field(..., description="Worker 唯一标识（内置本机为 'local'）")
    name: str = Field(..., description="主机名称展示")
    hostname: str = Field(..., description="操作系统 hostname")
    platform: str = Field(..., description="操作系统与架构信息")
    online: bool = Field(True, description="是否在线可调度")
    is_local: bool = Field(True, description="是否为操作台本机内置 Worker")
    default_repo: str = Field(..., description="该主机默认代码目录")
    online_agents: list[str] = Field(default_factory=list, description="该主机上探测到可用的 CLI Agent ID 列表")


class WorkerListOut(BaseModel):
    workers: list[WorkerInfoOut]


class WorkflowListOut(BaseModel):
    workflows: list[WorkflowSpec]


class DeleteWorkflowOut(BaseModel):
    ok: bool
    workflow_id: str


class ValidateWorkflowOut(BaseModel):
    valid: bool
    error: Optional[str] = None
    upstream_map: dict[str, list[str]] = Field(
        default_factory=dict,
        description="每个节点可引用的上游节点 ID 列表（供前端 Prompt 编辑器提示 {节点ID.output} / {节点ID.diff} 变量）",
    )


class RunIn(BaseModel):
    workflow_id: str = Field(..., description="要运行的工作流 ID")
    task: str = Field("", description="任务描述（也可放在 inputs.task 中）")
    inputs: dict[str, Any] = Field(
        default_factory=dict,
        description="对应 WorkflowSpec.inputs 定义的动态启动参数键值对",
    )
    repo: Optional[str] = Field(None, description="可选：默认工作目录覆盖")
    test_cmd: Optional[str] = Field(None, description="可选：测试命令快捷参数")
    max_attempts: int = Field(3, ge=1, le=20, description="最大循环尝试次数参考值")


class ResumeIn(BaseModel):
    action: ResumeAction = Field("approve", description="审批动作：approve(放行) | revise(打回指定节点) | abort(中止)")
    feedback: str = Field("", description="审批或打回修改意见（注入下游/打回节点的 {feedback} 变量）")
    edited_outputs: dict[str, str] = Field(
        default_factory=dict,
        description="可选：人工直接在线修改的上游节点产出文本映射 {node_id: 修改后的文本}，放行前自动写回 State",
    )
    target_node_id: Optional[str] = Field(
        None,
        description="当 action='revise' 时，指定要打回重跑的上游节点 ID（可从 interrupt.rewind_options 中选择）",
    )


class RetryRunIn(BaseModel):
    node_id: Optional[str] = Field(None, description="要原地重试的节点 ID（留空则自动重试当前停靠/报错的断点节点）")
    prompt_template: Optional[str] = Field(None, description="可选：重试该 Agent 节点时临时覆盖的 Prompt 模板")
    command: Optional[str] = Field(None, description="可选：重试该 Shell 节点时临时覆盖的命令")
    agent_id: Optional[str] = Field(None, description="可选：重试时临时切换使用的 CLI Agent ID")
    cwd: Optional[str] = Field(None, description="可选：重试时临时覆盖的工作目录")


class NodeExecutionOut(BaseModel):
    model_config = ConfigDict(extra="allow")

    node_id: str
    label: Optional[str] = None
    kind: Optional[str] = None
    status: Optional[str] = Field(None, description="running | succeeded | failed | cancelled | approve | revise | abort")
    ok: Optional[bool] = None
    exit_code: Optional[int] = None
    output: Optional[str] = Field(None, description="节点主产出文本（对应模板变量 {node_id.output}）")
    stdout: Optional[str] = Field(None, description="完整标准输出文本（对应模板变量 {node_id.stdout}）")
    diff: Optional[str] = Field(None, description="工作区 Git Diff（对应模板变量 {node_id.diff}）")
    runs: int = Field(1, description="该节点在本次运行中已执行的次数")
    worker_id: Optional[str] = None
    agent_id: Optional[str] = None
    cwd: Optional[str] = None
    duration_ms: Optional[int] = None
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    edited_by_human: bool = Field(False, description="该节点产出是否已被人工在卡点在线修订过")


class RewindOptionOut(BaseModel):
    id: str = Field(..., description="可打回的上游节点 ID")
    label: str = Field(..., description="上游节点显示名称")


class UpstreamSnapshotOut(BaseModel):
    node_id: str
    label: str
    kind: str = "agent"
    ok: bool = True
    exit_code: Optional[int] = 0
    output: str = ""
    diff: str = ""
    stdout: str = ""
    edited_by_human: bool = False


class InterruptPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    node_id: Optional[str] = Field(None, description="当前停靠的 Gate 节点 ID")
    label: Optional[str] = Field(None, description="当前 Gate 节点名称")
    stage: Optional[str] = Field(None, description="卡点阶段标识")
    instruction: Optional[str] = Field(None, description="审批说明")
    allow_edit_output: bool = Field(True, description="是否允许在线直接编辑 upstream_outputs 中的 output 文本")
    upstream_outputs: dict[str, UpstreamSnapshotOut] = Field(
        default_factory=dict,
        description="供审批人审阅（及在线编辑）的上游节点产出与 Git Diff 映射",
    )
    rewind_options: list[RewindOptionOut] = Field(
        default_factory=list,
        description="点击『打回』时允许选择回退的目标上游节点列表",
    )
    options: list[str] = Field(default_factory=lambda: ["approve", "revise", "abort"])


class RunValues(BaseModel):
    model_config = ConfigDict(extra="allow")

    inputs: dict[str, Any] = Field(default_factory=dict, description="本次运行的启动入参键值")
    task: Optional[str] = None
    repo: Optional[str] = None
    feedback: Optional[str] = None
    action: Optional[str] = None
    result: Optional[str] = None
    nodes: dict[str, NodeExecutionOut] = Field(
        default_factory=dict,
        description="按节点 ID 索引的各节点结构化执行状态、产出与 Diff（点击画布任意节点即展示此详情）",
    )
    events: list[str] = Field(default_factory=list)


class RunStateOut(BaseModel):
    thread_id: str
    workflow_id: str
    status: RunStatus
    next: list[str] = Field(default_factory=list, description="当前停靠或即将执行的节点 ID 列表")
    running_nodes: list[str] = Field(default_factory=list, description="此刻正在活跃执行中的节点 ID 列表（支持并行多节点高亮）")
    values: RunValues = Field(default_factory=RunValues)
    interrupts: list[InterruptPayload] = Field(default_factory=list)
    created_at: Optional[str] = None
    error: Optional[str] = None
    source: Optional[str] = None


class RunListOut(BaseModel):
    runs: list[RunStateOut]


class StartRunOut(BaseModel):
    thread_id: str
    workflow_id: str
    warnings: list[str] = Field(default_factory=list)


class ResumeOut(BaseModel):
    ok: bool
    thread_id: str


class CancelRunOut(BaseModel):
    ok: bool
    thread_id: str
    killed_processes: int = 0


class RetryRunOut(BaseModel):
    ok: bool
    thread_id: str
    retried_node_id: str


class RunLogsOut(BaseModel):
    thread_id: str
    logs: list[RunLogEntry]
    next_seq: int = Field(..., description="下次增量轮询传入 after_seq 的游标值")


class ErrorOut(BaseModel):
    error: str


class DebugThreadsOut(BaseModel):
    threads: list[str]


# ---------------------------------------------------------------- Routes: Health, Agents & Workers
@app.get("/api/health", response_model=HealthOut, summary="服务健康检查与本机可用 CLI Agent 探测摘要")
def health() -> dict[str, Any]:
    online = [a.id for a in _agent_registry.list_agents() if a.available]
    return {
        "ok": True,
        "agent": ",".join(online) if online else "none",
        "online_agents": online,
        "workers": ["local"],
        "default_repo": REPO_DEFAULT,
    }


@app.get("/api/agents", response_model=AgentListOut, summary="列出所有内置与自定义 CLI Agent 及其本机安装就绪状态")
def list_agents() -> dict[str, Any]:
    return {"agents": _agent_registry.list_agents()}


@app.post(
    "/api/agents",
    response_model=AgentProfileOut,
    responses={400: {"model": ErrorOut}},
    summary="新增或修改 CLI Agent 配置（如添加自定义 CLI 模板或指定二进制绝对路径）",
)
def save_agent(spec: AgentProfileSpec) -> Any:
    try:
        return _agent_registry.save_agent(spec)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.delete(
    "/api/agents/{agent_id}",
    response_model=DeleteAgentOut,
    responses={400: {"model": ErrorOut}, 404: {"model": ErrorOut}},
    summary="删除自定义 CLI Agent（或将已覆盖的内置预设恢复为默认值）",
)
def delete_agent(agent_id: str) -> Any:
    try:
        removed = _agent_registry.delete_agent(agent_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not removed:
        return JSONResponse({"error": f"未找到自定义 Agent: {agent_id}"}, status_code=404)
    return {"ok": True, "agent_id": agent_id}


@app.get(
    "/api/workers",
    response_model=WorkerListOut,
    summary="列出可用执行主机 Worker（一期提供本机 local，二期支持多主机轻量守护进程接入）",
)
def list_workers() -> dict[str, Any]:
    online_agents = [a.id for a in _agent_registry.list_agents() if a.available]
    host = socket.gethostname()
    return {
        "workers": [
            WorkerInfoOut(
                id="local",
                name=f"操作台本机 ({host})",
                hostname=host,
                platform=f"{platform.system()} {platform.machine()}",
                online=True,
                is_local=True,
                default_repo=REPO_DEFAULT,
                online_agents=online_agents,
            )
        ]
    }


# ---------------------------------------------------------------- Routes: Workflows & NL Copilot
@app.get(
    "/api/workflows",
    response_model=WorkflowListOut,
    summary="列出所有工作流定义（全部支持可视化增删节点与自定义编辑）",
)
def list_workflows() -> dict[str, Any]:
    return {"workflows": _registry.list_workflows()}


@app.post(
    "/api/workflows/validate",
    response_model=ValidateWorkflowOut,
    summary="校验画布工作流拓扑合法性并返回各节点可引用的上游节点映射",
)
def validate_workflow(spec: WorkflowSpec) -> dict[str, Any]:
    try:
        build_state_graph(spec).compile()
        upstream_map = {
            n.id: [a.id for a in compute_upstream_ancestors(spec, n.id)]
            for n in spec.nodes
        }
        return {"valid": True, "error": None, "upstream_map": upstream_map}
    except Exception as exc:  # noqa: BLE001
        return {"valid": False, "error": str(exc), "upstream_map": {}}


@app.post(
    "/api/workflows/copilot",
    response_model=CopilotResponse,
    responses={400: {"model": ErrorOut}, 503: {"model": ErrorOut}},
    summary="一句话自然语言生成新工作流或修改当前画布工作流（直接复用本机已登录 CLI Agent）",
)
def workflow_copilot(body: CopilotRequest) -> Any:
    try:
        return generate_or_modify_workflow(
            body,
            agent_registry=_agent_registry,
            workflow_registry=_registry,
        )
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=503)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get(
    "/api/workflows/{workflow_id}",
    response_model=WorkflowSpec,
    responses={404: {"model": ErrorOut}},
    summary="获取单个工作流的完整定义与拓扑结构",
)
def get_workflow(workflow_id: str) -> Any:
    spec = _registry.get_workflow(workflow_id)
    if spec is None:
        return JSONResponse({"error": f"未找到工作流: {workflow_id}"}, status_code=404)
    return spec


@app.post(
    "/api/workflows",
    response_model=WorkflowSpec,
    responses={400: {"model": ErrorOut}},
    summary="创建或保存工作流定义（支持新建、复制、增删节点、修改连线与启动参数）",
)
def save_workflow(spec: WorkflowSpec) -> Any:
    try:
        return _registry.save_workflow(spec)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.put(
    "/api/workflows/{workflow_id}",
    response_model=WorkflowSpec,
    responses={400: {"model": ErrorOut}},
    summary="按指定 workflow_id 更新工作流定义",
)
def update_workflow(workflow_id: str, spec: WorkflowSpec) -> Any:
    try:
        normalized = spec.model_copy(update={"id": workflow_id})
        return _registry.save_workflow(normalized)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.delete(
    "/api/workflows/{workflow_id}",
    response_model=DeleteWorkflowOut,
    responses={404: {"model": ErrorOut}},
    summary="删除指定工作流",
)
def delete_workflow(workflow_id: str) -> Any:
    removed = _registry.delete_workflow(workflow_id)
    if not removed:
        return JSONResponse({"error": f"未找到工作流: {workflow_id}"}, status_code=404)
    return {"ok": True, "workflow_id": workflow_id}


# ---------------------------------------------------------------- Routes: Runs, Streaming Logs & Flow Control
@app.get("/api/runs", response_model=RunListOut, summary="列出最近的工作流运行记录")
def list_runs() -> dict[str, Any]:
    tids = _runtime.list_thread_ids()
    runs = [_runtime.get_run_state(t) for t in tids]
    runs = [r for r in runs if r.get("status") != "unknown"]
    runs.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return {"runs": runs}


@app.post(
    "/api/runs",
    response_model=StartRunOut,
    responses={400: {"model": ErrorOut}, 404: {"model": ErrorOut}},
    summary="按指定工作流与动态入参启动一次运行",
)
def start_run(body: RunIn) -> Any:
    try:
        return _runtime.start_run(
            workflow_id=body.workflow_id,
            task=body.task,
            inputs=body.inputs,
            repo=body.repo,
            test_cmd=body.test_cmd,
            max_attempts=body.max_attempts,
        )
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get(
    "/api/runs/{tid}",
    response_model=RunStateOut,
    responses={404: {"model": ErrorOut}},
    summary="查询单次运行的完整状态、活跃并行节点、各节点产出/Diff 与待审批卡点载荷",
)
def get_run(tid: str) -> Any:
    st = _runtime.get_run_state(tid)
    if st["status"] == "unknown":
        return JSONResponse({"error": "not found"}, status_code=404)
    return st


@app.get(
    "/api/runs/{tid}/logs",
    response_model=RunLogsOut,
    summary="增量获取运行实时日志（支持按 node_id 过滤单节点 CLI 输出，用 after_seq 无损拉取新行）",
)
def get_run_logs(
    tid: str,
    node_id: Optional[str] = Query(None, description="可选：仅筛选某个节点的日志"),
    after_seq: int = Query(0, ge=0, description="上次获取到的最大 seq，返回 seq > after_seq 的增量日志"),
    limit: int = Query(1000, ge=1, le=5000, description="单次最多返回条数"),
) -> dict[str, Any]:
    entries = _runtime.get_logs(tid, node_id=node_id, after_seq=after_seq, limit=limit)
    next_seq = entries[-1].seq if entries else after_seq
    return {"thread_id": tid, "logs": entries, "next_seq": next_seq}


@app.get(
    "/api/runs/{tid}/stream",
    summary="SSE 实时流式订阅某次运行的 CLI 输出日志与状态变更（text/event-stream）",
)
def stream_run_events(
    tid: str,
    after_seq: int = Query(0, ge=0, description="起始日志序号游标"),
) -> StreamingResponse:
    return StreamingResponse(
        _runtime.stream_sse(tid, after_seq=after_seq),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post(
    "/api/runs/{tid}/resume",
    response_model=ResumeOut,
    responses={409: {"model": ErrorOut}},
    summary="人工卡点决策：支持直接放行、在线修改上游节点产出后放行、自选打回到任意上游节点、或中止",
)
def resume_run(tid: str, body: ResumeIn) -> Any:
    try:
        return _runtime.resume_run(
            tid,
            action=body.action,
            feedback=body.feedback,
            edited_outputs=body.edited_outputs,
            target_node_id=body.target_node_id,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=409)


@app.post(
    "/api/runs/{tid}/cancel",
    response_model=CancelRunOut,
    responses={400: {"model": ErrorOut}, 404: {"model": ErrorOut}},
    summary="强杀中止当前正在执行的 CLI/Shell 子进程组并暂停运行（保留断点供随时重试）",
)
def cancel_run(tid: str) -> Any:
    try:
        return _runtime.cancel_run(tid)
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post(
    "/api/runs/{tid}/retry",
    response_model=RetryRunOut,
    responses={400: {"model": ErrorOut}, 404: {"model": ErrorOut}},
    summary="从报错、被强杀或中断的节点原地重试（支持临时覆盖 Prompt / 命令 / Agent / 目录）",
)
def retry_run(tid: str, body: RetryRunIn) -> Any:
    try:
        return _runtime.retry_run(
            tid,
            node_id=body.node_id,
            prompt_template=body.prompt_template,
            command=body.command,
            agent_id=body.agent_id,
            cwd=body.cwd,
        )
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get("/api/debug/threads", response_model=DebugThreadsOut)
def debug_threads() -> dict[str, Any]:
    return {"threads": _runtime.list_thread_ids(200)}


# 挂载官方 MCP Server
try:
    from src.mcp_server import mcp as _mcp_server

    app.mount("/mcp", _mcp_server.sse_app())
except Exception as _mcp_exc:  # noqa: BLE001
    print(f"[warn] MCP HTTP 挂载跳过: {_mcp_exc}", flush=True)


# 静态产物托管
@app.get("/assets/{file_path:path}", include_in_schema=False)
def frontend_assets(file_path: str) -> Any:
    assets_root = os.path.abspath(os.path.join(FRONTEND_DIST, "assets"))
    target = os.path.abspath(os.path.join(assets_root, file_path))
    if not target.startswith(assets_root + os.sep) or not os.path.isfile(target):
        return JSONResponse({"error": "asset not found"}, status_code=404)
    return FileResponse(target)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    dist_index = os.path.join(FRONTEND_DIST, "index.html")
    if os.path.isfile(dist_index):
        return FileResponse(dist_index, media_type="text/html")
    return FileResponse(os.path.join(HERE, "index.html"), media_type="text/html")
