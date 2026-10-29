"""多 CLI Agent 注册中心、本机二进制探测与流式子进程管理器。

核心职责：
1. 默认彻底弃用假 StubAgent；启动时自动扫描本机 PATH（含 /opt/homebrew/bin、/usr/local/bin、~/.local/bin）
   探测已安装的真实 CLI Agent（Codex / Claude Code / Kimi / Gemini）。
2. 支持用户通过 Web 操作台或 data/agents.json 新增、编辑、删除自定义 CLI 命令模板（custom 驱动）。
3. 使用 subprocess.Popen(..., start_new_session=True) 实时按行流式采集 stdout/stderr，
   并在 ProcessSupervisor 中按 (thread_id, node_id) 登记进程组，支持任意时刻一键强杀中止整棵子进程树。
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from typing import Callable, Literal, Optional

from pydantic import BaseModel, Field

AgentDriverKind = Literal["codex", "claude", "kimi", "gemini", "custom"]
SandboxMode = Literal["read-only", "workspace-write"]
LogCallback = Callable[[str, str], None]  # (stream: "stdout"|"stderr"|"system", line: str) -> None


def _augmented_path() -> str:
    """补充常见 CLI 安装路径，避免 systemd 或非登录 shell 找不到二进制。"""
    home = os.path.expanduser("~")
    extra = [
        "/opt/homebrew/bin",
        "/usr/local/bin",
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".npm-global", "bin"),
        os.path.join(home, ".cargo", "bin"),
    ]
    current = os.environ.get("PATH", "")
    parts = [p for p in current.split(os.pathsep) if p]
    for p in extra:
        if p not in parts and os.path.isdir(p):
            parts.append(p)
    return os.pathsep.join(parts)


def resolve_binary(binary: str) -> Optional[str]:
    if not binary:
        return None
    expanded = os.path.expanduser(binary)
    if os.path.sep in expanded:
        return os.path.abspath(expanded) if os.path.isfile(expanded) and os.access(expanded, os.X_OK) else None
    return shutil.which(expanded, path=_augmented_path())


class AgentProfileSpec(BaseModel):
    """可持久化的 CLI Agent 配置定义。"""

    id: str = Field(..., pattern=r"^[a-zA-Z0-9_-]+$", description="Agent 唯一标识，如 codex / claude / kimi / my-cli")
    name: str = Field(..., description="显示名称，如 Codex CLI / Claude Code / Kimi CLI")
    driver: AgentDriverKind = Field(
        "custom",
        description="驱动类型：codex | claude | kimi | gemini | custom（自定义命令模板）",
    )
    binary: str = Field(..., description="可执行文件名称或绝对路径，如 codex / claude / kimi")
    description: str = Field("", description="Agent 用途与特性说明")
    model: Optional[str] = Field(None, description="可选模型覆盖参数（传给 CLI 的 -m / --model）")
    args_template: list[str] = Field(
        default_factory=list,
        description=(
            "自定义参数列表模板（driver='custom' 时必填；内置驱动留空则使用官方非交互预设）。"
            "支持占位符：{prompt}、{prompt_file}、{cwd}、{sandbox}、{output_file}、{model}"
        ),
    )
    command_template: Optional[str] = Field(
        None,
        description="可选单行 Shell 命令模板（仅 driver='custom' 且未填 args_template 时使用）",
    )
    timeout_sec: int = Field(1800, ge=10, le=14400, description="单次执行超时秒数")
    env: dict[str, str] = Field(default_factory=dict, description="附加环境变量（键值对）")
    builtin: bool = Field(False, description="是否为系统内置预设")


class AgentProfileOut(AgentProfileSpec):
    """带实时本机可用性探测状态的 Agent 信息。"""

    available: bool = Field(False, description="当前主机是否已安装并可调用该 CLI 二进制")
    resolved_path: Optional[str] = Field(None, description="探测到的二进制绝对路径")


class CliExecResult(BaseModel):
    ok: bool
    exit_code: int
    output: str
    stdout: str
    cancelled: bool = False
    duration_ms: int = 0
    command_display: str = ""


def builtin_agent_profiles() -> list[AgentProfileSpec]:
    """内置四大主流研发 CLI 预设（均直接复用机器本地已登录的 CLI 会话，零 API Key 配置）。"""
    return [
        AgentProfileSpec(
            id="codex",
            name="Codex CLI",
            driver="codex",
            binary=os.environ.get("CONSOLE_CODEX_BIN", "codex"),
            description="OpenAI Codex 本地编码 CLI，支持 read-only 与 workspace-write 沙箱隔离",
            builtin=True,
        ),
        AgentProfileSpec(
            id="claude",
            name="Claude Code",
            driver="claude",
            binary=os.environ.get("CONSOLE_CLAUDE_BIN", "claude"),
            description="Anthropic Claude Code CLI，适合复杂架构规划、重构与代码审查",
            builtin=True,
        ),
        AgentProfileSpec(
            id="kimi",
            name="Kimi CLI",
            driver="kimi",
            binary=os.environ.get("CONSOLE_KIMI_BIN", "kimi"),
            description="Kimi 命令行研发 Agent，适合前端工程与多文件协同实现",
            builtin=True,
        ),
        AgentProfileSpec(
            id="gemini",
            name="Gemini CLI",
            driver="gemini",
            binary=os.environ.get("CONSOLE_GEMINI_BIN", "gemini"),
            description="Google Gemini CLI 本地非交互研发助手",
            builtin=True,
        ),
    ]


class ProcessSupervisor:
    """跟踪所有正在运行的节点子进程组，支持随时按 thread_id 或 (thread_id, node_id) 强杀中止。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # thread_id -> { node_key -> Popen }
        self._procs: dict[str, dict[str, subprocess.Popen[str]]] = {}
        self._cancelled_threads: set[str] = set()

    def clear_cancel(self, thread_id: str) -> None:
        with self._lock:
            self._cancelled_threads.discard(thread_id)

    def is_cancelled(self, thread_id: str) -> bool:
        with self._lock:
            return thread_id in self._cancelled_threads

    def register(self, thread_id: str, node_key: str, proc: subprocess.Popen[str]) -> bool:
        """登记子进程；若该 thread_id 已被标记取消，则立即返回 False 并终止进程。"""
        with self._lock:
            if thread_id in self._cancelled_threads:
                self._terminate_proc_unlocked(proc)
                return False
            self._procs.setdefault(thread_id, {})[node_key] = proc
            return True

    def unregister(self, thread_id: str, node_key: str) -> None:
        with self._lock:
            bucket = self._procs.get(thread_id)
            if bucket:
                bucket.pop(node_key, None)
                if not bucket:
                    self._procs.pop(thread_id, None)

    def cancel_thread(self, thread_id: str) -> int:
        """标记 thread_id 取消并强杀其名下所有活跃子进程组，返回被终止的进程组数量。"""
        with self._lock:
            self._cancelled_threads.add(thread_id)
            procs = list(self._procs.get(thread_id, {}).values())

        killed = 0
        for proc in procs:
            if self._terminate_proc_unlocked(proc):
                killed += 1
        return killed

    @staticmethod
    def _terminate_proc_unlocked(proc: subprocess.Popen[str]) -> bool:
        if proc.poll() is not None:
            return False
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            try:
                proc.terminate()
            except OSError:
                return False

        # 等待最多 1.5s 优雅退出，超时升级 SIGKILL
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return True
            time.sleep(0.05)

        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            try:
                proc.kill()
            except OSError:
                pass
        return True


