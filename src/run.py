"""交互式跑 coding 工作流（带 SQLite 持久化 + 人审中断）。

用法：
    source env.sh
    python -m src.run "给 verify_graph.py 加 --verbose 参数"

    人审时输入：approve / revise / abort
    （直接回车 = approve；revise 时可在下一行写反馈）

持久化：./checkpoints.sqlite，同一 thread 可回放：graph.get_state_history(config)
"""

import hashlib
import json
import os
import sqlite3
import sys
import time

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from src.workflows.coding import build

DB_PATH = "checkpoints.sqlite"


def _tracing_callbacks(trace_id: str) -> list:
    """有 LANGFUSE_PUBLIC_KEY 就上报 trace，否则静默跳过。

    显式传 trace_context：同一次工作流（含多次 interrupt/resume）合并成一条 trace。
    """
    if not os.getenv("LANGFUSE_PUBLIC_KEY"):
        return []
    try:
        from langfuse.langchain import CallbackHandler
        from langfuse.types import TraceContext
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] Langfuse 不可用，跳过 trace: {exc}")
        return []
    return [CallbackHandler(trace_context=TraceContext(trace_id=trace_id))]


def _flush_traces() -> None:
    try:
        from langfuse import get_client

        get_client().flush()
    except Exception:  # noqa: BLE001
        pass


def _read_decision() -> dict:
    """读人工决定；EOF 时默认 approve（方便管道/CI 跑）。"""
    try:
        raw = input(">>> (approve/revise/abort) ").strip()
    except EOFError:
        return {"action": "approve", "feedback": ""}
    action = raw or "approve"
    feedback = ""
    if action == "revise":
        try:
            feedback = input("    反馈: ").strip()
        except EOFError:
            feedback = ""
    return {"action": action, "feedback": feedback}


def main(argv: list[str]) -> None:
    task = " ".join(argv) or "给 verify_graph.py 增加 --verbose 参数"
    thread_id = f"coding-{int(time.time())}"

    # LangGraph 在工作线程写 checkpoint，sqlite 必须关同线程检查
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    graph = build().compile(checkpointer=SqliteSaver(conn))
    trace_id = hashlib.md5(thread_id.encode()).hexdigest()
    callbacks = _tracing_callbacks(trace_id)
    # metadata.thread_id 让 Langfuse 把同一 thread 的多次 resume 合并成一条 trace
    config = {
        "configurable": {"thread_id": thread_id},
        "metadata": {
            "thread_id": thread_id,
            # 下面两个 key 是 Langfuse 约定的元数据，UI 里能直接看到
            "langfuse_trace_name": f"coding: {task[:60]}",
            "langfuse_session_id": thread_id,
        },
        "callbacks": callbacks,
    }
    if callbacks:
        print(f"[trace]  上报到 {os.getenv('LANGFUSE_HOST')}（project=agent-workflows）")

    print(f"[thread] {thread_id}")
    print(f"[task]   {task}\n")

    graph.invoke(
        {"task": task, "repo": ".", "max_attempts": 2, "attempt": 0, "events": []},
        config,
    )

    while True:
        snap = graph.get_state(config)
        if not snap.interrupts:
            break
        for itr in snap.interrupts:
            print("\n=== 需要人工决策 ===")
            print(json.dumps(itr.value, ensure_ascii=False, indent=2))
        decision = _read_decision()
        graph.invoke(Command(resume=decision), config)

    snap = graph.get_state(config)
    values = snap.values
    print("\n=== events ===")
    for e in values.get("events", []):
        print(" -", e)
    print(f"\n=== result ===\n{values.get('result')}")
    print(f"\n[checkpoint] {DB_PATH} / thread={thread_id}，快照数={len(list(graph.get_state_history(config)))}")
    _flush_traces()


if __name__ == "__main__":
    main(sys.argv[1:])
