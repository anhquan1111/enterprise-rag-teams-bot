"""
models.py - SQLAlchemy ORM Models (ERD)
Định nghĩa toàn bộ schema cơ sở dữ liệu cho hệ thống.

Bảng:
    - users          : Tài khoản người dùng (đồng bộ từ Azure AD)
    - documents      : Tài liệu tải lên (được xử lý bởi RAG pipeline)
    - leave_requests : Đơn xin nghỉ phép
    - chat_sessions  : Lịch sử phiên chat với AI
"""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Enum, ForeignKey,
    Integer, JSON, String, Text, func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import relationship

from database import Base


# =============================================================================
# ENUMS - Các giá trị cố định cho cột kiểu Enum
# =============================================================================

class UserRole(str, enum.Enum):
    """Vai trò người dùng trong hệ thống."""
    admin = "admin"   # Quản trị viên: duyệt đơn, upload tài liệu, xem tất cả
    user = "user"     # Nhân viên thông thường: chat AI, xin nghỉ phép


class DocumentStatus(str, enum.Enum):
    """Trạng thái xử lý tài liệu trong pipeline RAG."""
    pending    = "pending"     # Vừa upload, chưa xử lý
    processing = "processing"  # Celery task đang chạy (OCR + Chunking + Embedding)
    done       = "done"        # Đã đẩy vào ChromaDB/LocalRecall thành công
    failed     = "failed"      # Xử lý thất bại (xem error_message)


class LeaveStatus(str, enum.Enum):
    """Trạng thái đơn xin nghỉ phép."""
    pending  = "pending"   # Chờ duyệt
    approved = "approved"  # Đã duyệt bởi Admin
    rejected = "rejected"  # Bị từ chối bởi Admin


# =============================================================================
# MODEL: Users
# Đồng bộ thông tin từ Azure AD khi user đăng nhập lần đầu (hoặc mock login).
# =============================================================================

class User(Base):
    """
    Bảng người dùng hệ thống.
    Được tạo/cập nhật tự động từ JWT token Azure AD khi user xác thực.
    """
    __tablename__ = "users"

    # Dùng UUID thay vì integer ID để tăng bảo mật và tránh ID enumeration
    id = Column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        index=True,
    )
    email = Column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
        comment="Email Azure AD - dùng làm định danh chính",
    )
    full_name = Column(
        String(255),
        nullable=False,
        comment="Họ và tên đầy đủ (lấy từ Azure AD token)",
    )
    department = Column(
        String(100),
        nullable=True,
        comment="Phòng ban (VD: Kế toán, Kỹ thuật, HR)",
    )
    role = Column(
        Enum(UserRole, name="user_role_enum"),
        nullable=False,
        default=UserRole.user,
        server_default=UserRole.user.value,
        comment="Vai trò: admin hoặc user",
    )
    is_active = Column(
        Boolean,
        nullable=False,
        default=True,
        server_default="true",
        comment="Tài khoản có đang hoạt động không",
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Thời điểm tạo tài khoản lần đầu",
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="Thời điểm cập nhật gần nhất",
    )

    # --- Relationships ---
    documents     = relationship("Document",     back_populates="uploader",       foreign_keys="Document.uploaded_by")
    leave_requests = relationship("LeaveRequest", back_populates="user",           foreign_keys="LeaveRequest.user_id")
    reviewed_requests = relationship("LeaveRequest", back_populates="reviewer",   foreign_keys="LeaveRequest.reviewed_by")
    chat_sessions = relationship("ChatSession",  back_populates="user")

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email} role={self.role}>"


# =============================================================================
# MODEL: Documents
# Metadata của tài liệu PDF/Word được tải lên để đưa vào RAG pipeline.
# =============================================================================

