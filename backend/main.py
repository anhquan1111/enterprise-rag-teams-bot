"""
main.py - Điểm khởi động ứng dụng FastAPI

Khởi tạo ứng dụng, kết nối các routers, và xử lý lifecycle events.

Startup Event:
    1. Kiểm tra kết nối PostgreSQL
    2. Tự động tạo tất cả tables (Base.metadata.create_all)
    3. Log thông tin cấu hình

Cách chạy local (không Docker):
    uvicorn main:app --reload --port 8000

Cách chạy với Docker:
    docker compose up backend
"""

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

# Import cấu hình và database
from config import settings
from database import Base, engine, verify_db_connection

# Import tất cả models để Base.metadata biết về chúng khi create_all
import models  # noqa: F401 - import đủ để register models vào Base.metadata

# Import routers
import auth
from routers import chat, documents, leave_requests, users

# =============================================================================
# LOGGING - Cấu hình ghi log
# =============================================================================
logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# =============================================================================
# LIFESPAN - Startup & Shutdown events
# =============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Quản lý vòng đời ứng dụng:
    - Yield trước: logic chạy khi khởi động (startup)
    - Yield sau: logic chạy khi tắt (shutdown)
    """
    # =========================================================================
    # STARTUP
    # =========================================================================
    logger.info("=" * 60)
    logger.info("Hệ thống AI Trợ Lý Hành Chính Văn Phòng đang khởi động...")
    logger.info("Environment: %s", settings.APP_ENV)
    logger.info("Database: %s@%s:%d/%s",
                settings.POSTGRES_USER, settings.POSTGRES_HOST,
                settings.POSTGRES_PORT, settings.POSTGRES_DB)
    logger.info("=" * 60)

    # Kiểm tra kết nối PostgreSQL (fail fast nếu DB không khả dụng)
    if not verify_db_connection():
        logger.critical("Không thể kết nối PostgreSQL! Dừng ứng dụng.")
        sys.exit(1)

    # Tự động tạo tất cả tables nếu chưa tồn tại
    # Trong production nên dùng Alembic migrations thay vì create_all
    logger.info("Kiểm tra và tạo database tables...")
    Base.metadata.create_all(bind=engine)

    # Migration shim: ADD COLUMN IF NOT EXISTS cho các DB đã tồn tại trước khi
    # cột mới được merge. Postgres 9.6+ hỗ trợ IF NOT EXISTS → idempotent, chạy
    # mỗi lần startup an toàn. Dùng tạm cho đến khi project di chuyển sang Alembic.
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE documents "
            "ADD COLUMN IF NOT EXISTS localrecall_indexed BOOLEAN NOT NULL DEFAULT FALSE"
        ))

    logger.info("Database tables đã sẵn sàng.")
    logger.info("API docs tại: http://localhost:8000/docs")
    logger.info("=" * 60)

    yield  # Ứng dụng đang chạy (serving requests)

    # =========================================================================
    # SHUTDOWN
    # =========================================================================
    logger.info("Đang dừng ứng dụng...")
    engine.dispose()  # Đóng tất cả connections trong pool
    logger.info("Ứng dụng đã dừng.")


# =============================================================================
# FASTAPI APP - Khởi tạo với metadata
# =============================================================================

app = FastAPI(
    title="AI Trợ Lý Hành Chính Văn Phòng",
    description=(
        "## Hệ thống AI Trợ Lý Hành Chính Văn Phòng\n\n"
        "Backend API cho hệ thống chatbot AI tích hợp Microsoft Teams.\n\n"
        "### Tính năng chính:\n"
        "- **RAG Chatbot**: Trả lời câu hỏi từ tài liệu nội bộ\n"
        "- **Xin nghỉ phép**: Quản lý đơn xin nghỉ qua Teams Bot\n"
        "- **Upload tài liệu**: Xử lý PDF/Word với Semantic Chunking\n\n"
        "### Authentication:\n"
        "Dùng `POST /auth/mock-login` để nhận Bearer token, sau đó click **Authorize** ở trên."
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)


# =============================================================================
# MIDDLEWARE
# =============================================================================

# CORS: Cho phép Teams Bot và frontend gọi API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # Trong production: giới hạn domain cụ thể
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# ROUTERS - Đăng ký tất cả route groups
# =============================================================================

# Authentication routes (mock Azure AD)
app.include_router(
    auth.router,
    prefix="/auth",
    tags=["🔐 Authentication (Mock Azure AD)"],
)

# User management
app.include_router(
    users.router,
    prefix="/api/users",
    tags=["👥 Users"],
)

# Document management (Phase 2: CRUD; Phase 3: upload + RAG)
app.include_router(
    documents.router,
    prefix="/api/documents",
    tags=["📄 Documents"],
)

# Leave request management
app.include_router(
    leave_requests.router,
    prefix="/api/leave-requests",
    tags=["🏖️ Leave Requests (Đơn nghỉ phép)"],
)

# Chat RAG endpoint (Phase 4)
app.include_router(
    chat.router,
    prefix="/api/chat",
    tags=["💬 Chat (RAG)"],
)


# =============================================================================
# ROOT & HEALTH CHECK ENDPOINTS
# =============================================================================

@app.get("/", include_in_schema=False)
async def root():
    """Redirect hint về docs."""
    return JSONResponse({
        "message": "AI Trợ Lý Hành Chính Văn Phòng API",
        "docs": "/docs",
        "health": "/health",
        "version": "1.0.0",
    })


@app.get(
    "/health",
    tags=["🏥 Health"],
    summary="Kiểm tra trạng thái hệ thống",
)
async def health_check():
    """
    Health check endpoint.
    Kiểm tra kết nối PostgreSQL và trả về trạng thái các service.
    """
    db_status = "ok" if verify_db_connection() else "error"

    return {
        "status": "ok" if db_status == "ok" else "degraded",
        "version": "1.0.0",
        "environment": settings.APP_ENV,
        "services": {
            "database": db_status,
            "ollama_host": settings.OLLAMA_HOST,
            "localrecall_host": settings.LOCALRECALL_HOST,
        },
    }
