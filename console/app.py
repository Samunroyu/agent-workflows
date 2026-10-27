"""工作流操作台：列表 + 待审 gate + approve/revise/abort + diff 预览。

单进程设计：
- 自己持有编译好的图 + SqliteSaver，不依赖 langgraph dev
- 每个 run 在后台线程执行；interrupt 时线程结束、state 落盘
- 前端轮询 /api/runs，调 /api/runs/{id}/resume 完成人审

启动：
    .venv/bin/uvicorn console.app:app --host 0.0.0.0 --port 8080
"""

import json
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
from pydantic import BaseModel, Field

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from src.workflows.coding import build

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
DB_PATH = os.environ.get("CONSOLE_DB", os.path.join(ROOT, "data", "checkpoints.sqlite"))
FRONTEND_DIST = os.environ.get("CONSOLE_FRONTEND_DIST", os.path.join(ROOT, "frontend", "dist"))
REPO_DEFAULT = os.environ.get("CONSOLE_REPO_DEFAULT", "/opt/agent-workflows/demo-repo")
AGENT_KIND = os.environ.get("CONSOLE_AGENT", "stub")  # stub | codex

os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)

# ---------------------------------------------------------------- 图
_agent = None
if AGENT_KIND == "codex":
    from src.agents.codex import CodexAgent

    _agent = CodexAgent(
        repo_root=REPO_DEFAULT,
        codex_bin=os.environ.get("CONSOLE_CODEX_BIN", "codex"),
    )

_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_graph = build(agent=_agent).compile(checkpointer=SqliteSaver(_conn))

_async_lock = threading.Lock()   # 串行化 invoke（SqliteSaver 单写者更稳）
_errors: dict[str, str] = {}
_active: set[str] = set()        # 本进程内正在执行的 thread

app = FastAPI(
    title="agent-workflows console API",
    version="0.2.0",
    description="操作台后端 API：提供工作流运行列表、启动、详情查询与人工审批（interrupt/resume）接口。前后端唯一契约文件位于 shared/openapi.json。",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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


def _cfg(tid: str, *, traced: bool = False) -> dict:
    cfg = {
        "configurable": {"thread_id": tid},
        "metadata": {
            "thread_id": tid,
            "langfuse_session_id": tid,
            "langfuse_trace_name": f"coding {tid}",
        },
    }
    if traced:
        cbs = _tracing_callbacks(tid)
        if cbs:
            cfg["callbacks"] = cbs
    return cfg


def _snapshot(tid: str) -> Optional[Any]:
    try:
        return _graph.get_state(_cfg(tid))
    except Exception:  # noqa: BLE001
        return None


def _run_state(tid: str) -> dict:
    snap = _snapshot(tid)
    if snap is None:
        return {"thread_id": tid, "status": "unknown", "values": {}, "interrupts": []}

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


def _invoke(tid: str, payload: Any) -> None:
    _active.add(tid)
    try:
        with _async_lock:
            _graph.invoke(payload, _cfg(tid, traced=True))
        _errors.pop(tid, None)
    except Exception:  # noqa: BLE001
        _errors[tid] = traceback.format_exc(limit=3)
        print(f"[error] thread={tid}\n{_errors[tid]}", flush=True)
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
    stage: Optional[str] = Field(None, description="审批阶段：plan_approval | final_approval")
    task: Optional[str] = None
    plan: Optional[str] = None
    patch: Optional[str] = None
    test_output: Optional[str] = None
    review: Optional[str] = None
    options: list[str] = Field(default_factory=lambda: ["approve", "revise", "abort"])


class RunStateOut(BaseModel):
    thread_id: str
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
    warnings: list[str] = Field(default_factory=list)


class ResumeOut(BaseModel):
    ok: bool
    thread_id: str


class DebugThreadsOut(BaseModel):
    threads: list[str]


# ---------------------------------------------------------------- Routes
@app.get("/api/health", response_model=HealthOut)
def health() -> dict:
    return {"ok": True, "agent": AGENT_KIND}


@app.get("/api/runs", response_model=RunListOut)
def list_runs() -> dict:
    tids = _list_threads()
    runs = [_run_state(t) for t in tids]
    runs.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return {"runs": runs}


@app.post("/api/runs", response_model=StartRunOut)
def start_run(body: RunIn) -> Any:
    task = (body.task or "").strip()
    if not task:
        return JSONResponse({"error": "任务描述不能为空"}, status_code=400)

    repo = (body.repo or REPO_DEFAULT).strip()
    if not os.path.isdir(repo):
        return JSONResponse(
            {"error": f"repo 不存在或不是目录: {repo}（请使用操作台服务器上的路径）"},
            status_code=400,
        )

    warnings = []
    if not os.path.isdir(os.path.join(repo, ".git")):
        warnings.append(f"{repo} 不是 git 仓库：改动不会产生 diff，也拿不到 patch")

    tid = f"run-{int(time.time() * 1000)}"
    state = {
        "task": task,
        "repo": repo,
        "max_attempts": body.max_attempts,
        "attempt": 0,
        "events": [],
    }
    if body.test_cmd:
        state["test_cmd"] = body.test_cmd
    threading.Thread(target=_invoke, args=(tid, state), daemon=True).start()
    return {"thread_id": tid, "warnings": warnings}


@app.get("/api/runs/{tid}", response_model=RunStateOut)
def get_run(tid: str) -> Any:
    return run_state_or_404(tid)


@app.post("/api/runs/{tid}/resume", response_model=ResumeOut)
def resume_run(tid: str, body: ResumeIn) -> Any:
    st = _run_state(tid)
    if st["status"] != "waiting":
        return JSONResponse({"error": f"run 不在等待审批状态: {st['status']}"}, status_code=409)
    threading.Thread(
        target=_invoke,
        args=(tid, Command(resume={"action": body.action, "feedback": body.feedback})),
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


# 静态产物托管：若 frontend/dist 已构建则优先托管新前端，否则回退到旧版 console/index.html
if os.path.isdir(os.path.join(FRONTEND_DIST, "assets")):
    app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_DIST, "assets")), name="assets")


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    dist_index = os.path.join(FRONTEND_DIST, "index.html")
    if os.path.isfile(dist_index):
        return FileResponse(dist_index, media_type="text/html")
    return FileResponse(os.path.join(HERE, "index.html"), media_type="text/html")