_CLI_ARG_PLACEHOLDER_RE = re.compile(r"\{(prompt|prompt_file|cwd|sandbox|output_file|model|binary)\}")


def _safe_replace_cli_placeholders(template: str, mapping: dict[str, str]) -> str:
    return _CLI_ARG_PLACEHOLDER_RE.sub(lambda m: mapping.get(m.group(1), m.group(0)), template)


def _build_cli_command(
    profile: AgentProfileSpec,
    resolved_bin: str,
    *,
    prompt: str,
    cwd: str,
    sandbox: SandboxMode,
    prompt_file: str,
    output_file: str,
) -> tuple[list[str] | str, bool]:
    """根据驱动类型构造非交互命令及 shell 标志。"""
    placeholders = {
        "prompt": prompt,
        "prompt_file": prompt_file,
        "cwd": cwd,
        "sandbox": sandbox,
        "output_file": output_file,
        "model": profile.model or "",
        "binary": resolved_bin,
    }

    # 若用户显式配置了 args_template，则优先使用自定义参数模板（安全替换已知占位符，不破坏 JSON/Shell 花括号）
    if profile.args_template:
        cmd = [resolved_bin] + [_safe_replace_cli_placeholders(arg, placeholders) for arg in profile.args_template]
        return cmd, False

    if profile.driver == "custom" and profile.command_template:
        safe_map = {
            "prompt": shlex.quote(prompt),
            "prompt_file": shlex.quote(prompt_file),
            "cwd": shlex.quote(cwd),
            "sandbox": shlex.quote(sandbox),
            "output_file": shlex.quote(output_file),
            "model": shlex.quote(profile.model or ""),
            "binary": shlex.quote(resolved_bin),
        }
        return _safe_replace_cli_placeholders(profile.command_template, safe_map), True

    if profile.driver == "codex":
        cmd = [
            resolved_bin,
            "exec",
            "--cd",
            cwd,
            "-s",
            sandbox,
            "--skip-git-repo-check",
            "-c",
            'approval_policy="never"',
            "-o",
            output_file,
        ]
        if profile.model:
            cmd += ["-m", profile.model]
        cmd.append(prompt)
        return cmd, False

    if profile.driver == "claude":
        cmd = [resolved_bin, "-p", prompt, "--output-format", "text"]
        if sandbox == "workspace-write":
            cmd += ["--permission-mode", "acceptEdits"]
        else:
            cmd += ["--permission-mode", "plan"]
        if profile.model:
            cmd += ["--model", profile.model]
        return cmd, False

    if profile.driver == "kimi":
        cmd = [resolved_bin, "--quiet"]
        if sandbox == "workspace-write":
            cmd.append("--yolo")
        if profile.model:
            cmd += ["--model", profile.model]
        cmd += ["-p", prompt]
        return cmd, False

    if profile.driver == "gemini":
        cmd = [resolved_bin]
        if sandbox == "workspace-write":
            cmd.append("--yolo")
        if profile.model:
            cmd += ["-m", profile.model]
        cmd += ["-p", prompt]
        return cmd, False

    # custom 默认兜底：直接把 prompt 作为最后一个参数传入
    return [resolved_bin, prompt], False


