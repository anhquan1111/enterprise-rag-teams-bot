"""
celery_app.py - Khởi tạo Celery application

Celery sử dụng Redis làm message broker VÀ result backend.
Các task được định nghĩa trong tasks.py và được import qua `include`.

Cách dùng:
    # Khởi động worker (trong container hoặc local):
    celery -A celery_app worker --loglevel=info --concurrency=2
"""

from celery import Celery

from config import settings

# Khởi tạo Celery app với tên "qlda_worker"
# broker: Redis nhận và phân phối task message
# backend: Redis lưu trạng thái và kết quả task
celery_app = Celery(
    "qlda_worker",
    broker=settings.REDIS_URL,
    backend=settings.REDIS_URL,
    include=["tasks"],  # Module chứa các @celery_app.task
)

celery_app.conf.update(
    # --- Serialization ---
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],

    # --- Timezone ---
    timezone="Asia/Ho_Chi_Minh",
    enable_utc=True,

    # --- Task tracking ---
    task_track_started=True,          # Lưu trạng thái STARTED vào backend
    task_acks_late=True,              # Chỉ ack sau khi task hoàn thành (tránh mất task khi worker crash)
    worker_prefetch_multiplier=1,     # Mỗi worker nhận 1 task tại một thời điểm (phù hợp task nặng)

    # --- Result expiry ---
    result_expires=3600,              # Kết quả task hết hạn sau 1 giờ
)
