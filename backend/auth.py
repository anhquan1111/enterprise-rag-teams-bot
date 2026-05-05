"""
auth.py - Mock Azure AD JWT Authentication Middleware

Mô phỏng luồng xác thực Azure AD SSO:
    1. Client (Teams Bot / Frontend) gọi POST /auth/mock-login với thông tin user
    2. Backend tạo JWT token giả lập token Azure AD
    3. Mọi request tiếp theo gửi kèm header: Authorization: Bearer <token>
    4. Dependency get_current_user() decode token, lookup/create user trong DB

Trong production:
    - Thay thế decode logic bằng microsoft-identity-web hoặc kiểm tra với Azure AD JWKS endpoint
    - Token thực từ Azure AD có iss = "https://login.microsoftonline.com/{tenant_id}/v2.0"
    - Không cần endpoint /auth/mock-login (Azure AD xử lý phần này)
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from config import settings
from database import get_db
from models import User, UserRole
from schemas import MockLoginRequest, TokenResponse, UserResponse

logger = logging.getLogger(__name__)

# =============================================================================
# ROUTER - Các endpoint liên quan đến xác thực
# =============================================================================
router = APIRouter()

# HTTP Bearer scheme: tự động đọc token từ header "Authorization: Bearer ..."
bearer_scheme = HTTPBearer(auto_error=False)


# =============================================================================
# TOKEN UTILITIES
# =============================================================================

def _create_access_token(payload: dict, expire_minutes: Optional[int] = None) -> str:
    """
    Tạo JWT access token từ payload.

    Args:
        payload: Dữ liệu cần mã hóa (email, name, role, v.v.)
        expire_minutes: Số phút hết hạn. Dùng settings mặc định nếu None.

    Returns:
        JWT token string (dạng "xxxxx.yyyyy.zzzzz")
    """
    expire_delta = timedelta(minutes=expire_minutes or settings.JWT_EXPIRE_MINUTES)
    now = datetime.now(timezone.utc)

    # Payload chuẩn JWT + thông tin giả lập Azure AD
    data = {
        **payload,
        "iss": "mock-azure-ad",              # Issuer (Azure AD thật: login.microsoftonline.com/...)
        "iat": now,                          # Issued At
        "exp": now + expire_delta,           # Expiration Time
        "nbf": now,                          # Not Before
    }

    token = jwt.encode(data, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)
    return token


def _decode_token(token: str) -> dict:
    """
    Decode và validate JWT token.

    Raises:
        JWTError: Nếu token không hợp lệ hoặc đã hết hạn.
    """
    return jwt.decode(
        token,
        settings.JWT_SECRET_KEY,
        algorithms=[settings.JWT_ALGORITHM],
    )


def _get_or_create_user(db: Session, email: str, full_name: str,
                        department: Optional[str], role: str) -> User:
    """
    Tìm user theo email trong DB. Nếu chưa tồn tại thì tạo mới.
    Giả lập hành vi "first login via Azure AD": user record được tạo tự động.

    Args:
        db: Database session
        email: Email từ JWT token
        full_name: Tên đầy đủ từ JWT token
        department: Phòng ban từ JWT token
        role: Vai trò ("admin" hoặc "user") từ JWT token

    Returns:
        User ORM object (đã có trong DB)
    """
    user = db.query(User).filter(User.email == email).first()

    if user is None:
        # Tạo user mới - giả lập first-time Azure AD login
        logger.info("Tạo user mới từ token: %s", email)
        user = User(
            email=email,
            full_name=full_name,
            department=department,
            role=UserRole(role) if role in [r.value for r in UserRole] else UserRole.user,
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        logger.info("User mới đã được tạo với ID: %s", user.id)
    else:
        # Cập nhật thông tin nếu có thay đổi từ Azure AD
        changed = False
        if user.full_name != full_name:
            user.full_name = full_name
            changed = True
        if department and user.department != department:
            user.department = department
            changed = True
        if changed:
            db.commit()
            db.refresh(user)

    return user


# =============================================================================
# FASTAPI DEPENDENCIES
# =============================================================================

async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> User:
    """
    FastAPI dependency: Xác thực Bearer token và trả về User object.

    Luồng xử lý:
        1. Đọc token từ header "Authorization: Bearer <token>"
        2. Decode JWT và kiểm tra chữ ký, thời hạn
        3. Lấy thông tin user từ payload
        4. Lookup hoặc auto-create user trong DB
        5. Kiểm tra user có đang active không

    Raises:
        HTTPException 401: Token thiếu, không hợp lệ, hoặc hết hạn.
        HTTPException 403: User bị vô hiệu hóa (is_active=False).

    Cách dùng trong endpoint:
        def my_endpoint(current_user: User = Depends(get_current_user)):
            ...
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Không thể xác thực token. Vui lòng đăng nhập lại.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if credentials is None:
        raise credentials_exception

    try:
        payload = _decode_token(credentials.credentials)
    except JWTError as e:
        logger.warning("Token decode thất bại: %s", str(e))
        raise credentials_exception

    # Lấy thông tin bắt buộc từ payload
    email: Optional[str] = payload.get("email")
    if not email:
        logger.warning("Token không chứa 'email' field")
        raise credentials_exception

    # Lookup hoặc tạo user
    user = _get_or_create_user(
        db=db,
        email=email,
        full_name=payload.get("name", email),
        department=payload.get("department"),
        role=payload.get("role", UserRole.user.value),
    )

    # Kiểm tra tài khoản còn hoạt động không
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Tài khoản của bạn đã bị vô hiệu hóa. Liên hệ Admin.",
        )

    return user


