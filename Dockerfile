FROM python:3.13.2-slim

# 1. 创建非 root 用户并设置工作目录
RUN useradd -m appuser
WORKDIR /app

# Set non-sensitive environment variables
ARG APP_ENV=production

ENV APP_ENV=${APP_ENV} \
    PYTHONFAULTHANDLER=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONHASHSEED=random \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=on \
    PIP_DEFAULT_TIMEOUT=100

# 2. 尝试切换 apt 源为清华镜像加速（如果失败则保留默认官方源）
RUN (sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list 2>/dev/null) || true

# 3. 安装系统依赖与 uv
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/ \
    && pip install uv -i https://mirrors.aliyun.com/pypi/simple/

# 4. 修改 /app 目录归属并切换为 appuser 用户
RUN chown -R appuser:appuser /app
USER appuser

ENV UV_HTTP_TIMEOUT=120

# 5. 使用 appuser 身份拷贝并安装依赖（利用 --chown 避免后续的大规模磁盘改写）
COPY --chown=appuser:appuser pyproject.toml uv.lock ./
RUN uv sync --default-index https://pypi.tuna.tsinghua.edu.cn/simple

# 6. 拷贝应用源码
COPY --chown=appuser:appuser . .

# 赋予入口脚本执行权限并创建日志目录
RUN chmod +x /app/scripts/docker-entrypoint.sh && mkdir -p /app/logs

# Default port
EXPOSE 8000

# Command to run the application
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
CMD ["/app/.venv/bin/uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
