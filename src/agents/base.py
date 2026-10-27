"""Agent 抽象：工作流只依赖这个接口，不关心 agent 跑在哪。

后续按 README 的规划实现三种：
- LocalAgent : 本机子进程跑 codex / claude code
- SSHAgent   : 远端机器上跑
- HTTPAgent  : 调用远端服务（如自建的 agent gateway）

现在先用 StubAgent 把图跑通，节点逻辑和真实 agent 一致。
"""

from typing import Protocol, TypedDict


class AgentResult(TypedDict):
    ok: bool
    output: str


class AgentTarget(Protocol):
    """所有 agent 实现的统一接口。"""

    name: str

    def run(self, prompt: str) -> AgentResult:
        """同步执行一次 agent，返回是否成功 + 文本输出。"""
        ...


class StubAgent:
    """离线假 agent：不调用任何外部命令，用于跑通图和写测试。

    行为由 prompt 前缀决定（PLAN/IMPLEMENT/TEST/REVIEW），
    其中 TEST 在第一次尝试时故意失败，用来演示 fix loop。
    """

    name = "stub"

    def run(self, prompt: str) -> AgentResult:
        if prompt.startswith("PLAN:"):
            return {
                "ok": True,
                "output": "1. 读代码\n2. 改实现\n3. 跑测试\n4. 提 MR",
            }
        if prompt.startswith("IMPLEMENT:"):
            attempt = prompt.rsplit("第", 1)[-1].split("次")[0].strip()
            return {"ok": True, "output": f"diff --git a/x b/x (stub 第{attempt}次实现)"}
        if prompt.startswith("TEST:"):
            fail = "第 1 次" in prompt
            return {
                "ok": not fail,
                "output": "FAILED tests/test_x.py::test_add - assert 3 == 4"
                if fail
                else "PASSED 1 passed",
            }
        if prompt.startswith("REVIEW:"):
            return {"ok": True, "output": "LGTM（stub 审查通过）"}
        return {"ok": False, "output": f"[stub] 未知指令: {prompt[:40]}"}
