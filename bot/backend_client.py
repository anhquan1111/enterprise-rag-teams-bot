"""
backend_client.py - HTTP Client giao tiếp với Backend FastAPI (Phase 4).

Xử lý:
    - Xác thực: Mock JWT token riêng cho từng Teams user (mock-login per user ID)
    - Chat: Tiêu thụ SSE stream, trả về full response + session_id
    - Leave Request: Tạo đơn nghỉ phép cho user cụ thể

Lưu ý quan trọng:
    - Token cache theo teams_user_id để tránh gọi mock-login lặp lại.
    - Session cache theo teams_user_id cho hội thoại multi-turn.
    - Retry 1 lần tự động khi gặp HTTP 401 (token hết hạn).
"""

import json
import logging
import os
import re
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

BACKEND_API_URL = os.getenv("BACKEND_API_URL", "http://backend:8000")


def _teams_id_to_email(teams_user_id: str) -> str:
    """
    Tạo email ổn định từ Teams user ID để dùng với mock auth.

    Teams user ID dạng "29:1AbcXyz..." không phải email hợp lệ,
    nên chúng ta normalize thành email giả để backend nhận diện user.
    Ví dụ: "29:1AbcXyz" → "teams_291abcxyz@company.com"

    LƯU Ý: PHẢI dùng TLD công khai như `.com`. KHÔNG dùng `.local` vì
    Pydantic EmailStr (qua thư viện `email-validator>=2.0`) reject các
    "special-use" TLD theo RFC 6761 (.local, .localhost, .test, .invalid)
    → Backend trả HTTP 422 Unprocessable Entity ở field `email`.
    """
    clean = re.sub(r"[^a-zA-Z0-9]", "", teams_user_id).lower()[:24]
    if not clean:
        clean = "anonymous"
    return f"teams_{clean}@company.com"


