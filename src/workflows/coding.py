"""coding 工作流：plan -> 人审 -> implement -> test -> (fix loop) -> review -> 人审 -> 完成

这就是 README「下一步」里那条链路的最小可跑版本：
- 每个节点是一个普通函数：读 state，返回要合并进 state 的增量 dict
- 两处人审用 interrupt()：图会停住并持久化，等 Command(resume=...) 再继续
- test 失败回到 implement 形成修复循环，超过 max_attempts 转人工 (give_up)
- 真实 agent 通过 AgentTarget 注入；现在默认 StubAgent，可离线跑

运行：
    source env.sh
    python -m src.run "给 verify_graph.py 加 --verbose"
或交给 langgraph dev（见 langgraph.json）：graph 是未编译的 builder。
"""

import subprocess

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from src.agents.base import AgentResult, AgentTarget, StubAgent
from src.state import CodingState

# 换真实 agent 只需改这里（或注入到 build(agent=...)）
DEFAULT_AGENT: AgentTarget = StubAgent()


def _git_diff(repo: str, limit: int = 20000) -> str:
    """抓当前工作区改动，作为人审的 diff 预览。"""
    try:
        out = subprocess.run(
            ["git", "-C", repo, "diff"], capture_output=True, text=True, timeout=60
        )
        return out.stdout[:limit].strip()
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 节点
def plan_node(state: CodingState) -> dict:
    """让 agent 出实现计划。"""
    res: AgentResult = DEFAULT_AGENT.run(
        f"PLAN: 为以下任务给出分步实现计划\n任务：{state['task']}\n"
        f"人工反馈：{state.get('feedback') or '无'}"
    )
    return {"plan": res["output"], "events": ["plan"]}


def plan_gate_node(state: CodingState) -> dict:
    """人审 gate 1：计划必须人工批准才继续。"""
    decision = interrupt(
        {
            "stage": "plan_approval",
            "task": state["task"],
            "plan": state["plan"],
            "options": ["approve", "revise", "abort"],
        }
    )
    # resume 可以传 dict，也可以直接传字符串（方便命令行）
    if isinstance(decision, dict):
        action = decision.get("action", "approve")
        feedback = decision.get("feedback", "")
    else:
        action, feedback = str(decision), ""
    return {
        "action": action,
        "feedback": feedback,
        "approved": action == "approve",
        "events": [f"plan_gate:{action}"],
    }


def implement_node(state: CodingState) -> dict:
    """按计划改代码；每次进来 attempt +1。"""
    attempt = state.get("attempt", 0) + 1
    res = DEFAULT_AGENT.run(
        f"IMPLEMENT: 按计划实现改动（第 {attempt} 次）\n"
        f"计划：{state['plan']}\n"
        f"上次测试输出：{state.get('test_output') or '无'}"
    )
    # 真实 agent 直接改工作区，patch 用 git diff 抓；stub 则用它的文本输出
    patch = _git_diff(state.get("repo", ".")) or res["output"]
    return {
        "attempt": attempt,
        "patch": patch,
        "events": [f"implement#{attempt}"],
    }


def test_node(state: CodingState) -> dict:
    """跑测试，结果决定是继续修还是进入 review。

    配了 test_cmd 就跑真实命令；否则退回让 agent 跑（stub 模式）。
    """
    cmd = state.get("test_cmd")
    if cmd:
        try:
            out = subprocess.run(
                cmd, shell=True, cwd=state.get("repo", "."),
                capture_output=True, text=True, timeout=1800,
            )
            ok = out.returncode == 0
            text = (out.stdout + out.stderr)[-6000:]
        except Exception as exc:  # noqa: BLE001
            ok, text = False, f"测试命令执行失败: {exc}"
        return {
            "passed": ok,
            "test_output": text,
            "events": [f"test#{state['attempt']}:{'pass' if ok else 'fail'}"],
        }

    res = DEFAULT_AGENT.run(f"TEST: 运行测试（第 {state['attempt']} 次）")
    return {
        "passed": res["ok"],
        "test_output": res["output"],
        "events": [f"test#{state['attempt']}:{'pass' if res['ok'] else 'fail'}"],
    }


def review_node(state: CodingState) -> dict:
    """代码审查。"""
    res = DEFAULT_AGENT.run(f"REVIEW: 审查以下改动\n{state['patch']}")
    return {"review": res["output"], "events": [f"review:{'ok' if res['ok'] else 'ng'}"]}


def final_gate_node(state: CodingState) -> dict:
    """人审 gate 2：合并/提 MR 前最后确认。"""
    decision = interrupt(
        {
            "stage": "final_approval",
            "patch": state["patch"],
            "test_output": state["test_output"],
            "review": state["review"],
            "options": ["approve", "revise", "abort"],
        }
    )
    if isinstance(decision, dict):
        action = decision.get("action", "approve")
        feedback = decision.get("feedback", "")
    else:
        action, feedback = str(decision), ""
    return {
        "action": action,
        "feedback": feedback,
        "approved": action == "approve",
        "events": [f"final_gate:{action}"],
    }


def finish_node(state: CodingState) -> dict:
    if state.get("action") == "abort":
        result = "已中止（人工 abort）"
    else:
        result = (
            f"完成：{state['task']} | 尝试 {state['attempt']} 次 | "
            f"测试 {state['test_output']} | review {state['review']}"
        )
    return {"result": result, "events": ["finish"]}


def give_up_node(state: CodingState) -> dict:
    return {
        "result": f"测试 {state['max_attempts']} 次仍未通过，转人工：{state['test_output']}",
        "events": ["give_up"],
    }


# ---------------------------------------------------------------- 路由
def route_after_plan_gate(state: CodingState) -> str:
    if state["action"] == "approve":
        return "implement"
    if state["action"] == "abort":
        return "finish"
    return "plan"  # revise


def route_after_test(state: CodingState) -> str:
    if state["passed"]:
        return "review"
    if state["attempt"] >= state.get("max_attempts", 2):
        return "give_up"
    return "implement"


def route_after_final_gate(state: CodingState) -> str:
    if state["action"] == "approve":
        return "finish"
    if state["action"] == "abort":
        return "finish"
    return "implement"  # revise：改完重新走 test/review


# ---------------------------------------------------------------- 组图
def build(agent: AgentTarget | None = None) -> StateGraph:
    global DEFAULT_AGENT
    if agent is not None:
        DEFAULT_AGENT = agent

    b = StateGraph(CodingState)
    b.add_node("plan", plan_node)
    b.add_node("plan_gate", plan_gate_node)
    b.add_node("implement", implement_node)
    b.add_node("test", test_node)
    b.add_node("review", review_node)
    b.add_node("final_gate", final_gate_node)
    b.add_node("finish", finish_node)
    b.add_node("give_up", give_up_node)

    b.add_edge(START, "plan")
    b.add_edge("plan", "plan_gate")
    b.add_conditional_edges(
        "plan_gate",
        route_after_plan_gate,
        {"implement": "implement", "plan": "plan", "finish": "finish"},
    )
    b.add_edge("implement", "test")
    b.add_conditional_edges(
        "test",
        route_after_test,
        {"review": "review", "implement": "implement", "give_up": "give_up"},
    )
    b.add_edge("review", "final_gate")
    b.add_conditional_edges(
        "final_gate",
        route_after_final_gate,
        {"finish": "finish", "implement": "implement"},
    )
    b.add_edge("finish", END)
    b.add_edge("give_up", END)
    return b


# 供 langgraph dev 使用：暴露未编译的 builder，由 server 注入 checkpointer
graph = build()
