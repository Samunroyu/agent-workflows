"""第一期全链路集成验证套件：新图引擎 v2、多 CLI Agent 真实子进程、并行汇聚、实时流式控场、自然语言改图与 MCP Server。

验证项：
1. AgentRegistry 本机 CLI 探测（无假 StubAgent）与自定义 CLI 子进程注册/调用
2. 工作流自由增删改查、拓扑校验与上游变量推导（/api/workflows, /api/workflows/validate）
3. 四类核心节点 + 非对称并行汇聚（Fan-out / Barrier Join）+ 人工卡点『指定节点打回』与『在线修改上游产出放行』
4. 条件判断节点（condition）与 Shell 非零退出码自动修复循环
5. 运行中实时流式日志（/logs & SSE /stream）、进程组强杀中止（/cancel）与断点原地改参重试（/retry）
6. 一句话自然语言改图引擎（/api/workflows/copilot）与自动分层排版
7. 官方 MCP Server v2 stdio 协议握手与工具调用
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# 使用独立临时数据目录运行验证，绝不污染用户的 data/ 目录
_TEST_DATA_DIR = tempfile.mkdtemp(prefix="aw-verify-p1-")
os.environ["CONSOLE_DATA_DIR"] = _TEST_DATA_DIR
os.environ["CONSOLE_DB"] = os.path.join(_TEST_DATA_DIR, "checkpoints.sqlite")
os.environ["CONSOLE_WORKFLOWS_DIR"] = os.path.join(_TEST_DATA_DIR, "workflows")
os.environ["CONSOLE_AGENTS_FILE"] = os.path.join(_TEST_DATA_DIR, "agents.json")

from fastapi.testclient import TestClient  # noqa: E402

from console.app import app  # noqa: E402
from src.registry import _render_template  # noqa: E402


def _wait_for_status(client: TestClient, tid: str, targets: set[str], timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        r = client.get(f"/api/runs/{tid}")
        assert r.status_code == 200, r.text
        last = r.json()
        if last["status"] in targets:
            return last
        if last["status"] == "error" and "error" not in targets:
            raise AssertionError(f"运行 {tid} 发生错误: {last.get('error')}")
        time.sleep(0.05)
    raise AssertionError(f"等待运行 {tid} 进入状态 {targets} 超时，当前状态: {last}")


def test_1_agents_and_security(client: TestClient) -> str:
    # 安全响应头与跨站拦截校验
    r_health = client.get("/api/health")
    assert r_health.status_code == 200
    assert r_health.headers.get("x-content-type-options") == "nosniff"
    r_cross = client.post(
        "/api/runs",
        json={"workflow_id": "iterative-coding", "task": "attack"},
        headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
    )
    assert r_cross.status_code == 403

    # 模板安全渲染校验
    rendered = _render_template(
        'JSON {"ok": true}, task={task}, step={plan.output}, block_dunder={task.__class__}',
        {"inputs": {"task": "重构图引擎"}, "nodes": {"plan": {"output": "步骤一"}}},
    )
    assert '{"ok": true}' in rendered
    assert "task=重构图引擎" in rendered
    assert "step=步骤一" in rendered
    assert "{task.__class__}" in rendered

    # CLI Agent 列表：绝不包含假 stub，且包含四大内置预设（gemini 已替换为 agy）
    r_agents = client.get("/api/agents")
    assert r_agents.status_code == 200
    agents = r_agents.json()["agents"]
    ids = {a["id"] for a in agents}
    assert "stub" not in ids, "不应存在假 StubAgent"
    assert "gemini" not in ids, "内置列表不应再出现旧 gemini 预设"
    assert {"codex", "claude", "kimi", "agy"}.issubset(ids)

    # 注册一个使用当前 Python 解释器的真实本地自定义 CLI Agent（用于快速确定性验证多进程流式执行）
    py_cli = {
        "id": "local-py-agent",
        "name": "Local Python Test CLI",
        "driver": "custom",
        "binary": sys.executable,
        "description": "使用真实 Python 子进程模拟本地 CLI Agent 输出",
        "args_template": [
            "-u",
            "-c",
            "import sys, time; p=open(sys.argv[1], encoding='utf-8').read(); "
            "print('[trace] reading prompt...', flush=True); time.sleep(0.05); "
            "print('AGENT_OUT::' + p.replace('\\n', ' | '), flush=True)",
            "{prompt_file}",
        ],
    }
    r_save = client.post("/api/agents", json=py_cli)
    assert r_save.status_code == 200, r_save.text
    saved = r_save.json()
    assert saved["available"] is True
    assert saved["resolved_path"] is not None
    print("[ok] 1/6 多 CLI Agent 探测（零假 Stub）与真实自定义 CLI 注册校验通过")
    return "local-py-agent"


def test_2_workflow_crud_and_parallel_gate_controls(client: TestClient, agent_id: str) -> None:
    # 初始模板列表均可自由编辑与删除
    r_list = client.get("/api/workflows")
    assert r_list.status_code == 200
    initial_ids = {w["id"] for w in r_list.json()["workflows"]}
    assert "multi-agent-parallel" in initial_ids
    assert "iterative-coding" in initial_ids

    # 创建包含：Agent -> Gate -> 非对称并行 [后端 Agent || 前端 Agent -> 前端构建 Shell] -> 屏障汇聚 Shell -> 最终 Gate 的自定义工作流
    parallel_wf = {
        "id": "custom-parallel-flow",
        "name": "自定义前后端并行测试流",
        "description": "验证在线改方案、指定节点打回与非对称并行屏障汇聚",
        "entry_point": "arch",
        "inputs": [
            {"key": "task", "label": "任务", "kind": "textarea", "required": True},
            {"key": "tag", "label": "版本标签", "kind": "text", "required": False, "default": "v2.0"},
        ],
        "nodes": [
            {
                "id": "arch",
                "label": "架构设计",
                "kind": "agent",
                "agent_id": agent_id,
                "sandbox": "read-only",
                "capture_git_diff": False,
                "prompt_template": "ARCH[{inputs.tag}]: {task} / FB={feedback}",
            },
            {
                "id": "arch_gate",
                "label": "架构卡点",
                "kind": "gate",
                "review_nodes": ["arch"],
                "allow_edit_output": True,
                "rewind_targets": ["arch"],
            },
            {
                "id": "back_dev",
                "label": "后端开发",
                "kind": "agent",
                "agent_id": agent_id,
                "sandbox": "read-only",
                "capture_git_diff": False,
                "prompt_template": "BACK_USE_ARCH=({arch.output})",
            },
            {
                "id": "front_dev",
                "label": "前端开发",
                "kind": "agent",
                "agent_id": agent_id,
                "sandbox": "read-only",
                "capture_git_diff": False,
                "prompt_template": "FRONT_USE_ARCH=({arch.output})",
            },
            {
                "id": "front_lint",
                "label": "前端静态检查",
                "kind": "shell",
                "command": "echo 'FRONT_LINT_OK'",
            },
            {
                "id": "join_check",
                "label": "汇聚联调",
                "kind": "shell",
                "join_mode": "auto",
                "command": "echo 'JOIN_ALL_DONE'",
            },
            {
                "id": "final_gate",
                "label": "最终验收",
                "kind": "gate",
                "review_nodes": ["back_dev", "front_dev", "join_check"],
                "rewind_targets": ["back_dev", "front_dev", "arch"],
            },
        ],
        "edges": [
            {"source": "arch", "target": "arch_gate"},
            {"source": "arch_gate", "target": "back_dev"},
            {"source": "arch_gate", "target": "front_dev"},
            {"source": "front_dev", "target": "front_lint"},
            {"source": "back_dev", "target": "join_check"},
            {"source": "front_lint", "target": "join_check"},
            {"source": "join_check", "target": "final_gate"},
            {"source": "final_gate", "target": "END"},
        ],
    }

    # 先调 /api/workflows/validate 验证拓扑与上游变量提示
    r_val = client.post("/api/workflows/validate", json=parallel_wf)
    assert r_val.status_code == 200
    val_data = r_val.json()
    assert val_data["valid"] is True, val_data
    assert set(val_data["upstream_map"]["join_check"]) == {"arch", "back_dev", "front_dev", "front_lint"}

    r_create = client.post("/api/workflows", json=parallel_wf)
    assert r_create.status_code == 200, r_create.text

    # 启动运行（只传 task，使用默认 tag='v2.0'）
    r_run = client.post("/api/runs", json={"workflow_id": "custom-parallel-flow", "task": "支持节点自由增删"})
    assert r_run.status_code == 200, r_run.text
    tid = r_run.json()["thread_id"]

    # 1) 停在 arch_gate
    st1 = _wait_for_status(client, tid, {"waiting"})
    assert st1["next"] == ["arch_gate"]
    assert "ARCH[v2.0]: 支持节点自由增删" in st1["interrupts"][0]["upstream_outputs"]["arch"]["output"]

    # 2) 测试人工打回指定节点并附带反馈
    r_rev = client.post(
        f"/api/runs/{tid}/resume",
        json={"action": "revise", "target_node_id": "arch", "feedback": "请补充并行汇聚说明"},
    )
    assert r_rev.status_code == 200
    st2 = _wait_for_status(client, tid, {"waiting"})
    assert st2["values"]["nodes"]["arch"]["runs"] == 2
    assert "FB=请补充并行汇聚说明" in st2["values"]["nodes"]["arch"]["output"]

    # 3) 测试人工在卡点直接在线修改上游 arch 的产出文本并放行！
    r_app = client.post(
        f"/api/runs/{tid}/resume",
        json={
            "action": "approve",
            "edited_outputs": {"arch": "HUMAN_EDITED_CONTRACT_FINAL"},
        },
    )
    assert r_app.status_code == 200

    # 4) 应并行跑完 back_dev 与 (front_dev -> front_lint)，且 join_check 仅执行 1 次后停在 final_gate！
    st3 = _wait_for_status(client, tid, {"waiting"})
    assert st3["next"] == ["final_gate"]
    nodes_res = st3["values"]["nodes"]
    assert nodes_res["arch"]["edited_by_human"] is True
    assert "HUMAN_EDITED_CONTRACT_FINAL" in nodes_res["back_dev"]["output"]
    assert "HUMAN_EDITED_CONTRACT_FINAL" in nodes_res["front_dev"]["output"]
    assert nodes_res["join_check"]["runs"] == 1, f"并行屏障汇聚应只执行 1 次，实际: {nodes_res['join_check']}"

    # 5) 在 final_gate 单独选择打回给 front_dev 重跑（不重跑 back_dev！）
    r_rev_front = client.post(
        f"/api/runs/{tid}/resume",
        json={"action": "revise", "target_node_id": "front_dev", "feedback": "调整按钮间距"},
    )
    assert r_rev_front.status_code == 200
    st4 = _wait_for_status(client, tid, {"waiting"})
    assert st4["values"]["nodes"]["front_dev"]["runs"] == 2
    assert st4["values"]["nodes"]["back_dev"]["runs"] == 1, "只打回前端节点时不应重跑后端节点"

    # 6) 最终通过 -> done
    client.post(f"/api/runs/{tid}/resume", json={"action": "approve"})
    st5 = _wait_for_status(client, tid, {"done"})
    assert st5["status"] == "done"
    print("[ok] 2/6 四类节点、非对称并行屏障汇聚、在线改产出放行与自选节点打回全链路通过")


def test_3_condition_fix_loop(client: TestClient) -> None:
    # 利用临时文件计数：第 1 次测试失败 (exit 1)，第 2 次测试通过 (exit 0)
    flag_file = os.path.join(_TEST_DATA_DIR, "test_once.flag")
    loop_wf = {
        "id": "cond-loop-flow",
        "name": "条件修复循环验证",
        "entry_point": "fix_step",
        "nodes": [
            {
                "id": "fix_step",
                "label": "执行修复",
                "kind": "shell",
                "command": "echo 'fixing attempt {runs}'",
            },
            {
                "id": "verify_cmd",
                "label": "运行单测",
                "kind": "shell",
                "command": (
                    f"{sys.executable} -c \"import os, sys; p={flag_file!r}; "
                    "exists=os.path.exists(p); open(p,'w').write('1'); "
                    "print('PASS' if exists else 'FAIL_FIRST'); sys.exit(0 if exists else 1)\""
                ),
            },
            {
                "id": "judge",
                "label": "判断是否通过",
                "kind": "condition",
            },
        ],
        "edges": [
            {"source": "fix_step", "target": "verify_cmd"},
            {"source": "verify_cmd", "target": "judge"},
            {
                "source": "judge",
                "target": "END",
                "label": "通过",
                "condition": {"field": "verify_cmd.ok", "op": "==", "value": True},
            },
            {
                "source": "judge",
                "target": "fix_step",
                "label": "重试",
                "condition": {"field": "fix_step.runs", "op": "<", "value": 3},
            },
            {"source": "judge", "target": "END", "is_default": True},
        ],
    }
    assert client.post("/api/workflows", json=loop_wf).status_code == 200
    tid = client.post("/api/runs", json={"workflow_id": "cond-loop-flow", "task": "测试自动修复循环"}).json()["thread_id"]
    done_st = _wait_for_status(client, tid, {"done"})
    assert done_st["values"]["nodes"]["fix_step"]["runs"] == 2
    assert done_st["values"]["nodes"]["verify_cmd"]["ok"] is True
    print("[ok] 3/6 Condition 条件节点与 Shell 失败自动循环修复验证通过")


def test_4_streaming_logs_cancel_and_inplace_retry(client: TestClient) -> None:
    slow_wf = {
        "id": "cancel-retry-flow",
        "name": "实时日志强杀与原地重试验证",
        "entry_point": "step_fast",
        "nodes": [
            {
                "id": "step_fast",
                "label": "前置快速步骤",
                "kind": "shell",
                "command": "echo 'FAST_DONE'",
            },
            {
                "id": "step_slow",
                "label": "耗时步骤",
                "kind": "shell",
                "command": f"{sys.executable} -u -c \"import time; print('LIVE_LINE_1', flush=True); time.sleep(20)\"",
            },
        ],
        "edges": [
            {"source": "step_fast", "target": "step_slow"},
            {"source": "step_slow", "target": "END"},
        ],
    }
    assert client.post("/api/workflows", json=slow_wf).status_code == 200
    tid = client.post("/api/runs", json={"workflow_id": "cancel-retry-flow", "task": "验证实时日志与强杀"}).json()["thread_id"]

    # 1) 在 step_slow 仍在 sleep(20) 期间，验证增量日志接口已能实时读到 LIVE_LINE_1
    deadline = time.monotonic() + 8.0
    found_live = False
    while time.monotonic() < deadline:
        logs = client.get(f"/api/runs/{tid}/logs", params={"node_id": "step_slow"}).json()["logs"]
        if any("LIVE_LINE_1" in item["text"] for item in logs):
            found_live = True
            break
        time.sleep(0.08)
    assert found_live, "未在子进程运行期间实时流式获取到 LIVE_LINE_1"

    # 2) 立即调用强杀接口 /cancel，应在 2.5s 内迅速终止进程（无需等 20s！）
    t_kill = time.monotonic()
    r_cancel = client.post(f"/api/runs/{tid}/cancel")
    assert r_cancel.status_code == 200, r_cancel.text
    st_cancelled = _wait_for_status(client, tid, {"cancelled"}, timeout=4.0)
    assert (time.monotonic() - t_kill) < 4.0
    assert st_cancelled["next"] == ["step_slow"]

    # 3) 原地改参重试 step_slow（不重跑 step_fast！）
    r_retry = client.post(
        f"/api/runs/{tid}/retry",
        json={"node_id": "step_slow", "command": "echo 'RETRIED_QUICK_SUCCESS'"},
    )
    assert r_retry.status_code == 200, r_retry.text
    st_done = _wait_for_status(client, tid, {"done"})
    assert st_done["values"]["nodes"]["step_fast"]["runs"] == 1, "原地重试不应重复执行已完成的前置节点 step_fast"
    assert "RETRIED_QUICK_SUCCESS" in st_done["values"]["nodes"]["step_slow"]["output"]

    # 4) 验证 SSE 流端点可正常输出事件流并收尾
    with client.stream("GET", f"/api/runs/{tid}/stream") as sse_resp:
        assert sse_resp.status_code == 200
        body_text = "".join(list(sse_resp.iter_text()))
        assert "event: log" in body_text
        assert "RETRIED_QUICK_SUCCESS" in body_text

    print("[ok] 4/6 实时流式日志（REST + SSE）、进程组强杀中止与断点原地改参重试验证通过")


def test_5_natural_language_copilot_and_delete(client: TestClient) -> None:
    # 注册一个模拟返回 JSON 的本地 CLI 来确定性验证 /api/workflows/copilot 链路与自动排版
    copilot_mock_wf = {
        "summary": "已按指令创建『代码扫描与人工确认』工作流",
        "workflow": {
            "id": "nl-generated-scan",
            "name": "安全扫描与确认",
            "description": "由一句话自然语言生成",
            "entry_point": "scan",
            "nodes": [
                {"id": "scan", "label": "运行扫描", "kind": "shell", "command": "echo scan_ok"},
                {"id": "confirm", "label": "人工确认", "kind": "gate"},
            ],
            "edges": [
                {"source": "scan", "target": "confirm"},
                {"source": "confirm", "target": "END"},
            ],
        },
    }
    encoded = json.dumps(copilot_mock_wf, ensure_ascii=False)
    client.post(
        "/api/agents",
        json={
            "id": "copilot-cli-test",
            "name": "Copilot CLI Test",
            "driver": "custom",
            "binary": sys.executable,
            "args_template": ["-c", f"print('''```json\\n{encoded}\\n```''')"],
        },
    )

    r_cp = client.post(
        "/api/workflows/copilot",
        json={
            "instruction": "创建一个先运行扫描脚本、再人工确认的工作流",
            "agent_id": "copilot-cli-test",
            "auto_save": True,
        },
    )
    assert r_cp.status_code == 200, r_cp.text
    cp_data = r_cp.json()
    assert cp_data["saved"] is True
    assert cp_data["workflow"]["id"] == "nl-generated-scan"
    # 验证自动排版已为未填坐标的节点分配不重叠的 x 坐标
    pos0 = cp_data["workflow"]["nodes"][0]["position"]
    pos1 = cp_data["workflow"]["nodes"][1]["position"]
    assert pos0["x"] < pos1["x"]

    # 验证任何工作流（包括初始模板 iterative-coding）均可被自由删除，不再报错拦截
    assert client.delete("/api/workflows/nl-generated-scan").status_code == 200
    assert client.delete("/api/workflows/iterative-coding").status_code == 200
    print("[ok] 5/6 自然语言构图改图引擎、自动拓扑排版与任意工作流自由删除验证通过")


async def test_6_mcp_server_stdio() -> None:
    import importlib.util

    if importlib.util.find_spec("mcp") is None:
        print("[ok] 6/6 当前本地 .venv 未安装可选包 mcp（console.app 已优雅降级跳过 /mcp 挂载）")
        return

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "src.mcp_server"],
        cwd=ROOT,
        env={**os.environ},
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_res = await session.list_tools()
            tool_names = {t.name for t in tools_res.tools}
            expected = {
                "list_agents",
                "list_workflows",
                "get_workflow",
                "save_workflow",
                "delete_workflow",
                "start_run",
                "get_run",
                "resume_run",
                "cancel_run",
                "retry_run",
            }
            assert expected.issubset(tool_names), f"MCP 工具缺失: {tool_names}"
            res = await session.call_tool("list_agents", {})
            assert not res.is_error
            print(f"[ok] 6/6 官方 MCP Server v2 stdio 握手与 10 项工具校验通过")


def test_7_quick_create_rename_and_condition_edge_polish(client: TestClient) -> None:
    # 1) 纯中文名称一键新建空白工作流（无需填英文 ID，立即落盘并出现在工作流列表中）
    r_blank = client.post(
        "/api/workflows/create",
        json={"name": "全新空白协同研发流", "mode": "blank"},
    )
    assert r_blank.status_code == 200, r_blank.text
    blank_wf = r_blank.json()
    assert blank_wf["id"] == "workflow-1"
    assert blank_wf["name"] == "全新空白协同研发流"
    assert blank_wf["updated_at"] is not None

    # 再次纯中文新建自动递增为 workflow-2，绝不覆盖 workflow-1
    r_blank2 = client.post(
        "/api/workflows/create",
        json={"name": "第二张空白工作流", "mode": "blank"},
    )
    assert r_blank2.status_code == 200, r_blank2.text
    assert r_blank2.json()["id"] == "workflow-2"

    # 2) 基于现有模板克隆新建，并测试显式重复 ID 返回 409 Conflict
    r_tpl = client.post(
        "/api/workflows/create",
        json={
            "name": "并行工作流定制版",
            "id": "my-parallel-v2",
            "mode": "template",
            "template_id": "multi-agent-parallel",
        },
    )
    assert r_tpl.status_code == 200, r_tpl.text
    assert r_tpl.json()["id"] == "my-parallel-v2"
    assert r_tpl.json()["name"] == "并行工作流定制版"

    r_dup = client.post(
        "/api/workflows/create",
        json={"name": "冲突测试", "id": "my-parallel-v2", "mode": "blank"},
    )
    assert r_dup.status_code == 409, r_dup.text

    # 3) 一句话 AI 新建模式（保留用户指定的中文名称并自动分配唯一 ID 落盘）
    r_ai_new = client.post(
        "/api/workflows/create",
        json={
            "name": "AI 一句话新建扫描流",
            "mode": "ai",
            "ai_instruction": "创建一个先运行扫描脚本、再人工确认的工作流",
            "agent_id": "copilot-cli-test",
        },
    )
    assert r_ai_new.status_code == 200, r_ai_new.text
    ai_wf = r_ai_new.json()
    assert ai_wf["name"] == "AI 一句话新建扫描流"
    assert ai_wf["id"] in {w["id"] for w in client.get("/api/workflows").json()["workflows"]}

    # 4) 节点 ID 重命名全图联动（entry_point、edges、review_nodes、rewind_targets 与 {node_id.xxx} 模板）
    target_wf = client.get("/api/workflows/custom-parallel-flow").json()
    r_ren = client.post(
        "/api/workflows/rename-node",
        json={
            "workflow": target_wf,
            "old_node_id": "arch",
            "new_node_id": "design_spec",
        },
    )
    assert r_ren.status_code == 200, r_ren.text
    renamed = r_ren.json()
    assert renamed["entry_point"] == "design_spec"
    node_by_id = {n["id"]: n for n in renamed["nodes"]}
    assert "design_spec" in node_by_id and "arch" not in node_by_id
    assert "{design_spec.output}" in node_by_id["back_dev"]["prompt_template"]
    assert "{design_spec.output}" in node_by_id["front_dev"]["prompt_template"]
    assert node_by_id["arch_gate"]["review_nodes"] == ["design_spec"]
    assert "design_spec" in node_by_id["final_gate"]["rewind_targets"]

    # 5) 兜底边携带空 condition 残留时仍优先按 is_default 路由
    stale_cond_wf = {
        "id": "stale-cond-edge-flow",
        "name": "空条件残留兜底路由测试",
        "entry_point": "step_a",
        "nodes": [
            {"id": "step_a", "label": "步骤A", "kind": "shell", "command": "echo 'A'"},
            {"id": "cond_a", "label": "判断", "kind": "condition"},
            {"id": "fallback_ok", "label": "兜底目标", "kind": "shell", "command": "echo 'HIT_DEFAULT'"},
        ],
        "edges": [
            {"source": "step_a", "target": "cond_a", "condition": {"field": "", "op": "==", "value": ""}},
            {
                "source": "cond_a",
                "target": "END",
                "condition": {"field": "step_a.output", "op": "==", "value": "NEVER_MATCH"},
            },
            {
                "source": "cond_a",
                "target": "fallback_ok",
                "is_default": True,
                "condition": {"field": "", "op": "==", "value": ""},
            },
            {"source": "fallback_ok", "target": "END"},
        ],
    }
    assert client.post("/api/workflows", json=stale_cond_wf).status_code == 200
    tid = client.post("/api/runs", json={"workflow_id": "stale-cond-edge-flow", "task": "check"}).json()["thread_id"]
    st = _wait_for_status(client, tid, {"done"})
    assert st["values"]["nodes"]["fallback_ok"]["ok"] is True
    print("[ok] 7/8 一键新建工作流（空白/模板/AI）、节点 ID 全图联动重命名与条件边容错验证通过")


def test_8_agy_preset_and_gemini_compat(client: TestClient) -> None:
    from src.agents.registry import AgentProfileSpec, AgentRegistry, _build_cli_command

    # 1) _build_cli_command 在只读 / 可写两种模式（及带 model）下生成的命令均正确
    agy_spec = AgentProfileSpec(id="agy", name="Antigravity CLI", driver="agy", binary="agy", builtin=True)
    cmd_ro, shell_ro = _build_cli_command(
        agy_spec,
        "/usr/local/bin/agy",
        prompt="分析架构",
        cwd="/tmp",
        sandbox="read-only",
        prompt_file="/tmp/p.txt",
        output_file="/tmp/o.txt",
    )
    assert shell_ro is False
    assert cmd_ro == ["/usr/local/bin/agy", "-p", "分析架构"], cmd_ro

    cmd_rw_no_model, _ = _build_cli_command(
        agy_spec,
        "/usr/local/bin/agy",
        prompt="修改代码",
        cwd="/tmp",
        sandbox="workspace-write",
        prompt_file="/tmp/p.txt",
        output_file="/tmp/o.txt",
    )
    assert cmd_rw_no_model == ["/usr/local/bin/agy", "--dangerously-skip-permissions", "-p", "修改代码"], cmd_rw_no_model

    cmd_rw, shell_rw = _build_cli_command(
        agy_spec.model_copy(update={"model": "gemini-2.5-pro"}),
        "/usr/local/bin/agy",
        prompt="实现功能",
        cwd="/tmp",
        sandbox="workspace-write",
        prompt_file="/tmp/p.txt",
        output_file="/tmp/o.txt",
    )
    assert shell_rw is False
    assert cmd_rw == [
        "/usr/local/bin/agy",
        "--model",
        "gemini-2.5-pro",
        "--dangerously-skip-permissions",
        "-p",
        "实现功能",
    ], cmd_rw

    # 2) 含 driver="gemini" 及旧内置覆盖 id="gemini" 的配置文件可正常读取、跳过旧 id="gemini" 且不改写原文件
    mock_agy_bin = os.path.join(_TEST_DATA_DIR, "mock_agy")
    with open(mock_agy_bin, "w", encoding="utf-8") as fh:
        fh.write(f"#!{sys.executable}\nimport json, sys\nprint('AGY_ARGV::' + json.dumps(sys.argv[1:], ensure_ascii=False), flush=True)\n")
    os.chmod(mock_agy_bin, 0o755)

    legacy_cfg_path = os.path.join(_TEST_DATA_DIR, "legacy_agents.json")
    legacy_raw = {
        "agents": [
            {
                "id": "gemini",
                "name": "Gemini CLI Legacy Override",
                "driver": "gemini",
                "binary": "/old/path/gemini",
                "builtin": True,
            },
            {
                "id": "team-gemini-wrapper",
                "name": "Team Wrapper",
                "driver": "gemini",
                "binary": mock_agy_bin,
            },
        ]
    }
    with open(legacy_cfg_path, "w", encoding="utf-8") as fh:
        json.dump(legacy_raw, fh, ensure_ascii=False, indent=2)
    before_bytes = open(legacy_cfg_path, "rb").read()

    old_agy_bin = os.environ.get("CONSOLE_AGY_BIN")
    os.environ["CONSOLE_AGY_BIN"] = mock_agy_bin
    try:
        reg = AgentRegistry(legacy_cfg_path)
        listed = reg.list_agents()
        listed_ids = [a.id for a in listed]
        assert "agy" in listed_ids
        assert "gemini" not in listed_ids, f"旧覆盖项 id=gemini 应被跳过: {listed_ids}"
        assert "team-gemini-wrapper" in listed_ids

        wrapper = next(a for a in listed if a.id == "team-gemini-wrapper")
        assert wrapper.driver == "agy", f"旧 driver=gemini 应自动归一化为 agy，实际: {wrapper.driver}"

        # 3) get_agent("gemini") 与 resolve_available_agent("gemini") 自动别名解析到 agy
        got = reg.get_agent("gemini")
        assert got is not None and got.id == "agy"
        resolved = reg.resolve_available_agent("gemini")
        assert resolved.id == "agy" and resolved.available is True

        # 确保未改写用户原始配置文件
        after_bytes = open(legacy_cfg_path, "rb").read()
        assert before_bytes == after_bytes, "读取兼容不应改写用户原有 agents.json 文件"

        # 拒绝通过 API 新保存已废弃的 id="gemini"（避免保存后被列表跳过造成困惑）
        r_bad_save = client.post(
            "/api/agents",
            json={"id": "gemini", "name": "Old Gemini", "driver": "agy", "binary": "agy"},
        )
        assert r_bad_save.status_code == 400, r_bad_save.text

        # 4) 老工作流节点 agent_id="gemini" 端到端运行自动路由到 agy（验证只读与可写参数传递）
        legacy_wf = {
            "id": "legacy-gemini-to-agy-flow",
            "name": "旧版 gemini 节点自动兼容测试",
            "entry_point": "ro_step",
            "nodes": [
                {
                    "id": "ro_step",
                    "label": "旧版只读节点",
                    "kind": "agent",
                    "agent_id": "gemini",
                    "sandbox": "read-only",
                    "capture_git_diff": False,
                    "prompt_template": "RO_TASK={task}",
                },
                {
                    "id": "rw_step",
                    "label": "新版可写节点",
                    "kind": "agent",
                    "agent_id": "agy",
                    "sandbox": "workspace-write",
                    "capture_git_diff": False,
                    "prompt_template": "RW_TASK={task}",
                },
            ],
            "edges": [
                {"source": "ro_step", "target": "rw_step"},
                {"source": "rw_step", "target": "END"},
            ],
        }
        assert client.post("/api/workflows", json=legacy_wf).status_code == 200
        tid = client.post(
            "/api/runs",
            json={"workflow_id": "legacy-gemini-to-agy-flow", "task": "兼容校验"},
        ).json()["thread_id"]
        done_st = _wait_for_status(client, tid, {"done"})
        ro_node = done_st["values"]["nodes"]["ro_step"]
        rw_node = done_st["values"]["nodes"]["rw_step"]
        assert ro_node["agent_id"] == "agy"
        assert 'AGY_ARGV::["-p", "RO_TASK=兼容校验"]' in ro_node["output"], ro_node["output"]
        assert rw_node["agent_id"] == "agy"
        assert 'AGY_ARGV::["--dangerously-skip-permissions", "-p", "RW_TASK=兼容校验"]' in rw_node["output"], rw_node["output"]
    finally:
        if old_agy_bin is None:
            os.environ.pop("CONSOLE_AGY_BIN", None)
        else:
            os.environ["CONSOLE_AGY_BIN"] = old_agy_bin

    print("[ok] 8/8 Antigravity CLI (agy) 命令构造与旧版 gemini 配置/工作流节点无损兼容验证通过")


def main() -> None:
    client = TestClient(app)
    agent_id = test_1_agents_and_security(client)
    test_2_workflow_crud_and_parallel_gate_controls(client, agent_id)
    test_3_condition_fix_loop(client)
    test_4_streaming_logs_cancel_and_inplace_retry(client)
    test_5_natural_language_copilot_and_delete(client)
    asyncio.run(test_6_mcp_server_stdio())
    test_7_quick_create_rename_and_condition_edge_polish(client)
    test_8_agy_preset_and_gemini_compat(client)


if __name__ == "__main__":
    main()
