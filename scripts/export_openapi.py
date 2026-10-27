"""导出后端 FastAPI OpenAPI 契约到 shared/openapi.json。

前后端分离约定：
- 后端 Agent 修改 console/app.py 接口或 Pydantic 模型后，必须运行本脚本更新 shared/openapi.json。
- 前端 Agent (Kimi) 仅以 shared/openapi.json 作为接口与类型唯一契约，无需读取后端 Python 源码。

用法：
    python scripts/export_openapi.py
"""

import json
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from console.app import app  # noqa: E402


def main() -> None:
    out_dir = os.path.join(ROOT, "shared")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "openapi.json")
    schema = app.openapi()
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(schema, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"[ok] exported OpenAPI schema -> {out_path}")


if __name__ == "__main__":
    main()
