"""工作流操作台：工作流注册中心 + 运行列表 + 待审 gate + approve/revise/abort + diff 预览。

单进程设计：
- 通过 WorkflowRegistry 管理声明式工作流（data/workflows/*.json）并动态编译为 LangGraph 状态机
- 共用 SqliteSaver (data/checkpoints.sqlite)，不依赖 langgraph dev
- 每个 run 在后台线程执行；interrupt 时线程结束、state 落盘
- 前端通过 /api/workflows 拉取拓扑图渲染画布，轮询 /api/runs，调 /api/runs/{id}/resume 完成人审

启动：
    .venv/bin/uvicorn console.app:app --host 0.0.0.0 --port 8080
"""

import os
import sqlite3
import threading
import time
import traceback
from typing import Any, Literal, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field

from src.agents.base import AgentTarget, StubAgent
from src.registry import WorkflowRegistry, WorkflowSpec

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DB_PATH = os.environ.get("CONSOLE_DB", os.path.join(ROOT, "data", "checkpoints.sqlite"))
WORKFLOWS_DIR = os.environ.get("CONSOLE_WORKFLOWS_DIR", os.path.join(ROOT, "data", "workflows"))
FRONTEND_DIST = os.environ.get("CONSOLE_FRONTEND_DIST", os.path.join(ROOT, "frontend", "dist"))
REPO_DEFAULT = os.environ.get("CONSOLE_REPO_DEFAULT", ROOT)
AGENT_KIND = os.environ.get("CONSOLE_AGENT", "stub")  # stub | codex

os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
os.makedirs(os.path.abspath(WORKFLOWS_DIR), exist_ok=True)

# ---------------------------------------------------------------- Agent & Registry & Checkpointer
_agent: AgentTarget = StubAgent()
if AGENT_KIND == "codex":
    from src.agents.codex import CodexAgent

    _agent = CodexAgent(
        repo_root=REPO_DEFAULT,
        codex_bin=os.environ.get("CONSOLE_CODEX_BIN", "codex"),
    )

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_checkpointer = SqliteSaver(_conn)
_registry = WorkflowRegistry(WORKFLOWS_DIR)

