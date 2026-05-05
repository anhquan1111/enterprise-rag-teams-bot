"""
config.py - Cấu hình ứng dụng từ biến môi trường
Sử dụng pydantic-settings để tự động đọc từ environment variables (hoặc file .env).
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache


class Settings(BaseSettings):
    """
    Toàn bộ cấu hình hệ thống được khai báo tập trung ở đây.
    Giá trị được đọc từ environment variables; nếu không có thì dùng default.
    """

    # --- PostgreSQL ---
    POSTGRES_USER: str = "qlda_user"
    POSTGRES_PASSWORD: str = "qlda_secure_pass_2024"
    POSTGRES_DB: str = "qlda_db"
    POSTGRES_HOST: str = "postgres"          # Tên service trong Docker network
    POSTGRES_PORT: int = 5432

    # --- Redis ---
    REDIS_HOST: str = "redis"               # Tên service trong Docker network
    REDIS_PORT: int = 6379

    # --- Ollama ---
    OLLAMA_HOST: str = "http://ollama:11434"
    OLLAMA_MODEL: str = "qwen2.5:7b"

    # --- LocalRecall (kept for search compatibility) ---
    LOCALRECALL_HOST: str = "http://localrecall:8080"
    LOCALRECALL_COLLECTION: str = "qlda_documents"   # Tên collection dùng chung cho mọi tài liệu

    # --- ChromaDB (vector storage — internal Docker hostname) ---
    CHROMADB_HOST: str = "chromadb"   # Service name trong Docker network
    CHROMADB_PORT: int = 8000          # Internal port (host port là 8001)

    # --- Upload ---
    UPLOAD_DIR: str = "/app/uploads"                 # Thư mục lưu file tạm (dùng chung backend & worker)

    # --- JWT (Mock Azure AD) ---
    # Secret key dùng để ký và xác thực JWT token (đổi thành giá trị bí mật trong production)
    JWT_SECRET_KEY: str = "MOCK_AZURE_AD_SECRET_KEY_CHANGE_IN_PRODUCTION_2024"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 480           # Token hết hạn sau 8 giờ

    # --- App ---
    APP_ENV: str = "development"
    DEBUG: bool = True

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",                     # Bỏ qua các env vars không khai báo
    )

    @property
    def DATABASE_URL(self) -> str:
        """Tạo connection string PostgreSQL từ các thành phần riêng lẻ."""
        return (
            f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def REDIS_URL(self) -> str:
        """URL kết nối Redis dạng chuẩn."""
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/0"


@lru_cache()
def get_settings() -> Settings:
    """
    Trả về singleton Settings instance (cache bằng lru_cache).
    Dùng Depends(get_settings) trong FastAPI endpoints để inject settings.
    """
    return Settings()


# Singleton instance dùng ở module level (database.py, main.py, v.v.)
settings = get_settings()
