#!/usr/bin/env bash
# 从零重建本项目的 LangGraph 环境（macOS arm64 + 国内网络）。
# 用法：bash bootstrap.sh
set -euo pipefail

LG_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY_VER="3.12.15"
PBS_TAG="20261020"   # NJU 镜像上的 tag，比 uv 内置清单滞后约一天
PBS_URL="https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone/${PBS_TAG}/cpython-${PY_VER}%2B${PBS_TAG}-aarch64-apple-darwin-install_only.tar.gz"
PYPI="https://pypi.tuna.tsinghua.edu.cn/simple"

mkdir -p "$LG_HOME"/{bin,python,dl,cache}

# 1) uv：优先复用本目录已有的，否则从 PyPI wheel 里取二进制（GitHub 直连太慢）
if [ ! -x "$LG_HOME/bin/uv" ]; then
  echo ">>> 获取 uv"
  python3 -m pip download uv --no-deps -d "$LG_HOME/dl" -i "$PYPI"
  rm -rf "$LG_HOME/dl/uvwheel"
  unzip -oq "$LG_HOME"/dl/uv-*.whl -d "$LG_HOME/dl/uvwheel"
  cp "$(find "$LG_HOME/dl/uvwheel" -type f -name uv -perm -u+x | head -1)" "$LG_HOME/bin/uv"
  chmod +x "$LG_HOME/bin/uv"
fi

export UV_CACHE_DIR="$LG_HOME/cache"
export UV_DEFAULT_INDEX="$PYPI"
export UV_INDEX_URL="$PYPI"
export UV_PYTHON_INSTALL_DIR="$LG_HOME/python"

# 2) CPython standalone
if [ ! -x "$LG_HOME/python/python/bin/python3" ]; then
  echo ">>> 获取 CPython ${PY_VER}"
  curl -fsSL -o "$LG_HOME/dl/cpython.tar.gz" "$PBS_URL"
  tar -xzf "$LG_HOME/dl/cpython.tar.gz" -C "$LG_HOME/python"
fi

# 3) venv + 依赖
echo ">>> 创建 .venv 并安装依赖"
"$LG_HOME/bin/uv" venv --python "$LG_HOME/python/python/bin/python3" "$LG_HOME/.venv"
"$LG_HOME/bin/uv" pip install --python "$LG_HOME/.venv/bin/python" \
  langgraph langchain langgraph-checkpoint-sqlite "langgraph-cli[inmem]" \
  python-dotenv httpx pytest
"$LG_HOME/bin/uv" pip freeze --python "$LG_HOME/.venv/bin/python" > "$LG_HOME/requirements.lock.txt"

# 4) 验证
"$LG_HOME/.venv/bin/python" "$LG_HOME/examples/verify_graph.py"
echo ">>> 完成。使用前：source $LG_HOME/env.sh"