class Document(Base):
    """
    Bảng metadata tài liệu.
    File thực được lưu trên disk; embedding được đẩy vào ChromaDB/LocalRecall.
    """
    __tablename__ = "documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    filename = Column(
        String(500),
        nullable=False,
        comment="Tên file gốc khi upload (VD: quy_trinh_nghi_phep.pdf)",
    )
    file_path = Column(
        String(1000),
        nullable=False,
        comment="Đường dẫn lưu file trên server (VD: /app/uploads/uuid/file.pdf)",
    )
    status = Column(
        Enum(DocumentStatus, name="document_status_enum"),
        nullable=False,
        default=DocumentStatus.pending,
        server_default=DocumentStatus.pending.value,
        index=True,
        comment="Trạng thái xử lý trong pipeline RAG",
    )
    upload_time = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Thời điểm upload file",
    )
    vector_collection_name = Column(
        String(255),
        nullable=True,
        comment="Tên collection trong ChromaDB/LocalRecall (sau khi xử lý xong)",
    )
    chunk_count = Column(
        Integer,
        nullable=True,
        comment="Số lượng chunks sau khi Semantic Chunking",
    )
    error_message = Column(
        Text,
        nullable=True,
        comment="Thông báo lỗi nếu pipeline thất bại",
    )
    uploaded_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="ID user đã upload tài liệu này",
    )

    # --- Relationships ---
    uploader = relationship("User", back_populates="documents", foreign_keys=[uploaded_by])

    def __repr__(self) -> str:
        return f"<Document id={self.id} filename={self.filename} status={self.status}>"


# =============================================================================
# MODEL: LeaveRequests
# Đơn xin nghỉ phép gửi từ Teams Bot; Admin duyệt trực tiếp trên Teams.
# =============================================================================

class LeaveRequest(Base):
    """
    Bảng đơn xin nghỉ phép.
    Được tạo khi user nhập lệnh /xin-nghi trong Teams và điền Adaptive Card.
    """
    __tablename__ = "leave_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="ID người dùng gửi đơn",
    )
    start_date = Column(
        Date,
        nullable=False,
        comment="Ngày bắt đầu nghỉ",
    )
    end_date = Column(
        Date,
        nullable=False,
        comment="Ngày kết thúc nghỉ (bao gồm ngày này)",
    )
    reason = Column(
        Text,
        nullable=False,
        comment="Lý do xin nghỉ phép",
    )
    status = Column(
        Enum(LeaveStatus, name="leave_status_enum"),
        nullable=False,
        default=LeaveStatus.pending,
        server_default=LeaveStatus.pending.value,
        index=True,
        comment="Trạng thái đơn: pending/approved/rejected",
    )
    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Thời điểm nộp đơn",
    )
    reviewed_by = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        comment="ID admin đã duyệt/từ chối",
    )
    reviewed_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Thời điểm admin xử lý đơn",
    )
    review_note = Column(
        Text,
        nullable=True,
        comment="Ghi chú của admin khi duyệt/từ chối",
    )

    # --- Relationships ---
    user     = relationship("User", back_populates="leave_requests",  foreign_keys=[user_id])
    reviewer = relationship("User", back_populates="reviewed_requests", foreign_keys=[reviewed_by])

    def __repr__(self) -> str:
        return f"<LeaveRequest id={self.id} user_id={self.user_id} status={self.status}>"


# =============================================================================
# MODEL: ChatSessions
# Lịch sử phiên hội thoại với AI, lưu context để multi-turn conversation.
# =============================================================================

class ChatSession(Base):
    """
    Bảng phiên chat.
    Mỗi phiên chat lưu lịch sử hội thoại dạng JSON để AI nhớ ngữ cảnh.
    """
    __tablename__ = "chat_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4, index=True)
    user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="ID người dùng sở hữu phiên chat này",
    )
    start_time = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="Thời điểm bắt đầu phiên chat",
    )
    ended_at = Column(
        DateTime(timezone=True),
        nullable=True,
        comment="Thời điểm kết thúc phiên (None = đang active)",
    )
    context_json = Column(
        JSON,
        nullable=True,
        default=list,
        comment="Lịch sử hội thoại: [{role: 'user'|'assistant', content: '...'}]",
    )

    # --- Relationships ---
    user = relationship("User", back_populates="chat_sessions")

    def __repr__(self) -> str:
        return f"<ChatSession id={self.id} user_id={self.user_id}>"
