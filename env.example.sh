# 在项目根目录执行：cp env.example.sh env.sh && source env.sh
# env.sh 是本地配置，已加入 .gitignore。
export LG_HOME="${LG_HOME:-$PWD}"
export UV_CACHE_DIR="$LG_HOME/cache"
export UV_PYTHON_INSTALL_DIR="$LG_HOME/python"
export VIRTUAL_ENV="$LG_HOME/.venv"
export PATH="$LG_HOME/bin:$LG_HOME/.venv/bin:$PATH"

# 国内镜像（按需启用）
# export UV_PYTHON_INSTALL_MIRROR="https://mirror.nju.edu.cn/github-release/astral-sh/python-build-standalone"
# export UV_DEFAULT_INDEX="https://pypi.tuna.tsinghua.edu.cn/simple"
# export PIP_INDEX_URL="$UV_DEFAULT_INDEX"

# Langfuse 追踪（可选）：填入自己的服务地址和项目密钥。
# 留空时不启用追踪；Langfuse SDK 需单独安装。
# export LANGFUSE_HOST="http://localhost:3000"
# export LANGFUSE_PUBLIC_KEY=""
# export LANGFUSE_SECRET_KEY=""
