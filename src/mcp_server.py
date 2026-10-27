"""基于官方 MCP Python SDK 的工作流管理与运行 MCP Server。

外部 Agent（Claude Code / Codex / Cursor / Antigravity 等）可通过本 MCP Server：
1. 查看现有工作流列表与定义模板（list_workflows / get_workflow）
2. 动态生成并保存新工作流（save_workflow），落盘后 Web 操作台立即可见并渲染画布
3. 触发工作流运行并查询状态（start_run / get_run）

启动方式：
- 本地 stdio 模式（默认）：
    .venv/bin/python -m src.mcp_server
- 独立 Streamable HTTP 模式：
    .venv/bin/python -m src.mcp_server --transport streamable-http --port 8081
- 随 console.app:app 一起挂载在 `/mcp` 路径下
"""

from __future__ import annotations

import argparse
from typing import Any, Optional

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]

from console.app import _run_state, get_registry, start_run_service
from src.registry import WorkflowSpec

mcp = MCPServer(
    "agent-workflows",
    instructions=(
        "管理 agent-workflows 编排平台的工作流定义与运行。"
        "你可以通过 save_workflow 创建或更新声明式工作流（包含 agent/gate/shell/summary 节点与条件路由边），"
        "保存后将持久化到 data/workflows/<id>.json 并在 Web 操作台画布上立即可见。"
    ),
)


@mcp.tool(description="列出所有已注册的工作流（含 ID、名称、描述及节点/连线拓扑）")
def list_workflows() -> dict[str, Any]:
    specs = get_registry().list_workflows()
    return {
        "workflows": [s.model_dump(mode="json") for s in specs],
    }


@mcp.tool(description="获取指定 workflow_id 的完整 WorkflowSpec 定义（可作为生成新工作流的参考模板）")
def get_workflow(workflow_id: str = "coding") -> dict[str, Any]:
    spec = get_registry().get_workflow(workflow_id)
    if spec is None:
        raise ValueError(f"未找到工作流: {workflow_id}")
    return spec.model_dump(mode="json")


@mcp.tool(
    description=(
        "创建或更新一个工作流定义（WorkflowSpec）。"
        "后端会先动态编译验证 LangGraph 状态机连通性与合法性，通过后落盘到 data/workflows/<id>.json，"
        "Web 前端刷新即可看到该工作流的拓扑画布并可直接启动运行。"
    )
)
def save_workflow(spec: WorkflowSpec) -> dict[str, Any]:
    saved = get_registry().save_workflow(spec)
    return saved.model_dump(mode="json")


@mcp.tool(description="删除指定的自定义工作流（内置 coding 工作流不可删除）")
def delete_workflow(workflow_id: str) -> dict[str, Any]:
    removed = get_registry().delete_workflow(workflow_id)
    if not removed:
        raise ValueError(f"未找到工作流: {workflow_id}")
    return {"ok": True, "workflow_id": workflow_id}


@mcp.tool(description="按指定 workflow_id 启动一次工作流运行，返回 thread_id")
def start_run(
    task: str,
    workflow_id: str = "coding",
    repo: Optional[str] = None,
    test_cmd: Optional[str] = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    return start_run_service(
        task=task,
        workflow_id=workflow_id,
        repo=repo,
        max_attempts=max_attempts,
        test_cmd=test_cmd,
    )


@mcp.tool(description="查询指定 thread_id 的运行状态、当前节点、事件流与待人工审批信息")
def get_run(thread_id: str) -> dict[str, Any]:
    state = _run_state(thread_id)
    if state.get("status") == "unknown":
        raise ValueError(f"未找到运行记录: {thread_id}")
    return state


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-workflows MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="传输协议（默认 stdio）",
    )
    args = parser.parse_args()
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
