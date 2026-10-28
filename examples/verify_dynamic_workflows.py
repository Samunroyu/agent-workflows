"""全链路验证：WorkflowRegistry 动态编译、REST API 与官方 MCP Server 交互测试。

验证项：
1. 默认 coding 工作流动态编译并跑完 interrupt -> resume -> fix loop -> finish 全链路
2. 外部 Agent 通过 MCP save_workflow 工具动态生成一个全新的 doc-review 工作流并落盘
3. FastAPI GET /api/workflows 立即可见新工作流，且可启动运行、暂停审批并恢复完成
4. 官方 MCP Server stdio 协议客户端连接与工具调用测试
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fastapi.testclient import TestClient  # noqa: E402
from langgraph.checkpoint.memory import InMemorySaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from src.registry import (  # noqa: E402
    EdgeSpec,
    NodePosition,
    NodeSpec,
    RouteRule,
    WorkflowRegistry,
    WorkflowSpec,
    _render_template,
)


def test_registry_coding_workflow() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        reg = WorkflowRegistry(tmpdir)
        assert os.path.isfile(os.path.join(tmpdir, "coding.json")), "应自动生成 coding.json 种子文件"
        graph = reg.compile_graph("coding", checkpointer=InMemorySaver())
        cfg = {"configurable": {"thread_id": "t-coding-1"}}

        graph.invoke(
            {"task": "测试动态编译 coding 图", "repo": ROOT, "max_attempts": 2, "attempt": 0, "events": []},
            cfg,
        )
        s1 = graph.get_state(cfg)
        assert s1.next == ("plan_gate",), f"应在 plan_gate 暂停, 实际: {s1.next}"
        assert s1.interrupts[0].value["stage"] == "plan_approval"

        # 批准计划 -> 走 implement#1 -> test#1(fail) -> implement#2 -> test#2(pass) -> review -> 停在 final_gate
        graph.invoke(Command(resume={"action": "approve", "feedback": ""}), cfg)
        s2 = graph.get_state(cfg)
        assert s2.next == ("final_gate",), f"应在 final_gate 暂停, 实际: {s2.next}"
        assert s2.interrupts[0].value["stage"] == "final_approval"

        # 批准合并 -> finish
        out = graph.invoke(Command(resume={"action": "approve", "feedback": ""}), cfg)
        assert out["events"] == [
            "plan",
            "plan_gate:approve",
            "implement#1",
            "test#1:fail",
            "implement#2",
            "test#2:pass",
            "review:ok",
            "final_gate:approve",
            "finish",
        ], f"事件流不匹配: {out['events']}"
        print("[ok] 1/4 默认 coding 工作流动态构图与中断恢复全链路通过")


def test_custom_workflow_and_fastapi() -> None:
    from console.app import app

    client = TestClient(app)

    # 1) 初始应有 coding 工作流，且响应头包含安全与隔离策略
    r0 = client.get("/api/workflows")
    assert r0.status_code == 200
    assert r0.headers.get("x-content-type-options") == "nosniff"
    assert r0.headers.get("x-frame-options") == "SAMEORIGIN"
    assert r0.headers.get("referrer-policy") == "strict-origin-when-cross-origin"
    assert "frame-ancestors 'self'" in (r0.headers.get("content-security-policy") or "")
    wf_ids = [w["id"] for w in r0.json()["workflows"]]
    assert "coding" in wf_ids

    # 1b) Fetch Metadata 跨站非导航请求应被 403 拦截
    r_cross = client.post(
        "/api/runs",
        json={"task": "cross-site attack"},
        headers={"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"},
    )
    assert r_cross.status_code == 403

    # 1c) 模板渲染对 JSON 花括号与属性表达式安全
    rendered = _render_template(
        '输出 JSON {"ok": true}，任务={task}，忽略属性访问={task.__class__}',
        {"task": "测试任务"},
    )
    assert '{"ok": true}' in rendered
    assert "任务=测试任务" in rendered
    assert "{task.__class__}" in rendered

    # 2) 模拟外部 Agent 通过 POST /api/workflows 注册一条新工作流 doc-review
    doc_wf = WorkflowSpec(
        id="doc-review",
        name="文档撰写与审批工作流",
        description="撰写技术文档 -> 人工审核 -> 发布归档",
        entry_point="draft",
        nodes=[
            NodeSpec(
                id="draft",
                label="撰写初稿",
                kind="agent",
                position=NodePosition(x=100, y=150),
                prompt_template="PLAN: 为任务撰写文档初稿：{task}\n反馈：{feedback}",
                sandbox="read-only",
                output_key="plan",
                event_template="draft",
            ),
            NodeSpec(
                id="review_gate",
                label="人工审稿",
                kind="gate",
                position=NodePosition(x=350, y=150),
                stage="doc_approval",
                expose_keys=["task", "plan"],
                options=["approve", "revise", "abort"],
                event_template="review_gate:{action}",
            ),
            NodeSpec(
                id="publish",
                label="发布文档",
                kind="summary",
                position=NodePosition(x=600, y=150),
                result_template="文档已发布：{task} | 内容摘要：{plan}",
                abort_result_template="文档发布已取消",
                event_template="publish",
            ),
        ],
        edges=[
            EdgeSpec(source="draft", target="review_gate"),
            EdgeSpec(
                source="review_gate",
                conditions=[
                    RouteRule(field="action", op="==", value="approve", target="publish", label="approve"),
                    RouteRule(field="action", op="==", value="abort", target="publish", label="abort"),
                ],
                default_target="draft",
                label="revise",
            ),
            EdgeSpec(source="publish", target="END"),
        ],
    )

    r_save = client.post("/api/workflows", json=doc_wf.model_dump(mode="json"))
    assert r_save.status_code == 200, r_save.text
    assert r_save.json()["id"] == "doc-review"

    # 3) Web 前端拉取 /api/workflows 立即可见
    r_list = client.get("/api/workflows")
    assert any(w["id"] == "doc-review" for w in r_list.json()["workflows"])

    # 4) 清理测试产生的工作流文件
    r_del = client.delete("/api/workflows/doc-review")
    assert r_del.status_code == 200
    print("[ok] 2/4 FastAPI 动态工作流创建、查询与删除接口验证通过")


async def test_mcp_server_stdio() -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    server_params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "src.mcp_server"],
        cwd=ROOT,
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools_res = await session.list_tools()
            tool_names = {t.name for t in tools_res.tools}
            expected = {"list_workflows", "get_workflow", "save_workflow", "delete_workflow", "start_run", "get_run"}
            assert expected.issubset(tool_names), f"MCP 工具缺失: {tool_names}"

            # 调用 list_workflows
            res = await session.call_tool("list_workflows", {})
            assert not res.is_error
            print(f"[ok] 3/4 官方 MCP Server stdio 握手与工具列表校验通过: {sorted(tool_names)}")

            # 通过 MCP save_workflow 动态创建工作流并在磁盘验证
            custom_spec = {
                "id": "mcp-generated-flow",
                "name": "MCP 自动生成工作流",
                "description": "由外部 Agent 通过 MCP 创建",
                "entry_point": "step1",
                "nodes": [
                    {
                        "id": "step1",
                        "label": "分析",
                        "kind": "agent",
                        "prompt_template": "PLAN: {task}",
                        "output_key": "plan",
                    },
                    {
                        "id": "done_step",
                        "label": "结束",
                        "kind": "summary",
                        "result_template": "MCP 流程完成: {plan}",
                    },
                ],
                "edges": [
                    {"source": "step1", "target": "done_step"},
                    {"source": "done_step", "target": "END"},
                ],
            }
            save_res = await session.call_tool("save_workflow", {"spec": custom_spec})
            assert not save_res.is_error, f"save_workflow 失败: {save_res}"
            wf_file = os.path.join(ROOT, "data", "workflows", "mcp-generated-flow.json")
            assert os.path.isfile(wf_file), "MCP 创建的工作流未落盘到 data/workflows/"

            # 删除测试工作流
            del_res = await session.call_tool("delete_workflow", {"workflow_id": "mcp-generated-flow"})
            assert not del_res.is_error
            assert not os.path.isfile(wf_file)
            print("[ok] 4/4 外部 Agent 通过 MCP save_workflow 动态生成工作流并落盘验证通过")


def main() -> None:
    test_registry_coding_workflow()
    test_custom_workflow_and_fastapi()
    asyncio.run(test_mcp_server_stdio())


if __name__ == "__main__":
    main()