class BackendClient:
    """
    Singleton HTTP Client quản lý xác thực và giao tiếp với Backend FastAPI.

    Token cache: per Teams user ID → JWT string.
    Session cache: per Teams user ID → backend chat session_id (UUID string).
    """

    def __init__(self):
        # {teams_user_id: jwt_token}
        self._token_cache: dict[str, str] = {}
        # {teams_user_id: session_id} — duy trì multi-turn conversation
        self._session_cache: dict[str, str] = {}

    # =========================================================================
    # AUTH
    # =========================================================================

    async def _fetch_token(self, email: str, display_name: str) -> str:
        """Gọi POST /auth/mock-login, trả về JWT access_token."""
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{BACKEND_API_URL}/auth/mock-login",
                json={
                    "email": email,
                    "full_name": display_name,
                    "department": "Microsoft Teams",
                    "role": "user",
                },
            )
            resp.raise_for_status()
            token = resp.json()["access_token"]
            logger.info("Đã lấy JWT token cho: %s (%s)", display_name, email)
            return token

    async def get_token(self, teams_user_id: str, display_name: str) -> str:
        """Trả về token đã cache hoặc fetch mới nếu chưa có."""
        if teams_user_id not in self._token_cache:
            email = _teams_id_to_email(teams_user_id)
            self._token_cache[teams_user_id] = await self._fetch_token(
                email, display_name
            )
        return self._token_cache[teams_user_id]

    def invalidate_token(self, teams_user_id: str):
        """Xóa token cache để buộc fetch lại (dùng sau khi nhận HTTP 401)."""
        self._token_cache.pop(teams_user_id, None)

    # =========================================================================
    # CHAT (SSE)
    # =========================================================================

    async def chat(
        self,
        teams_user_id: str,
        display_name: str,
        message: str,
    ) -> tuple[str, str]:
        """
        Gọi POST /api/chat và tiêu thụ toàn bộ SSE stream.

        Tự động quản lý session_id cho hội thoại multi-turn.
        Retry 1 lần nếu gặp HTTP 401 (token hết hạn).

        Returns:
            (response_text, session_id) — session_id để dùng trong lần chat tiếp theo.
        """
        session_id = self._session_cache.get(teams_user_id)
        payload: dict = {"message": message}
        if session_id:
            payload["session_id"] = session_id

        for attempt in range(2):
            token = await self.get_token(teams_user_id, display_name)
            headers = {"Authorization": f"Bearer {token}"}

            try:
                full_response, new_session_id = await self._consume_sse(
                    url=f"{BACKEND_API_URL}/api/chat",
                    payload=payload,
                    headers=headers,
                )

                if new_session_id:
                    self._session_cache[teams_user_id] = new_session_id

                return full_response, new_session_id or session_id or ""

            except httpx.HTTPStatusError as e:
                if e.response.status_code == 401 and attempt == 0:
                    logger.warning(
                        "Token 401 cho user %s, làm mới token và thử lại...",
                        teams_user_id,
                    )
                    self.invalidate_token(teams_user_id)
                    continue
                logger.error("Backend trả lỗi HTTP khi chat: %s", e)
                return (
                    "❌ Hệ thống gặp sự cố khi xử lý câu hỏi. Vui lòng thử lại.",
                    "",
                )

            except httpx.ConnectError:
                logger.error("Không kết nối được Backend: %s", BACKEND_API_URL)
                return (
                    "❌ Không thể kết nối hệ thống AI lúc này. Vui lòng thử lại sau.",
                    "",
                )

        return "❌ Hệ thống gặp sự cố. Vui lòng thử lại sau.", ""

    async def _consume_sse(
        self,
        url: str,
        payload: dict,
        headers: dict,
    ) -> tuple[str, Optional[str]]:
        """
        Mở HTTP stream, đọc từng dòng SSE và tích lũy toàn bộ response.

        SSE format từ backend (chat.py Phase 4):
            data: {"token": "..."}                        ← mỗi token LLM
            data: {"error": "..."}                        ← nếu có lỗi
            data: {"event": "done", "session_id": "uuid"} ← luôn là dòng cuối

        Returns:
            (text_đã_gộp, session_id_mới)
        """
        accumulated: list[str] = []
        new_session_id: Optional[str] = None

        # Timeout 120s: đủ cho Ollama cold-start (~40s) + generate response
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST", url, json=payload, headers=headers
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    try:
                        event = json.loads(line[6:])
                    except json.JSONDecodeError:
                        logger.debug("Không parse được SSE line: %.80s", line)
                        continue

                    if "token" in event:
                        accumulated.append(event["token"])
                    elif "error" in event:
                        accumulated.append(f"\n⚠️ {event['error']}")
                    elif event.get("event") == "done":
                        new_session_id = event.get("session_id")

        text = "".join(accumulated).strip()
        return text or "Xin lỗi, tôi không nhận được phản hồi từ hệ thống AI.", new_session_id

    # =========================================================================
    # LEAVE REQUEST
    # =========================================================================

    async def create_leave_request(
        self,
        teams_user_id: str,
        display_name: str,
        start_date: str,
        end_date: str,
        reason: str,
    ) -> dict:
        """
        Gọi POST /api/leave-requests để tạo đơn nghỉ phép.

        Args:
            teams_user_id: ID của người dùng trên Teams.
            display_name: Tên hiển thị trên Teams.
            start_date: Ngày bắt đầu (YYYY-MM-DD).
            end_date: Ngày kết thúc (YYYY-MM-DD).
            reason: Lý do nghỉ phép.

        Returns:
            Dict response từ backend (có trường id, status, v.v.)

        Raises:
            ValueError: Khi backend trả 422 (validation error).
            httpx.HTTPStatusError: Khi backend trả lỗi HTTP khác.
            httpx.ConnectError: Khi không kết nối được backend.
        """
        token = await self.get_token(teams_user_id, display_name)
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{BACKEND_API_URL}/api/leave-requests",
                json={
                    "start_date": start_date,
                    "end_date": end_date,
                    "reason": reason,
                },
                headers=headers,
            )

            # 422: validation error (vd: end_date < start_date)
            if resp.status_code == 422:
                detail = resp.json().get("detail", "Dữ liệu không hợp lệ")
                # detail có thể là list (Pydantic errors) hoặc string
                if isinstance(detail, list):
                    msg = "; ".join(d.get("msg", str(d)) for d in detail)
                else:
                    msg = str(detail)
                raise ValueError(msg)

            resp.raise_for_status()
            return resp.json()


# Singleton dùng chung trong toàn bộ bot process
backend_client = BackendClient()
