"""
routers/documents.py - CRUD endpoints cho Documents

Endpoints:
    POST   /api/documents/upload   → Upload file PDF/DOCX, kick off Celery task (Phase 3)
    GET    /api/documents          → Danh sách tài liệu
    POST   /api/documents          → Tạo document record (admin, metadata only)
    GET    /api/documents/{id}     → Chi tiết tài liệu
    PUT    /api/documents/{id}     → Cập nhật metadata (admin / celery)
    DELETE /api/documents/{id}     → Xóa tài liệu (admin)

LƯU Ý: /upload phải được đăng ký TRƯỚC /{doc_id} để FastAPI không
        nhầm chuỗi "upload" với một UUID hợp lệ.
"""

import logging
import os
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy.orm import Session

from auth import get_current_admin, get_current_user
from config import settings
from database import get_db
from models import Document, DocumentStatus, User, UserRole
from schemas import (
    DocumentCreate, DocumentListResponse, DocumentResponse, DocumentUpdate,
    DocumentUploadResponse,
)
from tasks import process_document_task

logger = logging.getLogger(__name__)
router = APIRouter()

# Các định dạng file được chấp nhận
ALLOWED_EXTENSIONS = {".pdf", ".docx"}
MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB


# =============================================================================
# UPLOAD ENDPOINT (Phase 3) - Phải đứng TRƯỚC /{doc_id}
# =============================================================================

