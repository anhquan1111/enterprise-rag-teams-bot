"""
routers/leave_requests.py - CRUD endpoints cho LeaveRequests (Đơn xin nghỉ phép)

Endpoints:
    GET    /api/leave-requests         → Danh sách đơn (Admin: tất cả, User: của mình)
    POST   /api/leave-requests         → Tạo đơn mới
    GET    /api/leave-requests/{id}    → Chi tiết đơn
    PUT    /api/leave-requests/{id}/status → Admin duyệt/từ chối đơn
    DELETE /api/leave-requests/{id}    → Xóa đơn (chỉ khi đang pending)
"""

import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from auth import get_current_admin, get_current_user
from database import get_db
from models import LeaveRequest, LeaveStatus, User, UserRole
from schemas import (
    LeaveRequestCreate, LeaveRequestListResponse,
    LeaveRequestResponse, LeaveRequestStatusUpdate,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/",
    response_model=LeaveRequestListResponse,
    summary="Danh sách đơn xin nghỉ phép",
)
def list_leave_requests(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    status_filter: LeaveStatus = Query(default=None, alias="status"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Lấy danh sách đơn nghỉ phép.
    - Admin: xem tất cả đơn của toàn công ty.
    - User thường: chỉ xem đơn của bản thân.
    """
    query = db.query(LeaveRequest)

    if current_user.role != UserRole.admin:
        query = query.filter(LeaveRequest.user_id == current_user.id)

    if status_filter:
        query = query.filter(LeaveRequest.status == status_filter)

    query = query.order_by(LeaveRequest.created_at.desc())

    total = query.count()
    requests = query.offset((page - 1) * page_size).limit(page_size).all()

    return LeaveRequestListResponse(
        items=[LeaveRequestResponse.model_validate(r) for r in requests],
        total=total,
    )


@router.post(
    "/",
    response_model=LeaveRequestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Tạo đơn xin nghỉ phép",
)
def create_leave_request(
    request_data: LeaveRequestCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """User tạo đơn xin nghỉ phép mới."""
    new_request = LeaveRequest(
        user_id=current_user.id,
        start_date=request_data.start_date,
        end_date=request_data.end_date,
        reason=request_data.reason,
        status=LeaveStatus.pending,
    )
    db.add(new_request)
    db.commit()
    db.refresh(new_request)

    logger.info(
        "User %s tạo đơn nghỉ phép: %s → %s",
        current_user.email, request_data.start_date, request_data.end_date,
    )
    return LeaveRequestResponse.model_validate(new_request)


@router.get(
    "/{request_id}",
    response_model=LeaveRequestResponse,
    summary="Chi tiết đơn xin nghỉ phép",
)
def get_leave_request(
    request_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lấy chi tiết một đơn nghỉ phép."""
    leave_req = db.query(LeaveRequest).filter(LeaveRequest.id == request_id).first()
    if not leave_req:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Không tìm thấy đơn nghỉ phép với ID: {request_id}",
        )

    # User thường chỉ xem đơn của mình
    if current_user.role != UserRole.admin and leave_req.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bạn không có quyền xem đơn nghỉ phép này.",
        )

    return LeaveRequestResponse.model_validate(leave_req)


@router.put(
    "/{request_id}/status",
    response_model=LeaveRequestResponse,
    summary="Duyệt/Từ chối đơn nghỉ phép (Admin only)",
)
def update_leave_request_status(
    request_id: UUID,
    update_data: LeaveRequestStatusUpdate,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Admin duyệt hoặc từ chối đơn nghỉ phép."""
    leave_req = db.query(LeaveRequest).filter(LeaveRequest.id == request_id).first()
    if not leave_req:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Không tìm thấy đơn nghỉ phép với ID: {request_id}",
        )

    # Chỉ xử lý đơn đang pending
    if leave_req.status != LeaveStatus.pending:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Đơn này đã được xử lý trước đó (trạng thái: {leave_req.status.value}).",
        )

    leave_req.status = update_data.status
    leave_req.reviewed_by = admin.id
    leave_req.reviewed_at = datetime.now(timezone.utc)
    leave_req.review_note = update_data.review_note

    db.commit()
    db.refresh(leave_req)

    logger.info(
        "Admin %s đã %s đơn nghỉ phép %s",
        admin.email, update_data.status.value, request_id,
    )
    return LeaveRequestResponse.model_validate(leave_req)


@router.delete(
    "/{request_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Hủy đơn xin nghỉ phép",
)
def delete_leave_request(
    request_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """User hủy đơn nghỉ phép (chỉ khi đơn đang pending)."""
    leave_req = db.query(LeaveRequest).filter(LeaveRequest.id == request_id).first()
    if not leave_req:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Không tìm thấy đơn nghỉ phép với ID: {request_id}",
        )

    # Chỉ chủ đơn hoặc admin mới được hủy
    if current_user.role != UserRole.admin and leave_req.user_id != current_user.id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bạn không có quyền hủy đơn nghỉ phép này.",
        )

    # Chỉ hủy được đơn đang pending
    if leave_req.status != LeaveStatus.pending:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Chỉ có thể hủy đơn đang chờ duyệt (pending).",
        )

    db.delete(leave_req)
    db.commit()

    logger.info("User %s đã hủy đơn nghỉ phép: %s", current_user.email, request_id)
