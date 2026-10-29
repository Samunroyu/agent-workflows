"""基于官方 MCP Python SDK 的工作流管理与运行 MCP Server v2。

外部 Agent（Claude Code / Codex / Cursor / Antigravity 等）可通过本 MCP Server：
1. 查看本机已就绪的 CLI Agent 列表（list_agents）
2. 查看、创建、修改或删除自定义工作流（list_workflows / get_workflow / save_workflow / delete_workflow）
3. 触发工作流运行、查看节点实时状态与日志、执行人工卡点恢复或单步重试

启动方式：
- 本地 stdio 模式（默认）：
    .venv/bin/python -m src.mcp_server
- 随 console.app:app 一起挂载在 `/mcp` 路径下
"""

from __future__ import annotations

import argparse
from typing import Any, Literal, Optional

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]

from console.app import _run_state, get_agent_registry, get_registry, get_runtime, start_run_service
from src.registry import WorkflowSpec

mcp = MCPServer(
    "agent-workflows",
    instructions=(
        "管理 agent-workflows 多 Agent 研发协同平台的工作流定义、CLI Agent 与运行状态。"
        "支持四类标准节点（agent / shell / gate / condition）、并行汇聚与条件循环。"
    ),
)


@mcp.tool(description="列出当前机器所有已注册的 CLI Agent 及其二进制可用状态")
def list_agents() -> dict[str, Any]:
    return {
        "agents": [a.model_dump(mode="json") for a in get_agent_registry().list_agents()],
    }


@mcp.tool(description="列出所有已保存的工作流（含节点、连线与动态入参定义）")
def list_workflows() -> dict[str, Any]:
    specs = get_registry().list_workflows()
    return {
        "workflows": [s.model_dump(mode="json") for s in specs],
    }


@mcp.tool(description="获取指定 workflow_id 的完整 WorkflowSpec 定义")
def get_workflow(workflow_id: str = "iterative-coding") -> dict[str, Any]:
    spec = get_registry().get_workflow(workflow_id)
    if spec is None:
        raise ValueError(f"未找到工作流: {workflow_id}")
    return spec.model_dump(mode="json")


@mcp.tool(description="创建或更新工作流定义（支持四类节点、并行分支与条件循环），校验编译通过后落盘到 data/workflows/")
def save_workflow(spec: WorkflowSpec) -> dict[str, Any]:
    saved = get_registry().save_workflow(spec)
    return saved.model_dump(mode="json")


@mcp.tool(description="删除指定工作流")
def delete_workflow(workflow_id: str) -> dict[str, Any]:
    removed = get_registry().delete_workflow(workflow_id)
    if not removed:
        raise ValueError(f"未找到工作流: {workflow_id}")
    return {"ok": True, "workflow_id": workflow_id}


@mcp.tool(description="按指定 workflow_id 与入参启动一次工作流运行，返回 thread_id")
def start_run(
    task: str,
    workflow_id: str = "iterative-coding",
    inputs: Optional[dict[str, Any]] = None,
    repo: Optional[str] = None,
    test_cmd: Optional[str] = None,
) -> dict[str, Any]:
    return start_run_service(
        task=task,
        workflow_id=workflow_id,
        inputs=inputs,
        repo=repo,
        test_cmd=test_cmd,
    )


@mcp.tool(description="查询指定 thread_id 的运行状态、各节点产出/Diff 与待人工卡点信息")
def get_run(thread_id: str) -> dict[str, Any]:
    state = _run_state(thread_id)
    if state.get("status") == "unknown":
        raise ValueError(f"未找到运行记录: {thread_id}")
    return state


@mcp.tool(description="人工卡点恢复：支持 approve（可附 edited_outputs）、revise（可指定 target_node_id 打回任意上游节点）、abort")
def resume_run(
    thread_id: str,
    action: Literal["approve", "revise", "abort"] = "approve",
    feedback: str = "",
    edited_outputs: Optional[dict[str, str]] = None,
    target_node_id: Optional[str] = None,
) -> dict[str, Any]:
    return get_runtime().resume_run(
        thread_id,
        action=action,
        feedback=feedback,
        edited_outputs=edited_outputs,
        target_node_id=target_node_id,
    )


@mcp.tool(description="强杀中止指定运行的活跃子进程")
def cancel_run(thread_id: str) -> dict[str, Any]:
    return get_runtime().cancel_run(thread_id)


@mcp.tool(description="对报错或被中止的运行执行节点原地重试")
def retry_run(thread_id: str, node_id: Optional[str] = None) -> dict[str, Any]:
    return get_runtime().retry_run(thread_id, node_id=node_id)


def main() -> None:
    parser = argparse.ArgumentParser(description="agent-workflows MCP Server v2")
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