@router.post(
    "/upload",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Upload tài liệu PDF/DOCX để xử lý RAG",
    description=(
        "Nhận file PDF hoặc DOCX, lưu vào thư mục uploads, tạo record trong DB "
        "với status `pending`, sau đó kick off Celery background task để extract "
        "text → chunking → đẩy vector vào LocalRecall.\n\n"
        "**Trả về ngay HTTP 202** (không chờ xử lý xong). "
        "Client poll `GET /api/documents/{document_id}` để theo dõi trạng thái."
    ),
)
async def upload_document(
    file: UploadFile = File(..., description="File PDF hoặc DOCX, tối đa 50MB"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Upload file và kick off Celery pipeline xử lý bất đồng bộ.
    Mọi user đã đăng nhập đều có thể upload (không giới hạn admin).
    """
    # --- Kiểm tra định dạng file ---
    original_filename = file.filename or "unknown"
    ext = Path(original_filename).suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Định dạng file '{ext}' không được hỗ trợ. "
                f"Chỉ chấp nhận: {', '.join(sorted(ALLOWED_EXTENSIONS))}"
            ),
        )

    # --- Đọc nội dung và kiểm tra kích thước ---
    file_content = await file.read()
    if len(file_content) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File vượt quá giới hạn 50MB (kích thước thực: {len(file_content) / 1024 / 1024:.1f}MB).",
        )

    if len(file_content) == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File rỗng, không thể xử lý.",
        )

    # --- Tạo đường dẫn lưu file (dùng UUID để tránh trùng lặp tên) ---
    doc_id = uuid4()
    safe_filename = f"{doc_id}_{original_filename}"
    upload_dir = Path(settings.UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)
    file_path = str(upload_dir / safe_filename)

    # --- Lưu file ra disk ---
    try:
        with open(file_path, "wb") as f:
            f.write(file_content)
        logger.info(
            "User '%s' đã upload file '%s' → '%s' (%d bytes)",
            current_user.email,
            original_filename,
            file_path,
            len(file_content),
        )
    except OSError as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Không thể lưu file lên server: {e}",
        ) from e

    # --- Tạo Document record trong DB với status "pending" ---
    new_doc = Document(
        id=doc_id,
        filename=original_filename,
        file_path=file_path,
        status=DocumentStatus.pending,
        uploaded_by=current_user.id,
    )
    db.add(new_doc)
    db.commit()
    db.refresh(new_doc)
    logger.info("Đã tạo document record: id=%s, filename='%s'", doc_id, original_filename)

    # --- Kick off Celery task bất đồng bộ ---
    task = process_document_task.delay(str(doc_id))
    logger.info(
        "Đã gửi Celery task '%s' cho document '%s'.",
        task.id,
        original_filename,
    )

    # --- Trả về 202 Accepted ngay lập tức ---
    return DocumentUploadResponse(
        document_id=doc_id,
        task_id=task.id,
        filename=original_filename,
        status="pending",
        message="File đã được nhận. Đang xếp hàng xử lý. Poll GET /api/documents/{document_id} để theo dõi trạng thái.",
    )


@router.get(
    "/",
    response_model=DocumentListResponse,
    summary="Danh sách tài liệu",
)
def list_documents(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status_filter: DocumentStatus = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Lấy danh sách tài liệu.
    - Admin: xem tất cả tài liệu.
    - User thường: chỉ xem tài liệu do mình upload.
    """
    query = db.query(Document)

    # User thường chỉ xem tài liệu của mình
    if current_user.role != UserRole.admin:
        query = query.filter(Document.uploaded_by == current_user.id)

    # Lọc theo status nếu có
    if status_filter:
        query = query.filter(Document.status == status_filter)

    # Sắp xếp theo thời gian upload mới nhất
    query = query.order_by(Document.upload_time.desc())

    total = query.count()
    documents = query.offset((page - 1) * page_size).limit(page_size).all()

    return DocumentListResponse(
        items=[DocumentResponse.model_validate(d) for d in documents],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Tạo document record (Admin only)",
    description=(
        "Tạo metadata record cho tài liệu trong DB (không upload file).\n\n"
        "**Phase 3** sẽ thêm endpoint `POST /api/documents/upload` "
        "để nhận file thực và kick off Celery task."
    ),
)
def create_document(
    doc_data: DocumentCreate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Tạo document record trong DB (Admin only)."""
    new_doc = Document(
        filename=doc_data.filename,
        file_path=doc_data.file_path,
        vector_collection_name=doc_data.vector_collection_name,
        status=DocumentStatus.pending,
        uploaded_by=admin.id,
    )
    db.add(new_doc)
    db.commit()
    db.refresh(new_doc)

    logger.info("Admin %s tạo document record: %s", admin.email, new_doc.filename)
    return DocumentResponse.model_validate(new_doc)


@router.get(
    "/{doc_id}",
    response_model=DocumentResponse,
    summary="Chi tiết tài liệu",
)
def get_document(
    doc_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lấy thông tin chi tiết tài liệu theo ID."""
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Không tìm thấy tài liệu với ID: {doc_id}",
        )

    # User thường chỉ xem tài liệu của mình
    if current_user.role != UserRole.admin and doc.uploaded_by != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bạn không có quyền xem tài liệu này.",
        )

    return DocumentResponse.model_validate(doc)


@router.put(
    "/{doc_id}",
    response_model=DocumentResponse,
    summary="Cập nhật metadata tài liệu (Admin only)",
)
def update_document(
    doc_id: UUID,
    doc_data: DocumentUpdate,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    """
    Cập nhật metadata tài liệu.
    Chủ yếu dùng bởi Celery task để cập nhật status sau khi xử lý xong.
    """
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Không tìm thấy tài liệu với ID: {doc_id}",
        )

    update_data = doc_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(doc, field, value)

    db.commit()
    db.refresh(doc)

    logger.info("Document %s đã được cập nhật: %s", doc_id, update_data)
    return DocumentResponse.model_validate(doc)


@router.delete(
    "/{doc_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Xóa tài liệu (Admin only)",
)
def delete_document(
    doc_id: UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Xóa document record khỏi DB (Admin only). File vật lý cần xóa riêng."""
    doc = db.query(Document).filter(Document.id == doc_id).first()
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Không tìm thấy tài liệu với ID: {doc_id}",
        )

    db.delete(doc)
    db.commit()

    logger.info("Admin %s đã xóa document: %s (%s)", admin.email, doc.filename, doc_id)
