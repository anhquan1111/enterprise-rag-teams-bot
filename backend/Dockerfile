# =============================================================================
# Dockerfile - Backend FastAPI
# Base: Python 3.11 slim (nhỏ gọn, đủ dùng)
# =============================================================================

FROM python:3.11-slim

# Đặt working directory trong container
WORKDIR /app

# Cài đặt system dependencies
# gcc / g++   : compile C/C++ extensions (psycopg2, hnswlib/chroma-hnswlib cho chromadb)
# curl        : healthcheck trong Dockerfile HEALTHCHECK directive
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements trước để tận dụng Docker layer cache
# (chỉ rebuild khi requirements.txt thay đổi)
COPY requirements.txt .

# Cài đặt Python dependencies
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy toàn bộ source code
COPY . .

# Expose port FastAPI
EXPOSE 8000

# Health check: gọi /health endpoint
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -sf http://localhost:8000/health || exit 1

# Chạy uvicorn với --reload cho development
# Trong production: bỏ --reload và tăng --workers
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload", "--log-level", "info"]
