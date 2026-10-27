"""codex CLI 的 AgentTarget 实现。

使用运行环境中已配置的 codex CLI 认证和模型服务。

节点通过 prompt 前缀告诉 agent 该用什么权限：
    PLAN:      -> read-only       （只读分析、出计划）
    IMPLEMENT: -> workspace-write （允许改文件）
    REVIEW:    -> read-only
    TEST:      -> read-only
"""

import json
import os
import subprocess
import tempfile

from src.agents.base import AgentResult

# 前缀 -> 沙箱权限；默认只读，避免误改
_SANDBOX = {
    "PLAN": "read-only",
    "REVIEW": "read-only",
    "TEST": "read-only",
    "IMPLEMENT": "workspace-write",
}


class CodexAgent:
    """在本机以子进程方式跑 codex exec。"""

    name = "codex"

    def __init__(
        self,
        repo_root: str = ".",
        *,
        codex_bin: str = "codex",
        model: str | None = None,
        timeout: int = 1800,
        extra_args: list[str] | None = None,
    ) -> None:
        self.repo_root = os.path.abspath(repo_root)
        self.codex_bin = codex_bin
        self.model = model
        self.timeout = timeout
        self.extra_args = extra_args or []

    def run(self, prompt: str) -> AgentResult:
        kind = prompt.split(":", 1)[0].strip().upper()
        body = prompt.split(":", 1)[1].strip() if ":" in prompt else prompt
        sandbox = _SANDBOX.get(kind, "read-only")

        with tempfile.NamedTemporaryFile(
            "r", suffix=".txt", delete=False, encoding="utf-8"
        ) as fh:
            out_path = fh.name

        cmd = [
            self.codex_bin, "exec",
            "--cd", self.repo_root,
            "-s", sandbox,
            "--skip-git-repo-check",
            # 非交互模式：不弹审批。注意 --approve-for-me 与 -s 互斥，不能一起用
            "-c", "approval_policy=\"never\"",
            "-o", out_path,              # 只取最后一条消息
        ]
        if self.model:
            cmd += ["-m", self.model]
        cmd += self.extra_args
        cmd.append(body)

        try:
            proc = subprocess.run(
                cmd,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env={**os.environ, "NO_COLOR": "1"},
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "output": f"[codex] 超时 {self.timeout}s"}
        finally:
            pass

        last = ""
        try:
            with open(out_path, encoding="utf-8") as fh:
                last = fh.read().strip()
        except OSError:
            pass
        finally:
            try:
                os.unlink(out_path)
            except OSError:
                pass

        output = last or (proc.stdout or "").strip()[-4000:] or (proc.stderr or "").strip()[-2000:]
        ok = proc.returncode == 0
        if not ok and not output:
            output = f"[codex] exit={proc.returncode}\n{(proc.stderr or '')[-2000:]}"
        return {"ok": ok, "output": output}


def _main() -> None:  # 手动冒烟：python -m src.agents.codex "PLAN: 看下这个仓库"
    import sys

    agent = CodexAgent(os.environ.get("CODEX_REPO", "."))
    print(json.dumps(agent.run(" ".join(sys.argv[1:]) or "PLAN: 介绍这个仓库"), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _main()