# 初始化 run_meta 表：记录每个 thread_id 对应的 workflow_id
with _conn:
    _conn.execute(
        """
        CREATE TABLE IF NOT EXISTS run_meta (
            thread_id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )

_async_lock = threading.Lock()   # 串行化 invoke（SqliteSaver 单写者更稳）
_errors: dict[str, str] = {}
_active: set[str] = set()        # 本进程内正在执行的 thread

app = FastAPI(
    title="agent-workflows console API",
    version="0.3.0",
    description=(
        "操作台后端 API：提供工作流定义管理（/api/workflows）、运行列表、启动、详情查询与"
        "人工审批（interrupt/resume）接口。前后端唯一契约文件位于 shared/openapi.json。"
    ),
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def get_registry() -> WorkflowRegistry:
    return _registry


def _get_compiled_graph(workflow_id: str) -> Any:
    try:
        return _registry.compile_graph(
            workflow_id,
            agent=_agent,
            checkpointer=_checkpointer,
        )
    except KeyError:
        return _registry.compile_graph(
            "coding",
            agent=_agent,
            checkpointer=_checkpointer,
        )


def _record_run_workflow(tid: str, workflow_id: str) -> None:
    try:
        with _conn:
            _conn.execute(
                "INSERT OR REPLACE INTO run_meta (thread_id, workflow_id, created_at) VALUES (?, ?, ?)",
                (tid, workflow_id, time.time()),
            )
    except Exception:  # noqa: BLE001
        pass


def _get_run_workflow_id(tid: str) -> str:
    try:
        row = _conn.execute(
            "SELECT workflow_id FROM run_meta WHERE thread_id = ?",
            (tid,),
        ).fetchone()
        if row and row[0]:
            return str(row[0])
    except Exception:  # noqa: BLE001
        pass
    return "coding"


def _tracing_callbacks(tid: str) -> list:
    """有 LANGFUSE_PUBLIC_KEY 就上报 trace；一次 run 一条 trace。"""
    if not os.getenv("LANGFUSE_PUBLIC_KEY"):
        return []
    try:
        import hashlib

        from langfuse.langchain import CallbackHandler
        from langfuse.types import TraceContext
    except Exception:  # noqa: BLE001
        return []
    return [
        CallbackHandler(
            trace_context=TraceContext(
                trace_id=hashlib.md5(tid.encode()).hexdigest()
            )
        )
    ]


def _cfg(tid: str, *, workflow_id: str = "coding", traced: bool = False) -> dict:
    cfg = {
        "configurable": {"thread_id": tid},
        "metadata": {
            "thread_id": tid,
            "workflow_id": workflow_id,
            "langfuse_session_id": tid,
            "langfuse_trace_name": f"{workflow_id} {tid}",
        },
    }
    if traced:
        cbs = _tracing_callbacks(tid)
        if cbs:
            cfg["callbacks"] = cbs
    return cfg


def _snapshot(tid: str, workflow_id: str) -> Optional[Any]:
    try:
        graph = _get_compiled_graph(workflow_id)
        return graph.get_state(_cfg(tid, workflow_id=workflow_id))
    except Exception:  # noqa: BLE001
        return None


def _run_state(tid: str) -> dict:
    workflow_id = _get_run_workflow_id(tid)
    snap = _snapshot(tid, workflow_id)
    if snap is None or (not snap.values and not snap.next and not snap.created_at):
        return {
            "thread_id": tid,
            "workflow_id": workflow_id,
            "status": "unknown",
            "next": [],
            "values": {},
            "interrupts": [],
        }

    interrupts = [itr.value for itr in snap.interrupts]
    if _errors.get(tid):
        status = "error"
    elif interrupts:
        status = "waiting"
    elif snap.next:
        # 有下一步但本进程没在跑 -> 服务重启/进程被杀，标记为中断
        status = "running" if tid in _active else "interrupted"
    else:
        status = "done"

    return {
        "thread_id": tid,
        "workflow_id": workflow_id,
        "status": status,
        "next": list(snap.next),
        "values": snap.values,
        "interrupts": interrupts,
        "created_at": snap.created_at,
        "error": _errors.get(tid),
        "source": "codex" if AGENT_KIND == "codex" else "stub",
    }


def _list_threads(limit: int = 50) -> list[str]:
    try:
        rows = _conn.execute(
            "select thread_id, max(rowid) as r from checkpoints group by thread_id order by r desc limit ?",
            (limit,),
        ).fetchall()
        return [r[0] for r in rows]
    except Exception:  # noqa: BLE001
        return []


def _invoke(tid: str, workflow_id: str, payload: Any) -> None:
    _active.add(tid)
    try:
        graph = _get_compiled_graph(workflow_id)
        with _async_lock:
            graph.invoke(payload, _cfg(tid, workflow_id=workflow_id, traced=True))
        _errors.pop(tid, None)
    except Exception:  # noqa: BLE001
        _errors[tid] = traceback.format_exc(limit=3)
        print(f"[error] thread={tid} workflow={workflow_id}\n{_errors[tid]}", flush=True)
    finally:
        _active.discard(tid)
        _flush_traces()


def _flush_traces() -> None:
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- API Models (Contract)
RunStatus = Literal["waiting", "running", "done", "error", "interrupted", "unknown"]
ResumeAction = Literal["approve", "revise", "abort"]


class RunIn(BaseModel):
    task: str = Field(..., description="任务描述，例如：给 verify_graph.py 加 --verbose 参数")
    workflow_id: str = Field("coding", description="要运行的工作流 ID（默认 'coding'）")
    repo: Optional[str] = Field(None, description="目标 git 仓库路径（服务器本地路径）")
    max_attempts: int = Field(2, ge=1, le=10, description="测试-修复循环最大尝试次数")
    test_cmd: Optional[str] = Field(None, description="测试命令（在 repo 下执行，退出码 0 视为通过）")


class ResumeIn(BaseModel):
    action: ResumeAction = Field("approve", description="审批动作：approve(通过) | revise(打回重改) | abort(中止)")
    feedback: str = Field("", description="revise 时的修改意见（approve/abort 可留空）")


class HealthOut(BaseModel):
    ok: bool
    agent: str


class RunValues(BaseModel):
    model_config = ConfigDict(extra="allow")

    task: Optional[str] = None
    repo: Optional[str] = None
    test_cmd: Optional[str] = None
    max_attempts: Optional[int] = None
    plan: Optional[str] = None
    feedback: Optional[str] = None
    action: Optional[str] = None
    approved: Optional[bool] = None
    attempt: Optional[int] = None
    patch: Optional[str] = None
    test_output: Optional[str] = None
    passed: Optional[bool] = None
    review: Optional[str] = None
    result: Optional[str] = None
    events: list[str] = Field(default_factory=list)


class InterruptPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    stage: Optional[str] = Field(None, description="审批阶段：plan_approval | final_approval 等")
    task: Optional[str] = None
    plan: Optional[str] = None
    patch: Optional[str] = None
    test_output: Optional[str] = None
    review: Optional[str] = None
    options: list[str] = Field(default_factory=lambda: ["approve", "revise", "abort"])


class RunStateOut(BaseModel):
    thread_id: str
    workflow_id: str = Field("coding", description="该运行所属的工作流 ID")
    status: RunStatus
    next: list[str] = Field(default_factory=list)
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


class WorkflowListOut(BaseModel):
    workflows: list[WorkflowSpec]


class DeleteWorkflowOut(BaseModel):
    ok: bool
    workflow_id: str


class ErrorOut(BaseModel):
    error: str


class DebugThreadsOut(BaseModel):
    threads: list[str]


# ---------------------------------------------------------------- Service Helpers (供 API 与 MCP 共用)
def start_run_service(
    *,
    task: str,
    workflow_id: str = "coding",
    repo: Optional[str] = None,
    max_attempts: int = 2,
    test_cmd: Optional[str] = None,
) -> dict[str, Any]:
    clean_task = (task or "").strip()
    if not clean_task:
        raise ValueError("任务描述不能为空")

    wf_spec = _registry.get_workflow(workflow_id)
    if wf_spec is None:
        raise KeyError(f"工作流不存在: {workflow_id}")

    target_repo = (repo or REPO_DEFAULT).strip()
    if not os.path.isdir(target_repo):
        raise ValueError(f"repo 不存在或不是目录: {target_repo}（请使用操作台服务器上的路径）")

    warnings: list[str] = []
    if not os.path.isdir(os.path.join(target_repo, ".git")):
        warnings.append(f"{target_repo} 不是 git 仓库：改动不会产生 diff，也拿不到 patch")

    tid = f"run-{int(time.time() * 1000)}"
    _record_run_workflow(tid, wf_spec.id)
    state: dict[str, Any] = {
        "task": clean_task,
        "repo": target_repo,
        "max_attempts": max_attempts,
        "attempt": 0,
        "events": [],
    }
    if test_cmd:
        state["test_cmd"] = test_cmd

    threading.Thread(target=_invoke, args=(tid, wf_spec.id, state), daemon=True).start()
    return {"thread_id": tid, "workflow_id": wf_spec.id, "warnings": warnings}


# ---------------------------------------------------------------- Routes: Health & Workflows
@app.get("/api/health", response_model=HealthOut)
def health() -> dict:
    return {"ok": True, "agent": AGENT_KIND}


@app.get(
    "/api/workflows",
    response_model=WorkflowListOut,
    summary="列出所有工作流定义（含节点与连线拓扑）",
)
def list_workflows() -> dict:
    return {"workflows": _registry.list_workflows()}


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
    summary="创建或更新工作流定义（外部 Agent 或前端可调用，落盘后 Web 立即可见）",
)
def save_workflow(spec: WorkflowSpec) -> Any:
    try:
        return _registry.save_workflow(spec)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.delete(
    "/api/workflows/{workflow_id}",
    response_model=DeleteWorkflowOut,
    responses={400: {"model": ErrorOut}, 404: {"model": ErrorOut}},
    summary="删除自定义工作流（内置 coding 工作流不可删除）",
)
def delete_workflow(workflow_id: str) -> Any:
    try:
        removed = _registry.delete_workflow(workflow_id)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if not removed:
        return JSONResponse({"error": f"未找到工作流: {workflow_id}"}, status_code=404)
    return {"ok": True, "workflow_id": workflow_id}


# ---------------------------------------------------------------- Routes: Runs
@app.get("/api/runs", response_model=RunListOut)
def list_runs() -> dict:
    tids = _list_threads()
    runs = [_run_state(t) for t in tids]
    runs.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return {"runs": runs}


@app.post(
    "/api/runs",
    response_model=StartRunOut,
    responses={400: {"model": ErrorOut}, 404: {"model": ErrorOut}},
)
def start_run(body: RunIn) -> Any:
    try:
        return start_run_service(
            task=body.task,
            workflow_id=body.workflow_id,
            repo=body.repo,
            max_attempts=body.max_attempts,
            test_cmd=body.test_cmd,
        )
    except KeyError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.get(
    "/api/runs/{tid}",
    response_model=RunStateOut,
    responses={404: {"model": ErrorOut}},
)
def get_run(tid: str) -> Any:
    return run_state_or_404(tid)


@app.post(
    "/api/runs/{tid}/resume",
    response_model=ResumeOut,
    responses={409: {"model": ErrorOut}},
)
def resume_run(tid: str, body: ResumeIn) -> Any:
    st = _run_state(tid)
    if st["status"] != "waiting":
        return JSONResponse({"error": f"run 不在等待审批状态: {st['status']}"}, status_code=409)
    workflow_id = st.get("workflow_id") or "coding"
    threading.Thread(
        target=_invoke,
        args=(
            tid,
            workflow_id,
            Command(resume={"action": body.action, "feedback": body.feedback}),
        ),
        daemon=True,
    ).start()
    return {"ok": True, "thread_id": tid}


def run_state_or_404(tid: str) -> Any:
    st = _run_state(tid)
    if st["status"] == "unknown":
        return JSONResponse({"error": "not found"}, status_code=404)
    return st


@app.get("/api/debug/threads", response_model=DebugThreadsOut)
def debug_threads() -> dict:
    """排查用：直接看 checkpoint 里有哪些 thread。"""
    return {"threads": _list_threads(200)}


# 挂载官方 MCP Server (SSE 远程端点: /mcp/sse)
try:
    from src.mcp_server import mcp as _mcp_server

    app.mount("/mcp", _mcp_server.sse_app())
except Exception as _mcp_exc:  # noqa: BLE001
    print(f"[warn] MCP HTTP 挂载跳过: {_mcp_exc}", flush=True)

# 静态产物托管：若 frontend/dist 已构建则优先托管新前端，否则回退到旧版 console/index.html
if os.path.isdir(os.path.join(FRONTEND_DIST, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIST, "assets")), name="assets")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    dist_index = os.path.join(FRONTEND_DIST, "index.html")
    if os.path.isfile(dist_index):
        return FileResponse(dist_index, media_type="text/html")
    return FileResponse(os.path.join(HERE, "index.html"), media_type="text/html")
