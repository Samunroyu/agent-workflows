"""工作流共享 state。

原则：
- state 只放「节点之间要传递的数据」和「便于回放的事件日志」，不放连接、句柄等不可序列化对象
- 用 Annotated[..., reducer] 声明合并语义，默认是覆盖（后写覆盖前写）
"""

import operator
from typing import Annotated, TypedDict


class CodingState(TypedDict, total=False):
    # ---- 输入 ----
    task: str            # 要做什么
    repo: str            # 工作目录
    test_cmd: str        # 测试命令（在 repo 里执行，退出码 0 = 通过）
    max_attempts: int    # 测试-修复循环上限

    # ---- 各阶段产物 ----
    plan: str            # 当前计划（被 reject 会重新生成）
    feedback: str        # 人工对计划的反馈
    action: str          # 最近一次人审动作：approve / revise / abort
    approved: bool       # 计划是否通过
    attempt: int         # 第几次实现尝试
    patch: str           # implement 产出的改动
    test_output: str     # test 的输出
    passed: bool         # 测试是否通过
    review: str          # review 结论
    result: str          # 最终结果（成功/失败/中止）

    # ---- 事件日志：只追加 ----
    events: Annotated[list[str], operator.add]
