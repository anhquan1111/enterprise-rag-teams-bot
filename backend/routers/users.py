"""
routers/users.py - CRUD endpoints cho Users

Endpoints:
    GET    /api/users          → Danh sách users (Admin only)
    GET    /api/users/me       → Thông tin bản thân (đã có ở /auth/me, alias ở đây)
    GET    /api/users/{id}     → Chi tiết user (Admin hoặc chính user đó)
    PUT    /api/users/{id}     → Cập nhật user (Admin hoặc chính user đó)
    DELETE /api/users/{id}     → Xóa user (Admin only)
"""

import logging
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from auth import get_current_admin, get_current_user
from database import get_db
from models import User, UserRole
from schemas import UserCreate, UserListResponse, UserResponse, UserUpdate

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get(
    "/",
    response_model=UserListResponse,
    summary="Danh sách users (Admin only)",
)
def list_users(
    page: int = Query(default=1, ge=1, description="Số trang (bắt đầu từ 1)"),
    page_size: int = Query(default=20, ge=1, le=100, description="Số items mỗi trang"),
    department: Optional[str] = Query(default=None, description="Lọc theo phòng ban"),
    role: Optional[UserRole] = Query(default=None, description="Lọc theo vai trò"),
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),  # Chỉ Admin
):
    """Lấy danh sách toàn bộ users, hỗ trợ phân trang và lọc."""
    query = db.query(User)

    # Lọc theo department nếu có
    if department:
        query = query.filter(User.department.ilike(f"%{department}%"))

    # Lọc theo role nếu có
    if role:
        query = query.filter(User.role == role)

    total = query.count()
    users = query.offset((page - 1) * page_size).limit(page_size).all()

    return UserListResponse(
        items=[UserResponse.model_validate(u) for u in users],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Tạo user mới (Admin only)",
)
def create_user(
    user_data: UserCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(get_current_admin),
):
    """Admin tạo user mới thủ công (không qua Azure AD)."""
    # Kiểm tra email đã tồn tại chưa
    existing = db.query(User).filter(User.email == user_data.email).first()
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Email '{user_data.email}' đã được đăng ký.",
        )

    new_user = User(
        email=str(user_data.email),
        full_name=user_data.full_name,
        department=user_data.department,
        role=user_data.role,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    logger.info("Admin đã tạo user mới: %s", new_user.email)
    return UserResponse.model_validate(new_user)


@router.get(
    "/{user_id}",
    response_model=UserResponse,
    summary="Chi tiết một user",
)
def get_user(
    user_id: UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Lấy thông tin user theo ID. Admin xem được tất cả; user chỉ xem bản thân."""
    # Kiểm tra quyền: Admin hoặc chính user đó
    if current_user.role != UserRole.admin and current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bạn không có quyền xem thông tin người dùng khác.",
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Không tìm thấy user với ID: {user_id}",
        )

    return UserResponse.model_validate(user)


@router.put(
    "/{user_id}",
    response_model=UserResponse,
    summary="Cập nhật thông tin user",
)
def update_user(
    user_id: UUID,
    user_data: UserUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    Cập nhật thông tin user.
    - Admin: có thể cập nhật tất cả fields bao gồm role và is_active.
    - User thường: chỉ cập nhật full_name và department của bản thân.
    """
    # Kiểm tra quyền
    if current_user.role != UserRole.admin and current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Bạn không có quyền chỉnh sửa thông tin người dùng khác.",
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Không tìm thấy user với ID: {user_id}",
        )

    # User thường không được đổi role hoặc is_active
    if current_user.role != UserRole.admin:
        if user_data.role is not None or user_data.is_active is not None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Bạn không có quyền thay đổi vai trò hoặc trạng thái tài khoản.",
            )

    # Cập nhật từng field nếu được cung cấp
    update_data = user_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(user, field, value)

    db.commit()
    db.refresh(user)

    logger.info("User %s đã cập nhật thông tin cho user %s", current_user.email, user.email)
    return UserResponse.model_validate(user)


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Xóa user (Admin only)",
)
def delete_user(
    user_id: UUID,
    db: Session = Depends(get_db),
    admin: User = Depends(get_current_admin),
):
    """Vô hiệu hóa tài khoản user (soft delete: set is_active=False)."""
    # Không cho phép Admin tự xóa chính mình
    if admin.id == user_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Không thể vô hiệu hóa tài khoản của chính mình.",
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Không tìm thấy user với ID: {user_id}",
        )

    # Soft delete: không xóa record, chỉ set is_active=False
    user.is_active = False
    db.commit()

    logger.info("Admin %s đã vô hiệu hóa user: %s", admin.email, user.email)
