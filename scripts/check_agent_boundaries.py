"""检查 Git 工作区改动是否符合前后端 Agent 目录隔离规范。

用法：
    python scripts/check_agent_boundaries.py --role frontend   # 仅允许改动 frontend/
    python scripts/check_agent_boundaries.py --role backend    # 禁止改动 frontend/（AGENTS.md 除外）
    python scripts/check_agent_boundaries.py --role frontend --revert-violations  # 自动回滚越界文件
"""

import argparse
import os
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def changed_files(repo: str) -> list[str]:
    out = subprocess.run(
        ["git", "-C", repo, "status", "--porcelain", "-uall"],
        capture_output=True,
        text=True,
        check=False,
    )
    files: list[str] = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        # porcelain v1: XY PATH or XY ORIG -> PATH
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        files.append(path.strip('"'))
    return files


def is_allowed(path: str, role: str) -> bool:
    norm = path.replace("\\", "/").lstrip("./")
    if role == "frontend":
        return norm == "frontend" or norm.startswith("frontend/")
    if role == "backend":
        if norm == "frontend/AGENTS.md":
            return True
        return not (norm == "frontend" or norm.startswith("frontend/"))
    return True


def revert_path(repo: str, path: str) -> None:
    subprocess.run(["git", "-C", repo, "checkout", "--", path], check=False)
    subprocess.run(["git", "-C", repo, "clean", "-fd", "--", path], check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="前后端 Agent 改动边界校验")
    parser.add_argument("--role", choices=["frontend", "backend"], required=True)
    parser.add_argument("--repo", default=ROOT)
    parser.add_argument("--revert-violations", action="store_true", help="自动还原越界改动")
    args = parser.parse_args()

    files = changed_files(args.repo)
    violations = [f for f in files if not is_allowed(f, args.role)]

    if not violations:
        print(f"[ok] role={args.role}: 所有改动均在允许目录范围内 ({len(files)} files)")
        return 0

    print(f"[violation] role={args.role} 存在越界文件改动:", file=sys.stderr)
    for v in violations:
        print(f"  - {v}", file=sys.stderr)
        if args.revert_violations:
            revert_path(args.repo, v)
            print(f"    -> 已自动回滚: {v}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
