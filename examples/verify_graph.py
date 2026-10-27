"""最小验证图：plan -> 人审 gate -> done，带 SQLite checkpoint。

用途：确认本机 LangGraph 环境的 state / checkpointer / interrupt-resume 全链路可用。
运行：source env.sh && python examples/verify_graph.py
"""

import operator
import sqlite3
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt


class State(TypedDict):
    task: str
    plan: str
    approved: str
    steps: Annotated[list[str], operator.add]


def plan_node(state: State) -> dict:
    return {"plan": f"plan for: {state['task']}", "steps": ["plan"]}


def gate_node(state: State) -> dict:
    answer = interrupt({"ask": "approve this plan?", "plan": state["plan"]})
    return {"approved": str(answer), "steps": ["gate"]}


def done_node(state: State) -> dict:
    return {"steps": ["done"]}


def build() -> StateGraph:
    b = StateGraph(State)
    b.add_node("plan", plan_node)
    b.add_node("gate", gate_node)
    b.add_node("done", done_node)
    b.add_edge(START, "plan")
    b.add_edge("plan", "gate")
    b.add_edge("gate", "done")
    b.add_edge("done", END)
    return b


def main() -> None:
    # LangGraph 会在工作线程里写 checkpoint，必须关掉 sqlite 的同线程检查
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    graph = build().compile(checkpointer=SqliteSaver(conn))
    config = {"configurable": {"thread_id": "verify-1"}}

    graph.invoke({"task": "verify env", "steps": []}, config)
    pending = graph.get_state(config)
    assert pending.next == ("gate",), f"expected pause at gate, got {pending.next}"
    print("[ok] 1/3 图执行到 gate 并暂停，checkpoint 已落盘")

    final = graph.invoke(Command(resume="yes"), config)
    assert final["approved"] == "yes", final
    assert final["steps"] == ["plan", "gate", "done"], final
    print(f"[ok] 2/3 interrupt 恢复成功，trace = {final['steps']}")

    history = list(graph.get_state_history(config))
    print(f"[ok] 3/3 checkpointer 记录了 {len(history)} 个快照，可回放/时间旅行")
    print(f"[ok] 最终 state: {final}")


if __name__ == "__main__":
    main()


# 供 langgraph dev 使用（未编译 builder，由 server 自行 compile）
graph = build()
