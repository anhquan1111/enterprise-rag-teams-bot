"""
database.py - Kết nối SQLAlchemy với PostgreSQL
Khởi tạo engine, SessionLocal và Base declarative class.
"""

import logging
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from sqlalchemy.exc import OperationalError

from config import settings

logger = logging.getLogger(__name__)


# =============================================================================
# ENGINE - Kết nối đến PostgreSQL
# pool_pre_ping=True: kiểm tra kết nối trước mỗi lần dùng (tránh stale connections)
# pool_size=10: số kết nối tối đa trong pool
# max_overflow=20: cho phép vượt pool_size thêm 20 kết nối khi cần
# =============================================================================
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    echo=settings.DEBUG,   # Log SQL queries khi DEBUG=True
)

# =============================================================================
# SESSION - Factory tạo database sessions
# autocommit=False: phải gọi db.commit() thủ công (kiểm soát transaction)
# autoflush=False: không tự flush trước mỗi query (hiệu suất tốt hơn)
# =============================================================================
SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
)


# =============================================================================
# BASE - Base class cho tất cả SQLAlchemy models
# =============================================================================
class Base(DeclarativeBase):
    """
    Base class kế thừa bởi tất cả ORM models.
    Dùng DeclarativeBase mới của SQLAlchemy 2.0.
    """
    pass


# =============================================================================
# DEPENDENCY - FastAPI Dependency Injection cho database session
# =============================================================================
def get_db():
    """
    Generator function dùng làm FastAPI dependency.
    Mỗi request nhận một session riêng, tự động đóng sau khi xong.

    Cách dùng trong endpoint:
        def my_endpoint(db: Session = Depends(get_db)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def verify_db_connection() -> bool:
    """
    Kiểm tra kết nối PostgreSQL có hoạt động không.
    Được gọi khi khởi động ứng dụng để phát hiện lỗi sớm.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("Kết nối PostgreSQL thành công tại: %s:%d/%s",
                    settings.POSTGRES_HOST, settings.POSTGRES_PORT, settings.POSTGRES_DB)
        return True
    except OperationalError as e:
        logger.error("Không thể kết nối PostgreSQL: %s", str(e))
        return False
