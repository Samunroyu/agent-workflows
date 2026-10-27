"""通过 langgraph dev 的 HTTP API 驱动工作流（模拟未来 UI / 远程客户端）。

先起服务：
    source env.sh && langgraph dev --no-browser --port 20245

再跑本脚本：
    python examples/client_demo.py
"""

import json

from langgraph_sdk import get_sync_client

URL = "http://127.0.0.1:20246"
ASSISTANT = "coding"


def main() -> None:
    client = get_sync_client(url=URL)

    names = [a["graph_id"] for a in client.assistants.search()]
    print(f"[ok] server assistants: {names}")

    thread = client.threads.create()
    tid = thread["thread_id"]
    print(f"[ok] thread: {tid}")

    # 1) 启动 run；遇到 interrupt 会停住，wait 返回当前 state
    client.runs.wait(
        tid,
        ASSISTANT,
        input={
            "task": "通过 API 跑一遍 coding 工作流",
            "repo": ".",
            "max_attempts": 2,
            "attempt": 0,
            "events": [],
        },
    )

    # 2) 循环处理人审 gate
    while True:
        state = client.threads.get_state(tid)
        interrupts = [
            itr
            for task in state.get("tasks", [])
            for itr in task.get("interrupts", [])
        ]
        if not interrupts:
            break
        for itr in interrupts:
            print("\n=== 待审 ===")
            print(json.dumps(itr["value"], ensure_ascii=False, indent=2)[:600])
        # 这里模拟人点了 "approve"；真实 UI 由用户点击
        client.runs.wait(tid, ASSISTANT, command={"resume": {"action": "approve"}})

    final = client.threads.get_state(tid)["values"]
    print("\n=== events ===")
    print(" -> ".join(final.get("events", [])))
    print(f"\n=== result ===\n{final.get('result')}")


if __name__ == "__main__":
    main()
