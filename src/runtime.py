"""工作流运行期控制器（WorkflowRuntimeManager）。

核心职责：
1. 摆脱旧版全局串行锁 _async_lock：按 thread_id 独立加锁，天然支持多工作流并发与单工作流内多节点并行。
2. 实时日志流中心（RunLogHub）：每行 CLI/Shell 输出即时落盘到 SQLite `run_logs` 并广播到内存 Condition，
   同时支持 SSE 实时流（GET /api/runs/{tid}/stream）与增量分页拉取（GET /api/runs/{tid}/logs）。
3. 进程组强杀中止（cancel_run）：随时终止指定运行的所有活跃 CLI/Shell 子进程组，保留断点供后续重试。
4. 失败/中止节点原地重试（retry_run）：无需整条工作流重头再来，支持临时覆盖 Prompt / 命令 / Agent / 目录后原地继续。
5. 增强型人工卡点恢复（resume_run）：支持在线修改任意上游节点的产出文本后放行，或自选打回到任意上游节点。
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
import threading
import time
import traceback
from typing import Any, Generator, Literal, Optional

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel, Field

from src.agents.registry import (
    AgentRegistry,
    CliExecResult,
    ProcessSupervisor,
    run_streaming_subprocess,
)
from src.registry import (
    NodeExecutionError,
    NodeResultData,
    NodeSpec,
    WorkflowRegistry,
)

RunStatus = Literal["waiting", "running", "done", "error", "cancelled", "interrupted", "unknown"]
LogStreamKind = Literal["stdout", "stderr", "system"]


class RunLogEntry(BaseModel):
    seq: int = Field(..., description="单次运行内单调递增的日志序号（从 1 开始）")
    ts: str = Field(..., description="ISO-8601 时间戳")
    node_id: str = Field(..., description="产生该日志的节点 ID（系统事件为 '__system__'）")
    stream: LogStreamKind = Field("stdout", description="日志流类型：stdout | stderr | system")
    text: str = Field(..., description="单行日志内容")


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class WorkflowRuntimeManager:
    """统一管理检查点数据库、实时节点日志、子进程生命周期与运行控制。"""

    def __init__(
        self,
        *,
        db_path: str,
        workflow_registry: WorkflowRegistry,
        agent_registry: AgentRegistry,
        default_repo: str,
    ) -> None:
        self.db_path = os.path.abspath(db_path)
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self.workflow_registry = workflow_registry
        self.agent_registry = agent_registry
        self.default_repo = os.path.abspath(default_repo)

        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.checkpointer = SqliteSaver(self._conn)
        self.supervisor = ProcessSupervisor()

        self._meta_lock = threading.Lock()
        self._thread_locks: dict[str, threading.Lock] = {}
        self._active_runs: set[str] = set()
        # 实时执行中的节点快照（弥补并行 superstep 尚未落 checkpoint 前或节点抛错回滚后的状态可见性）
        self._live_nodes: dict[str, dict[str, NodeResultData]] = {}
        # SSE 实时日志通知条件变量：thread_id -> Condition
        self._log_conds: dict[str, threading.Condition] = {}
        self._log_seq_cache: dict[str, int] = {}

        self._init_tables()

    def _init_tables(self) -> None:
        with self.checkpointer.lock, self._conn:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_meta (
                    thread_id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    error TEXT,
                    status_override TEXT,
                    failed_nodes_json TEXT
                )
                """
            )
            for col in ("error TEXT", "status_override TEXT", "failed_nodes_json TEXT"):
                try:
                    self._conn.execute(f"ALTER TABLE run_meta ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass

            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS run_logs (
                    thread_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    ts TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    stream TEXT NOT NULL,
                    text TEXT NOT NULL,
                    PRIMARY KEY (thread_id, seq)
                )
                """
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_run_logs_tid_node ON run_logs(thread_id, node_id, seq)"
            )

    def _get_thread_lock(self, tid: str) -> threading.Lock:
        with self._meta_lock:
            lock = self._thread_locks.get(tid)
            if lock is None:
                lock = threading.Lock()
                self._thread_locks[tid] = lock
            return lock

    def _get_log_cond(self, tid: str) -> threading.Condition:
        with self._meta_lock:
            cond = self._log_conds.get(tid)
            if cond is None:
                cond = threading.Condition()
                self._log_conds[tid] = cond
            return cond

    def _notify_stream(self, tid: str) -> None:
        cond = self._get_log_cond(tid)
        with cond:
            cond.notify_all()

    # ---------------------------------------------------------------- NodeRuntimeHooks 实现
    def emit_log(self, thread_id: str, node_id: str, stream: str, line: str) -> None:
        ts = _now_iso()
        st = stream if stream in ("stdout", "stderr", "system") else "stdout"
        with self.checkpointer.lock, self._conn:
            cur_seq = self._log_seq_cache.get(thread_id)
            if cur_seq is None:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM run_logs WHERE thread_id = ?",
                    (thread_id,),
                ).fetchone()
                cur_seq = int(row[0]) if row else 0
            next_seq = cur_seq + 1
            self._log_seq_cache[thread_id] = next_seq
            self._conn.execute(
                "INSERT INTO run_logs (thread_id, seq, ts, node_id, stream, text) VALUES (?, ?, ?, ?, ?, ?)",
                (thread_id, next_seq, ts, node_id, st, line),
            )
        self._notify_stream(thread_id)

    def on_node_start(self, thread_id: str, node: NodeSpec, cwd: str, run_count: int) -> None:
        with self._meta_lock:
            bucket = self._live_nodes.setdefault(thread_id, {})
            prev = bucket.get(node.id) or {}
            bucket[node.id] = {
                **prev,
                "node_id": node.id,
                "label": node.label,
                "kind": node.kind,
                "status": "running",
                "runs": run_count,
                "worker_id": node.worker_id,
                "agent_id": node.agent_id if node.kind == "agent" else node.kind,
                "cwd": cwd,
                "started_at": _now_iso(),
            }
        self._notify_stream(thread_id)

    def on_node_end(self, thread_id: str, node: NodeSpec, result: NodeResultData) -> None:
        with self._meta_lock:
            self._live_nodes.setdefault(thread_id, {})[node.id] = result
        status_icon = "✓" if result.get("ok") else ("⏹" if result.get("status") == "cancelled" else "✗")
        self.emit_log(
            thread_id,
            node.id,
            "system",
            f"{status_icon} 节点 [{node.label}] 结束 (status={result.get('status')}, exit={result.get('exit_code')}, 耗时={result.get('duration_ms', 0)}ms)",
        )

    def execute_agent(
        self,
        *,
        thread_id: str,
        node: NodeSpec,
        prompt: str,
        cwd: str,
    ) -> tuple[str, CliExecResult]:
        if node.worker_id and node.worker_id != "local":
            raise RuntimeError(
                f"节点 [{node.label}] 指定了远端 Worker '{node.worker_id}'，跨主机 Worker 守护进程将在第二期启用；当前请先选择 'local'。"
            )
        profile, res = self.agent_registry.execute(
            node.agent_id,
            prompt,
            cwd=cwd,
            sandbox=node.sandbox,
            timeout_sec=node.timeout_sec,
            supervisor=self.supervisor,
            thread_id=thread_id,
            node_key=f"agent:{node.id}:{time.monotonic_ns()}",
            on_log=lambda stream, line: self.emit_log(thread_id, node.id, stream, line),
        )
        return profile.id, res

    def execute_shell(
        self,
        *,
        thread_id: str,
        node: NodeSpec,
        command: str,
        cwd: str,
    ) -> CliExecResult:
        if node.worker_id and node.worker_id != "local":
            raise RuntimeError(
                f"节点 [{node.label}] 指定了远端 Worker '{node.worker_id}'，跨主机 Worker 守护进程将在第二期启用；当前请先选择 'local'。"
            )
        target_cwd = os.path.abspath(os.path.expanduser(cwd or self.default_repo))
        if not os.path.isdir(target_cwd):
            raise ValueError(f"Shell 节点工作目录不存在: {target_cwd}")

        return run_streaming_subprocess(
            command,
            use_shell=True,
            cwd=target_cwd,
            timeout_sec=node.timeout_sec,
            supervisor=self.supervisor,
            thread_id=thread_id,
            node_key=f"shell:{node.id}:{time.monotonic_ns()}",
            on_log=lambda stream, line: self.emit_log(thread_id, node.id, stream, line),
        )

    # ---------------------------------------------------------------- 日志查询与 SSE 流
    def get_logs(
        self,
        thread_id: str,
        *,
        node_id: Optional[str] = None,
        after_seq: int = 0,
        limit: int = 1000,
    ) -> list[RunLogEntry]:
        safe_limit = max(1, min(limit, 5000))
        with self.checkpointer.lock:
            if node_id:
                rows = self._conn.execute(
                    """
                    SELECT seq, ts, node_id, stream, text
                    FROM run_logs
                    WHERE thread_id = ? AND node_id = ? AND seq > ?
                    ORDER BY seq ASC
                    LIMIT ?
                    """,
                    (thread_id, node_id, after_seq, safe_limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    """
                    SELECT seq, ts, node_id, stream, text
                    FROM run_logs
                    WHERE thread_id = ? AND seq > ?
                    ORDER BY seq ASC
                    LIMIT ?
                    """,
                    (thread_id, after_seq, safe_limit),
                ).fetchall()
        return [
            RunLogEntry(seq=int(r[0]), ts=str(r[1]), node_id=str(r[2]), stream=r[3], text=str(r[4]))
            for r in rows
        ]

    def stream_sse(self, thread_id: str, *, after_seq: int = 0) -> Generator[str, None, None]:
        """生成标准 SSE 事件流（实时推送 log 与 state 更新，直至运行暂停或结束）。"""
        cursor = after_seq
        cond = self._get_log_cond(thread_id)
        last_status = ""

        while True:
            entries = self.get_logs(thread_id, after_seq=cursor, limit=500)
            for entry in entries:
                cursor = entry.seq
                payload = json.dumps(entry.model_dump(mode="json"), ensure_ascii=False)
                yield f"event: log\ndata: {payload}\n\n"

            st = self.get_run_state(thread_id)
            cur_status = st.get("status", "unknown")
            if cur_status != last_status or entries:
                last_status = cur_status
                state_summary = json.dumps(
                    {
                        "thread_id": thread_id,
                        "status": cur_status,
                        "next": st.get("next", []),
                        "running_nodes": st.get("running_nodes", []),
                        "error": st.get("error"),
                    },
                    ensure_ascii=False,
                )
                yield f"event: status\ndata: {state_summary}\n\n"

            if cur_status in ("done", "waiting", "error", "cancelled", "interrupted", "unknown") and not self.is_active(thread_id):
                # 再排空一次尾部日志后优雅结束本轮流
                tail = self.get_logs(thread_id, after_seq=cursor, limit=500)
                for entry in tail:
                    payload = json.dumps(entry.model_dump(mode="json"), ensure_ascii=False)
                    yield f"event: log\ndata: {payload}\n\n"
                break

            with cond:
                cond.wait(timeout=1.5)
            yield ": heartbeat\n\n"

    # ---------------------------------------------------------------- 元数据与状态合成
    def is_active(self, tid: str) -> bool:
        with self._meta_lock:
            return tid in self._active_runs

    def _record_new_run(self, tid: str, workflow_id: str) -> None:
        with self.checkpointer.lock, self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO run_meta
                (thread_id, workflow_id, created_at, error, status_override, failed_nodes_json)
                VALUES (?, ?, ?, NULL, NULL, NULL)
                """,
                (tid, workflow_id, time.time()),
            )

    def _update_run_meta(
        self,
        tid: str,
        *,
        error: Optional[str],
        status_override: Optional[str],
        failed_nodes: Optional[dict[str, Any]] = None,
    ) -> None:
        failed_json = json.dumps(failed_nodes, ensure_ascii=False) if failed_nodes else None
        with self.checkpointer.lock, self._conn:
            self._conn.execute(
                """
                UPDATE run_meta
                SET error = ?, status_override = ?, failed_nodes_json = COALESCE(?, failed_nodes_json)
                WHERE thread_id = ?
                """,
                (error, status_override, failed_json, tid),
            )

    def _clear_run_failure(self, tid: str) -> None:
        with self.checkpointer.lock, self._conn:
            self._conn.execute(
                "UPDATE run_meta SET error = NULL, status_override = NULL, failed_nodes_json = NULL WHERE thread_id = ?",
                (tid,),
            )

    def _load_run_meta(self, tid: str) -> tuple[Optional[str], Optional[str], Optional[str], dict[str, Any], Optional[float]]:
        with self.checkpointer.lock:
            row = self._conn.execute(
                "SELECT workflow_id, error, status_override, failed_nodes_json, created_at FROM run_meta WHERE thread_id = ?",
                (tid,),
            ).fetchone()
        if not row:
            return None, None, None, {}, None
        failed_map: dict[str, Any] = {}
        if row[3]:
            try:
                failed_map = json.loads(row[3])
            except Exception:  # noqa: BLE001
                failed_map = {}
        return (
            str(row[0]) if row[0] else None,
            str(row[1]) if row[1] else None,
            str(row[2]) if row[2] else None,
            failed_map,
            float(row[4]) if row[4] is not None else None,
        )

    def _compile_for_run(self, workflow_id: str) -> Any:
        spec = self.workflow_registry.get_workflow(workflow_id)
        if spec is None:
            available = self.workflow_registry.list_workflows()
            if not available:
                raise KeyError(f"工作流不存在: {workflow_id}")
            spec = available[0]
        return self.workflow_registry.compile_graph(
            spec.id,
            hooks=self,
            checkpointer=self.checkpointer,
        )

    @staticmethod
    def _tracing_callbacks(tid: str) -> list[Any]:
        if not os.getenv("LANGFUSE_PUBLIC_KEY"):
            return []
        try:
            import hashlib

            from langfuse.langchain import CallbackHandler
            from langfuse.types import TraceContext

            return [CallbackHandler(trace_context=TraceContext(trace_id=hashlib.md5(tid.encode()).hexdigest()))]
        except Exception:  # noqa: BLE001
            return []

    def _cfg(self, tid: str, workflow_id: str, *, traced: bool = False) -> dict[str, Any]:
        cfg: dict[str, Any] = {
            "configurable": {"thread_id": tid},
            "metadata": {
                "thread_id": tid,
                "workflow_id": workflow_id,
                "langfuse_session_id": tid,
                "langfuse_trace_name": f"{workflow_id} {tid}",
            },
        }
        if traced:
            cbs = self._tracing_callbacks(tid)
            if cbs:
                cfg["callbacks"] = cbs
        return cfg

    def list_thread_ids(self, limit: int = 80) -> list[str]:
        with self.checkpointer.lock:
            rows = self._conn.execute(
                """
                SELECT thread_id, ts FROM (
                    SELECT thread_id, created_at AS ts FROM run_meta
                    UNION ALL
                    SELECT thread_id, CAST(MAX(rowid) AS REAL) AS ts FROM checkpoints GROUP BY thread_id
                )
                GROUP BY thread_id
                ORDER BY MAX(ts) DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [str(r[0]) for r in rows]

    def get_run_state(self, tid: str) -> dict[str, Any]:
        wf_id, run_error, status_override, persisted_failed_nodes, created_ts = self._load_run_meta(tid)
        effective_wf = wf_id or "iterative-coding"

        snap = None
        try:
            graph = self._compile_for_run(effective_wf)
            snap = graph.get_state(self._cfg(tid, effective_wf))
        except Exception:  # noqa: BLE001
            snap = None

        is_running_now = self.is_active(tid)

        if (snap is None or (not snap.values and not snap.next and not snap.created_at)) and wf_id is None:
            return {
                "thread_id": tid,
                "workflow_id": effective_wf,
                "status": "unknown",
                "next": [],
                "running_nodes": [],
                "values": {},
                "interrupts": [],
            }

        raw_values = dict(snap.values) if (snap and snap.values) else {}
        merged_nodes: dict[str, Any] = dict(raw_values.get("nodes") or {})
        for fnid, fdata in persisted_failed_nodes.items():
            merged_nodes[fnid] = {**(merged_nodes.get(fnid) or {}), **fdata}

        running_nodes: list[str] = []
        with self._meta_lock:
            live_map = dict(self._live_nodes.get(tid) or {})
        for lnid, ldata in live_map.items():
            if is_running_now or ldata.get("status") in ("failed", "cancelled"):
                merged_nodes[lnid] = {**(merged_nodes.get(lnid) or {}), **ldata}
            if is_running_now and ldata.get("status") == "running":
                running_nodes.append(lnid)

        raw_values["nodes"] = merged_nodes

        interrupts = [itr.value for itr in snap.interrupts] if (snap and snap.interrupts) else []
        next_nodes = list(snap.next) if (snap and snap.next) else []

        if is_running_now:
            status: RunStatus = "running"
        elif status_override == "cancelled":
            status = "cancelled"
        elif run_error:
            status = "error"
        elif interrupts:
            status = "waiting"
        elif next_nodes:
            status = "interrupted"
        elif snap and (snap.values or snap.created_at):
            status = "done"
        else:
            status = "running" if created_ts and (time.time() - created_ts < 5.0) else "unknown"

        created_iso = snap.created_at if (snap and snap.created_at) else (
            dt.datetime.fromtimestamp(created_ts, tz=dt.timezone.utc).isoformat().replace("+00:00", "Z")
            if created_ts
            else None
        )

        online_agents = [a.id for a in self.agent_registry.list_agents() if a.available]

        return {
            "thread_id": tid,
            "workflow_id": effective_wf,
            "status": status,
            "next": next_nodes,
            "running_nodes": running_nodes,
            "values": raw_values,
            "interrupts": interrupts,
            "created_at": created_iso,
            "error": run_error,
            "source": ",".join(online_agents) if online_agents else "cli",
        }

    # ---------------------------------------------------------------- 启动 / 取消 / 重试 / 审批恢复
    def _background_invoke(self, tid: str, workflow_id: str, payload: Any) -> None:
        lock = self._get_thread_lock(tid)
        with self._meta_lock:
            self._active_runs.add(tid)
        self._notify_stream(tid)

        try:
            with lock:
                graph = self._compile_for_run(workflow_id)
                graph.invoke(payload, self._cfg(tid, workflow_id, traced=True))
            if self.supervisor.is_cancelled(tid):
                self._update_run_meta(tid, error="运行已被人工强杀中止", status_override="cancelled")
            else:
                self._clear_run_failure(tid)
        except NodeExecutionError as exc:
            is_cancel = self.supervisor.is_cancelled(tid) or (
                exc.result is not None and exc.result.get("status") == "cancelled"
            )
            failed_patch = {exc.node_id: exc.result} if exc.result else None
            self._update_run_meta(
                tid,
                error=str(exc),
                status_override="cancelled" if is_cancel else "error",
                failed_nodes=failed_patch,
            )
            self.emit_log(tid, exc.node_id, "stderr" if not is_cancel else "system", str(exc))
        except Exception as exc:  # noqa: BLE001
            err_text = f"{exc}\n{traceback.format_exc(limit=4)}"
            is_cancel = self.supervisor.is_cancelled(tid)
            self._update_run_meta(
                tid,
                error="运行已被人工强杀中止" if is_cancel else err_text,
                status_override="cancelled" if is_cancel else "error",
            )
            self.emit_log(tid, "__system__", "stderr", f"运行异常中止: {exc}")
            print(f"[error] thread={tid} workflow={workflow_id}\n{err_text}", flush=True)
        finally:
            with self._meta_lock:
                self._active_runs.discard(tid)
                # 清理仍处于 running 的残留标记
                live_bucket = self._live_nodes.get(tid) or {}
                for ldata in live_bucket.values():
                    if ldata.get("status") == "running":
                        ldata["status"] = "cancelled" if self.supervisor.is_cancelled(tid) else "failed"
            self._notify_stream(tid)
            self._flush_traces()

    @staticmethod
    def _flush_traces() -> None:
        if not os.getenv("LANGFUSE_PUBLIC_KEY"):
            return
        try:
            from langfuse import get_client

            get_client().flush()
        except Exception:  # noqa: BLE001
            pass

    def start_run(
        self,
        *,
        workflow_id: str,
        task: str = "",
        inputs: Optional[dict[str, Any]] = None,
        repo: Optional[str] = None,
        test_cmd: Optional[str] = None,
        max_attempts: int = 2,
    ) -> dict[str, Any]:
        wf_spec = self.workflow_registry.get_workflow(workflow_id)
        if wf_spec is None:
            raise KeyError(f"工作流不存在: {workflow_id}")

        merged_inputs: dict[str, Any] = {}
        for field_spec in wf_spec.inputs:
            if field_spec.default is not None:
                merged_inputs[field_spec.key] = field_spec.default

        if inputs:
            merged_inputs.update({k: v for k, v in inputs.items() if v is not None})
        if task and task.strip():
            merged_inputs["task"] = task.strip()
        if repo and repo.strip():
            merged_inputs["repo"] = repo.strip()
        if test_cmd and test_cmd.strip():
            merged_inputs["test_cmd"] = test_cmd.strip()

        clean_task = str(merged_inputs.get("task") or "").strip()
        # 校验必填项
        for field_spec in wf_spec.inputs:
            if field_spec.required:
                val = merged_inputs.get(field_spec.key)
                if val is None or (isinstance(val, str) and not val.strip()):
                    raise ValueError(f"必填参数 '{field_spec.label}' ({field_spec.key}) 不能为空")

        raw_repo = str(merged_inputs.get("repo") or self.default_repo).strip()
        if "\x00" in raw_repo:
            raise ValueError("工作目录路径包含非法空字符")
        target_repo = os.path.abspath(os.path.expanduser(raw_repo))

        allowed_roots_env = os.environ.get("CONSOLE_ALLOWED_REPO_ROOTS", "").strip()
        if allowed_roots_env:
            allowed_roots = [
                os.path.abspath(os.path.expanduser(p.strip()))
                for p in allowed_roots_env.split(os.pathsep)
                if p.strip()
            ]
            if allowed_roots and not any(
                target_repo == root or target_repo.startswith(root + os.sep)
                for root in allowed_roots
            ):
                raise ValueError(f"repo 路径不在允许的工作区目录范围内: {target_repo}")
        if not os.path.isdir(target_repo):
            raise ValueError(f"工作目录不存在或不是文件夹: {target_repo}")

        merged_inputs["repo"] = target_repo
        merged_inputs["max_attempts"] = max_attempts

        warnings: list[str] = []
        if not os.path.isdir(os.path.join(target_repo, ".git")):
            warnings.append(f"{target_repo} 不是 Git 仓库：节点将无法抓取 Git Diff")

        tid = f"run-{int(time.time() * 1000)}"
        self.supervisor.clear_cancel(tid)
        self._record_new_run(tid, wf_spec.id)
        self.emit_log(
            tid,
            "__system__",
            "system",
            f"🚀 创建工作流运行 [{wf_spec.name}] ({wf_spec.id})，默认目录: {target_repo}",
        )

        initial_state: dict[str, Any] = {
            "inputs": merged_inputs,
            "task": clean_task,
            "repo": target_repo,
            "nodes": {},
            "feedback_by_node": {},
            "auto_runs": {},
            "node_overrides": {},
            "events": [],
        }
        if test_cmd:
            initial_state["test_cmd"] = test_cmd

        threading.Thread(
            target=self._background_invoke,
            args=(tid, wf_spec.id, initial_state),
            daemon=True,
        ).start()
        return {"thread_id": tid, "workflow_id": wf_spec.id, "warnings": warnings}

    def cancel_run(self, tid: str) -> dict[str, Any]:
        """强杀正在执行的子进程组，或取消正停留在人工卡点的运行。"""
        st = self.get_run_state(tid)
        if st["status"] == "unknown":
            raise KeyError(f"未找到运行记录: {tid}")
        if st["status"] == "done":
            raise ValueError("该运行已执行完毕，无需取消")

        killed = self.supervisor.cancel_thread(tid)
        self._update_run_meta(tid, error="运行已被人工强杀中止", status_override="cancelled")
        self.emit_log(tid, "__system__", "system", f"⏹ 已发送强杀中止指令（终止活跃进程组: {killed} 个）")
        return {"ok": True, "thread_id": tid, "killed_processes": killed}

    def retry_run(
        self,
        tid: str,
        *,
        node_id: Optional[str] = None,
        prompt_template: Optional[str] = None,
        command: Optional[str] = None,
        agent_id: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> dict[str, Any]:
        """从失败、被强杀中止或服务重启中断的断点节点原地重试（支持临时覆盖节点参数）。"""
        st = self.get_run_state(tid)
        if st["status"] == "unknown":
            raise KeyError(f"未找到运行记录: {tid}")
        if st["status"] == "running":
            raise ValueError("该运行正在执行中，请先强杀中止后再重试")
        if st["status"] == "done" and not node_id:
            raise ValueError("该运行已全部完成；若需重跑特定节点请显式传入 node_id")

        wf_id = st["workflow_id"]
        graph = self._compile_for_run(wf_id)
        cfg = self._cfg(tid, wf_id)
        snap = graph.get_state(cfg)
        next_nodes = list(snap.next) if (snap and snap.next) else []

        target_nid = node_id or (next_nodes[0] if next_nodes else None)
        if not target_nid:
            spec = self.workflow_registry.get_workflow(wf_id)
            target_nid = spec.entry_point if spec else None
        if not target_nid:
            raise ValueError("无法确定要重试的目标节点 ID")

        overrides_patch: dict[str, Any] = {}
        for k, v in (
            ("prompt_template", prompt_template),
            ("command", command),
            ("agent_id", agent_id),
            ("cwd", cwd),
        ):
            if v is not None and str(v).strip():
                overrides_patch[k] = str(v).strip()

        state_patch: dict[str, Any] = {"auto_runs": {"__reset__": True}}
        if overrides_patch:
            state_patch["node_overrides"] = {target_nid: overrides_patch}

        graph.update_state(cfg, state_patch)

        self.supervisor.clear_cancel(tid)
        self._clear_run_failure(tid)
        with self._meta_lock:
            live_bucket = self._live_nodes.get(tid) or {}
            live_bucket.pop(target_nid, None)

        self.emit_log(
            tid,
            target_nid,
            "system",
            f"↻ 人工触发节点 [{target_nid}] 原地重试" + (f" (覆盖参数: {list(overrides_patch.keys())})" if overrides_patch else ""),
        )

        # 若当前 checkpoint 的 next 已包含目标节点，直接 invoke(None) 即可接着断点重跑；否则使用 Command(goto=target_nid) 跳转
        invoke_payload: Any = None if (next_nodes and (node_id is None or node_id in next_nodes)) else Command(goto=target_nid)

        threading.Thread(
            target=self._background_invoke,
            args=(tid, wf_id, invoke_payload),
            daemon=True,
        ).start()
        return {"ok": True, "thread_id": tid, "retried_node_id": target_nid}

    def resume_run(
        self,
        tid: str,
        *,
        action: Literal["approve", "revise", "abort"],
        feedback: str = "",
        edited_outputs: Optional[dict[str, str]] = None,
        target_node_id: Optional[str] = None,
    ) -> dict[str, Any]:
        st = self.get_run_state(tid)
        if st["status"] != "waiting":
            raise ValueError(f"当前运行不在等待人工卡点状态 (status={st['status']})")

        wf_id = st["workflow_id"]
        self.supervisor.clear_cancel(tid)
        self._clear_run_failure(tid)

        resume_payload = {
            "action": action,
            "feedback": feedback,
            "edited_outputs": edited_outputs or {},
            "target_node_id": target_node_id,
        }
        threading.Thread(
            target=self._background_invoke,
            args=(tid, wf_id, Command(resume=resume_payload)),
            daemon=True,
        ).start()
        return {"ok": True, "thread_id": tid}