def run_streaming_subprocess(
    cmd: list[str] | str,
    *,
    use_shell: bool,
    cwd: str,
    timeout_sec: int,
    extra_env: Optional[dict[str, str]] = None,
    supervisor: Optional[ProcessSupervisor] = None,
    thread_id: Optional[str] = None,
    node_key: Optional[str] = None,
    on_log: Optional[LogCallback] = None,
    output_file: Optional[str] = None,
) -> CliExecResult:
    """以独立进程组启动命令，实时按行推送日志，并支持超时与中途强杀。"""
    t0 = time.monotonic()
    env = {
        **os.environ,
        "PATH": _augmented_path(),
        "NO_COLOR": "1",
        "TERM": "dumb",
    }
    if extra_env:
        env.update(extra_env)

    cmd_display = cmd if isinstance(cmd, str) else " ".join(shlex.quote(part) for part in cmd)
    if on_log:
        on_log("system", f"$ (cwd={cwd}) {cmd_display[:500]}")

    try:
        proc = subprocess.Popen(
            cmd,
            shell=use_shell,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            start_new_session=True,
        )
    except Exception as exc:  # noqa: BLE001
        err_msg = f"进程启动失败: {exc}"
        if on_log:
            on_log("stderr", err_msg)
        return CliExecResult(
            ok=False,
            exit_code=-1,
            output=err_msg,
            stdout=err_msg,
            duration_ms=int((time.monotonic() - t0) * 1000),
            command_display=cmd_display,
        )

    reg_key = node_key or f"proc-{proc.pid}"
    if supervisor and thread_id:
        if not supervisor.register(thread_id, reg_key, proc):
            if on_log:
                on_log("system", "[cancelled] 运行已被人工强杀中止")
            return CliExecResult(
                ok=False,
                exit_code=-15,
                output="运行已被人工中止",
                stdout="运行已被人工中止",
                cancelled=True,
                duration_ms=int((time.monotonic() - t0) * 1000),
                command_display=cmd_display,
            )

    timed_out = threading.Event()

    def _watchdog() -> None:
        if supervisor:
            ProcessSupervisor._terminate_proc_unlocked(proc)
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except OSError:
                proc.kill()
        timed_out.set()

    timer = threading.Timer(timeout_sec, _watchdog)
    timer.daemon = True
    timer.start()

    lines: list[str] = []
    try:
        assert proc.stdout is not None
        for raw_line in iter(proc.stdout.readline, ""):
            clean = raw_line.rstrip("\r\n")
            lines.append(clean)
            if on_log:
                on_log("stdout", clean)
        proc.wait()
    finally:
        timer.cancel()
        if supervisor and thread_id:
            supervisor.unregister(thread_id, reg_key)

    duration_ms = int((time.monotonic() - t0) * 1000)
    full_stdout = "\n".join(lines).strip()
    was_cancelled = bool(supervisor and thread_id and supervisor.is_cancelled(thread_id))

    if timed_out.is_set():
        msg = f"执行超时（>{timeout_sec}s），进程已被终止"
        if on_log:
            on_log("stderr", msg)
        return CliExecResult(
            ok=False,
            exit_code=-9,
            output=f"{full_stdout[-8000:]}\n\n[{msg}]".strip(),
            stdout=full_stdout[-12000:],
            duration_ms=duration_ms,
            command_display=cmd_display,
        )

    if was_cancelled:
        msg = "运行已被人工强杀中止"
        if on_log:
            on_log("system", f"[cancelled] {msg}")
        return CliExecResult(
            ok=False,
            exit_code=proc.returncode if proc.returncode is not None else -15,
            output=msg,
            stdout=full_stdout[-12000:],
            cancelled=True,
            duration_ms=duration_ms,
            command_display=cmd_display,
        )

    # 若驱动产出了独立最终消息文件（如 codex -o），优先作为结构化 output，同时保留完整 stdout 日志
    final_output = ""
    if output_file and os.path.isfile(output_file):
        try:
            with open(output_file, encoding="utf-8", errors="replace") as fh:
                final_output = fh.read().strip()
        except OSError:
            final_output = ""

    if not final_output:
        final_output = full_stdout[-12000:]

    rc = proc.returncode if proc.returncode is not None else -1
    ok = rc == 0
    if not ok and not final_output:
        final_output = f"CLI 退出码非零 (exit_code={rc})"
    return CliExecResult(
        ok=ok,
        exit_code=rc,
        output=final_output,
        stdout=full_stdout[-16000:],
        cancelled=False,
        duration_ms=duration_ms,
        command_display=cmd_display,
    )


