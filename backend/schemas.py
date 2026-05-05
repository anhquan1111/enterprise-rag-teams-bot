"""
schemas.py - Pydantic Schemas (Data Validation & Serialization)
Tách biệt hoàn toàn với ORM models để kiểm soát dữ liệu vào/ra API.

Quy ước đặt tên:
    <Model>Create  → Body của POST request (tạo mới)
    <Model>Update  → Body của PUT/PATCH request (cập nhật)
    <Model>Response → Kết quả trả về cho client
"""

from datetime import date, datetime
from typing import Any, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, field_validator

from models import DocumentStatus, LeaveStatus, UserRole


# =============================================================================
# BASE CONFIG - Cho phép ORM model → Pydantic (from_attributes)
# =============================================================================

class ORMBase(BaseModel):
    """Base class với from_attributes=True để đọc từ SQLAlchemy ORM objects."""
    model_config = ConfigDict(from_attributes=True)


# =============================================================================
# SCHEMAS: Authentication (Mock Azure AD)
# =============================================================================

class MockLoginRequest(BaseModel):
    """
    Body request cho POST /auth/mock-login.
    Giả lập thông tin từ Azure AD SSO token.
    """
    email: EmailStr
    full_name: str
    department: Optional[str] = None
    role: UserRole = UserRole.user

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "nguyen.van.a@company.com",
                "full_name": "Nguyễn Văn A",
                "department": "Phòng Kỹ thuật",
                "role": "user",
            }
        }
    )


class TokenResponse(BaseModel):
    """Response trả về sau khi đăng nhập thành công."""
    access_token: str
    token_type: str = "bearer"
    expires_in: int       # Số giây đến khi token hết hạn
    user: "UserResponse"  # Thông tin user đính kèm


# =============================================================================
# SCHEMAS: Users
# =============================================================================

class UserCreate(BaseModel):
    """Tạo user mới (Admin only)."""
    email: EmailStr
    full_name: str
    department: Optional[str] = None
    role: UserRole = UserRole.user

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "email": "tran.thi.b@company.com",
                "full_name": "Trần Thị B",
                "department": "Phòng Kế toán",
                "role": "user",
            }
        }
    )


class UserUpdate(BaseModel):
    """Cập nhật thông tin user. Tất cả field đều optional."""
    full_name: Optional[str] = None
    department: Optional[str] = None
    role: Optional[UserRole] = None
    is_active: Optional[bool] = None


class UserResponse(ORMBase):
    """Thông tin user trả về cho client (loại bỏ dữ liệu nhạy cảm)."""
    id: UUID
    email: str
    full_name: str
    department: Optional[str]
    role: UserRole
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "email": "nguyen.van.a@company.com",
                "full_name": "Nguyễn Văn A",
                "department": "Phòng Kỹ thuật",
                "role": "user",
                "is_active": True,
                "created_at": "2024-01-15T08:00:00Z",
                "updated_at": "2024-01-15T08:00:00Z",
            }
        }
    )


class UserListResponse(BaseModel):
    """Danh sách users với phân trang."""
    items: List[UserResponse]
    total: int
    page: int
    page_size: int


# =============================================================================
# SCHEMAS: Documents
# =============================================================================

class DocumentCreate(BaseModel):
    """Tạo document record (thường nội bộ, sau khi upload file)."""
    filename: str
    file_path: str
    vector_collection_name: Optional[str] = None

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "filename": "quy_trinh_nghi_phep_2024.pdf",
                "file_path": "/app/uploads/quy_trinh_nghi_phep_2024.pdf",
            }
        }
    )


class DocumentUpdate(BaseModel):
    """Cập nhật metadata tài liệu (dùng bởi Celery task)."""
    status: Optional[DocumentStatus] = None
    vector_collection_name: Optional[str] = None
    chunk_count: Optional[int] = None
    error_message: Optional[str] = None


class DocumentResponse(ORMBase):
    """Thông tin tài liệu trả về cho client."""
    id: UUID
    filename: str
    file_path: str
    status: DocumentStatus
    upload_time: datetime
    vector_collection_name: Optional[str]
    chunk_count: Optional[int]
    error_message: Optional[str]
    uploaded_by: Optional[UUID]

    model_config = ConfigDict(
        from_attributes=True,
        json_schema_extra={
            "example": {
                "id": "660e8400-e29b-41d4-a716-446655440001",
                "filename": "quy_trinh_nghi_phep_2024.pdf",
                "file_path": "/app/uploads/quy_trinh_nghi_phep_2024.pdf",
                "status": "pending",
                "upload_time": "2024-01-15T09:00:00Z",
                "vector_collection_name": None,
                "chunk_count": None,
                "error_message": None,
                "uploaded_by": "550e8400-e29b-41d4-a716-446655440000",
            }
        }
    )