async def get_current_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """
    FastAPI dependency: Chỉ cho phép Admin.
    Dùng thay get_current_user cho các endpoint yêu cầu quyền Admin.

    Raises:
        HTTPException 403: User không có quyền Admin.
    """
    if current_user.role != UserRole.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Chức năng này chỉ dành cho Admin.",
        )
    return current_user


# =============================================================================
# AUTH ENDPOINTS
# =============================================================================

@router.post(
    "/mock-login",
    response_model=TokenResponse,
    summary="[Mock] Đăng nhập giả lập Azure AD SSO",
    description=(
        "**CHỈ DÙNG CHO DEVELOPMENT/TESTING.**\n\n"
        "Giả lập quá trình Azure AD SSO: nhận thông tin user, tạo JWT token, "
        "tự động tạo tài khoản trong DB nếu chưa tồn tại.\n\n"
        "Trong production, thay bằng flow thực của Azure AD."
    ),
)
async def mock_login(request: MockLoginRequest, db: Session = Depends(get_db)):
    """Tạo JWT token giả lập để test các endpoint bảo mật."""

    # Tạo hoặc lấy user từ DB
    user = _get_or_create_user(
        db=db,
        email=str(request.email),
        full_name=request.full_name,
        department=request.department,
        role=request.role.value,
    )

    # Tạo JWT payload giả lập cấu trúc Azure AD token
    token_payload = {
        "sub": str(user.id),          # Subject (Azure AD object ID)
        "email": str(request.email),
        "name": request.full_name,
        "department": request.department,
        "role": request.role.value,
        "roles": [request.role.value],  # Azure AD app roles (dạng mảng)
        "oid": str(user.id),           # Azure AD Object ID
    }

    access_token = _create_access_token(token_payload)

    logger.info("Mock login thành công cho: %s (role=%s)", request.email, request.role)

    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.JWT_EXPIRE_MINUTES * 60,
        user=UserResponse.model_validate(user),
    )


@router.get(
    "/me",
    response_model=UserResponse,
    summary="Lấy thông tin user hiện tại",
    description="Trả về thông tin của user đang đăng nhập (từ Bearer token).",
)
async def get_me(current_user: User = Depends(get_current_user)):
    """Endpoint kiểm tra token và xem thông tin bản thân."""
    return UserResponse.model_validate(current_user)