class AgentRegistry:
    """管理内置与自定义 CLI Agent 配置，并提供实时可执行探测与调用入口。"""

    def __init__(self, config_path: str) -> None:
        self.config_path = os.path.abspath(config_path)
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        self._lock = threading.Lock()

    def _load_custom_map(self) -> dict[str, AgentProfileSpec]:
        if not os.path.isfile(self.config_path):
            return {}
        try:
            with open(self.config_path, encoding="utf-8") as fh:
                raw = json.load(fh)
            items = raw.get("agents", raw) if isinstance(raw, dict) else raw
            result: dict[str, AgentProfileSpec] = {}
            if isinstance(items, list):
                for item in items:
                    spec = AgentProfileSpec.model_validate(item)
                    result[spec.id] = spec
            return result
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] 读取自定义 Agent 配置失败 {self.config_path}: {exc}", flush=True)
            return {}

    def _save_custom_map(self, mapping: dict[str, AgentProfileSpec]) -> None:
        payload = {"agents": [spec.model_dump(mode="json") for spec in mapping.values()]}
        with open(self.config_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")

    def list_agents(self) -> list[AgentProfileOut]:
        with self._lock:
            merged: dict[str, AgentProfileSpec] = {p.id: p for p in builtin_agent_profiles()}
            custom_map = self._load_custom_map()
            for aid, spec in custom_map.items():
                is_builtin = aid in merged and merged[aid].builtin
                merged[aid] = spec.model_copy(update={"builtin": is_builtin})

        out: list[AgentProfileOut] = []
        builtin_order = {"codex": 0, "claude": 1, "kimi": 2, "gemini": 3}
        for spec in sorted(merged.values(), key=lambda s: (builtin_order.get(s.id, 10), s.id)):
            resolved = resolve_binary(spec.binary)
            out.append(
                AgentProfileOut(
                    **spec.model_dump(),
                    available=resolved is not None,
                    resolved_path=resolved,
                )
            )
        return out

    def get_agent(self, agent_id: str) -> Optional[AgentProfileOut]:
        for item in self.list_agents():
            if item.id == agent_id:
                return item
        return None

    def resolve_available_agent(self, preferred_id: Optional[str] = None) -> AgentProfileOut:
        """解析指定 Agent；若为 'auto' 或为空则自动选首个本机已安装的 CLI；若所选 CLI 未安装则明确报错（绝不静默退回假 Stub）。"""
        agents = self.list_agents()
        if preferred_id and preferred_id != "auto":
            target = next((a for a in agents if a.id == preferred_id), None)
            if target is None:
                available_ids = [a.id for a in agents if a.available]
                raise ValueError(
                    f"节点指定的 Agent '{preferred_id}' 未注册。当前可用 CLI Agent: {available_ids or '无'}"
                )
            if not target.available:
                available_ids = [a.id for a in agents if a.available]
                raise RuntimeError(
                    f"节点指定的 CLI Agent '{target.name}' (二进制: '{target.binary}') 在当前主机未安装或不在 PATH 中。"
                    f"请在节点配置中切换为当前主机已就绪的 Agent ({available_ids or '请先在主机安装 CLI 或添加自定义 Agent'})。"
                )
            return target

        first_online = next((a for a in agents if a.available), None)
        if first_online is None:
            raise RuntimeError(
                "当前主机未探测到任何可用的 CLI Agent（codex / claude / kimi / gemini）。"
                "请确认 CLI 已安装并登录，或在操作台『Agent 管理』中添加自定义 CLI 路径。"
            )
        return first_online

    def save_agent(self, spec: AgentProfileSpec) -> AgentProfileOut:
        with self._lock:
            custom_map = self._load_custom_map()
            builtins = {p.id for p in builtin_agent_profiles()}
            saved = spec.model_copy(update={"builtin": spec.id in builtins})
            custom_map[saved.id] = saved
            self._save_custom_map(custom_map)
        resolved = resolve_binary(saved.binary)
        return AgentProfileOut(
            **saved.model_dump(),
            available=resolved is not None,
            resolved_path=resolved,
        )

    def delete_agent(self, agent_id: str) -> bool:
        builtins = {p.id for p in builtin_agent_profiles()}
        with self._lock:
            custom_map = self._load_custom_map()
            if agent_id in builtins:
                # 内置预设若被用户覆盖过参数，删除操作将其恢复为默认预设；否则报错提示内置预设不可彻底移除
                if agent_id in custom_map:
                    custom_map.pop(agent_id, None)
                    self._save_custom_map(custom_map)
                    return True
                raise ValueError(f"内置 CLI 预设 '{agent_id}' 不可删除（你可以修改其二进制路径或参数模板）")
            if agent_id not in custom_map:
                return False
            custom_map.pop(agent_id, None)
            self._save_custom_map(custom_map)
            return True

    def execute(
        self,
        agent_id: str,
        prompt: str,
        *,
        cwd: str,
        sandbox: SandboxMode = "read-only",
        timeout_sec: Optional[int] = None,
        supervisor: Optional[ProcessSupervisor] = None,
        thread_id: Optional[str] = None,
        node_key: Optional[str] = None,
        on_log: Optional[LogCallback] = None,
    ) -> tuple[AgentProfileOut, CliExecResult]:
        """调用指定本地 CLI Agent 执行任务并实时流式回传日志。"""
        profile = self.resolve_available_agent(agent_id)
        assert profile.resolved_path is not None

        target_cwd = os.path.abspath(os.path.expanduser(cwd or "."))
        if not os.path.isdir(target_cwd):
            raise ValueError(f"节点工作目录不存在: {target_cwd}")

        with tempfile.TemporaryDirectory(prefix="aw-cli-") as tmpdir:
            prompt_file = os.path.join(tmpdir, "prompt.txt")
            output_file = os.path.join(tmpdir, "last_message.txt")
            with open(prompt_file, "w", encoding="utf-8") as fh:
                fh.write(prompt)

            cmd, use_shell = _build_cli_command(
                profile,
                profile.resolved_path,
                prompt=prompt,
                cwd=target_cwd,
                sandbox=sandbox,
                prompt_file=prompt_file,
                output_file=output_file,
            )
            res = run_streaming_subprocess(
                cmd,
                use_shell=use_shell,
                cwd=target_cwd,
                timeout_sec=timeout_sec or profile.timeout_sec,
                extra_env=profile.env,
                supervisor=supervisor,
                thread_id=thread_id,
                node_key=node_key,
                on_log=on_log,
                output_file=output_file,
            )
        return profile, res