class DocumentListResponse(BaseModel):
    """Danh sách tài liệu với phân trang."""
    items: List[DocumentResponse]
    total: int
    page: int
    page_size: int


class DocumentUploadResponse(BaseModel):
    """
    Response trả về ngay sau khi nhận file upload thành công (HTTP 202).
    Celery task sẽ xử lý bất đồng bộ - client poll GET /api/documents/{document_id}
    để theo dõi trạng thái.
    """
    document_id: UUID
    task_id: str
    filename: str
    status: str
    message: str

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "document_id": "660e8400-e29b-41d4-a716-446655440001",
                "task_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "filename": "quy_trinh_nghi_phep_2024.pdf",
                "status": "pending",
                "message": "File đã được nhận. Đang xếp hàng xử lý...",
            }
        }
    )


# =============================================================================
# SCHEMAS: LeaveRequests
# =============================================================================

class LeaveRequestCreate(BaseModel):
    """Tạo đơn xin nghỉ phép mới."""
    start_date: date
    end_date: date
    reason: str

    @field_validator("end_date")
    @classmethod
    def end_date_must_be_after_start(cls, end_date: date, info) -> date:
        """Kiểm tra ngày kết thúc phải sau hoặc bằng ngày bắt đầu."""
        if "start_date" in info.data and end_date < info.data["start_date"]:
            raise ValueError("Ngày kết thúc phải sau hoặc bằng ngày bắt đầu")
        return end_date

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "start_date": "2024-02-01",
                "end_date": "2024-02-03",
                "reason": "Nghỉ phép năm theo kế hoạch",
            }
        }
    )


class LeaveRequestStatusUpdate(BaseModel):
    """Admin cập nhật trạng thái đơn nghỉ phép."""
    status: LeaveStatus
    review_note: Optional[str] = None

    @field_validator("status")
    @classmethod
    def status_must_not_be_pending(cls, v: LeaveStatus) -> LeaveStatus:
        """Admin chỉ có thể set approved hoặc rejected."""
        if v == LeaveStatus.pending:
            raise ValueError("Admin phải chọn approved hoặc rejected")
        return v


class LeaveRequestResponse(ORMBase):
    """Thông tin đơn nghỉ phép trả về cho client."""
    id: UUID
    user_id: UUID
    start_date: date
    end_date: date
    reason: str
    status: LeaveStatus
    created_at: datetime
    reviewed_by: Optional[UUID]
    reviewed_at: Optional[datetime]
    review_note: Optional[str]

    # Số ngày nghỉ (tính toán từ start_date và end_date)
    @property
    def days_count(self) -> int:
        return (self.end_date - self.start_date).days + 1


class LeaveRequestListResponse(BaseModel):
    """Danh sách đơn nghỉ phép."""
    items: List[LeaveRequestResponse]
    total: int


# =============================================================================
# SCHEMAS: Chat (Phase 4) — RAG Chatbot
# =============================================================================

class ChatRequest(BaseModel):
    """Body request cho POST /api/chat."""
    message: str
    session_id: Optional[UUID] = None  # Truyền để tiếp tục hội thoại multi-turn

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "message": "Quy trình xin nghỉ phép như thế nào?",
                "session_id": None,
            }
        }
    )


# =============================================================================
# SCHEMAS: ChatSessions
# =============================================================================

class ChatSessionCreate(BaseModel):
    """Tạo phiên chat mới."""
    context_json: Optional[List[Any]] = None


class ChatSessionResponse(ORMBase):
    """Thông tin phiên chat trả về cho client."""
    id: UUID
    user_id: UUID
    start_time: datetime
    ended_at: Optional[datetime]
    context_json: Optional[List[Any]]


# =============================================================================
# SCHEMAS: Health Check
# =============================================================================

class HealthResponse(BaseModel):
    """Kết quả health check của hệ thống."""
    status: str
    version: str
    database: str
    services: dict


# Update forward references
TokenResponse.model_rebuild()
