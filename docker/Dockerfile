# ============================================================
# IDN-System 应用镜像
# FastAPI + LangGraph + MCP Servers
# 构建上下文: 项目根目录
# ============================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python

WORKDIR /app

# ── 系统依赖 ──
RUN apt-get update && apt-get install -y \
    gcc g++ \
    && rm -rf /var/lib/apt/lists/*

# ── Python 依赖 (分层: 先装依赖, 代码变更不会 invalidate 缓存) ──
COPY idn-system/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# ── 应用代码 ──
COPY idn-system/ /app/
COPY p4/build/ /app/p4/build/
# ResourceRegistry resolves the formal Mininet topology during application
# import.  Development normally sees this through the workspace bind mount;
# a production image must carry the same topology definition itself.
COPY mininet/topologies/ /app/mininet/topologies/

# ── 数据目录 ──
RUN mkdir -p /app/data

EXPOSE 8000

CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
