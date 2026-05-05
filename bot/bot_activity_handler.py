"""
bot_activity_handler.py - Xử lý mọi Activity từ Microsoft Teams.

Luồng chính:
    1. Tin nhắn văn bản thông thường  → RAG Chat qua /api/chat (SSE)
    2. Lệnh "/xin-nghi"               → Gửi Adaptive Card Form Xin Nghỉ Phép
    3. Submit Adaptive Card (invoke)   → Tạo đơn nghỉ phép qua /api/leave-requests

THAY ĐỔI (Debug fixes):
    - _handle_chat: Typing indicator đặt trong try/except riêng — nếu gửi typing
      thất bại thì LOG và TIẾP TỤC thay vì crash toàn bộ handler (Bug #2 cũ).
    - _handle_chat: send_activity của response cuối cùng cũng trong try/except
      để tránh exception im lặng sau khi LLM đã trả về kết quả.
    - Tất cả send_activity calls trong except blocks đều được bảo vệ.
    - Thêm logging chi tiết ở đầu mỗi handler để trace luồng xử lý.
    - on_members_added_activity: bảo vệ send_activity bằng try/except.
"""

import json
import logging
import sys
import traceback as tb
from pathlib import Path

from botbuilder.core import ActivityHandler, InvokeResponse, TurnContext
from botbuilder.schema import Activity, ActivityTypes, Attachment

from backend_client import backend_client

logger = logging.getLogger(__name__)

CARDS_DIR = Path(__file__).parent / "cards"

# Lệnh kích hoạt form xin nghỉ phép (so sánh sau khi strip + lower)
LEAVE_COMMANDS = {"/xin-nghi", "xin nghỉ", "/leave", "/nghi-phep"}


def _load_card(filename: str) -> dict:
    """Đọc Adaptive Card từ file JSON trong thư mục cards/."""
    with open(CARDS_DIR / filename, encoding="utf-8") as f:
        return json.load(f)


def _make_card_attachment(card_dict: dict) -> Attachment:
    """Đóng gói dict Adaptive Card thành Attachment để gửi qua Teams."""
    return Attachment(
        content_type="application/vnd.microsoft.card.adaptive",
        content=card_dict,
    )


async def _safe_send(turn_context: TurnContext, message, label: str = ""):
    """
    Wrapper quanh turn_context.send_activity với logging đầy đủ.

    Ghi log thành công/thất bại để trace chính xác điểm nào trong luồng
    có thể gửi về Teams và điểm nào không.
    """
    try:
        await turn_context.send_activity(message)
        logger.debug("send_activity OK | label=%s", label)
        return True
    except Exception as e:
        trace_str = "".join(tb.format_exception(type(e), e, e.__traceback__))
        print(
            f"[SEND FAILED] label={label} | {type(e).__name__}: {e}\n{trace_str}",
            file=sys.stderr,
            flush=True,
        )
        logger.error("send_activity FAILED | label=%s | %s: %s", label, type(e).__name__, e)
        return False


class TeamsBot(ActivityHandler):
    """
    Bot chính xử lý các hoạt động từ Microsoft Teams.

    Override:
        on_members_added_activity → Gửi tin nhắn chào mừng
        on_message_activity       → Tin nhắn text + lệnh + card submit (fallback)
        on_invoke_activity        → Adaptive Card submit (invoke path chính)
    """

    async def on_members_added_activity(self, members_added, turn_context: TurnContext):
        """Chào mừng thành viên mới khi được thêm vào cuộc trò chuyện."""
        for member in members_added:
            if member.id != turn_context.activity.recipient.id:
                logger.info("Chào mừng thành viên mới: %s", member.name)
                await _safe_send(
                    turn_context,
                    "Xin chào! Tôi là Trợ lý AI Hành chính của văn phòng.\n\n"
                    "Tôi có thể giúp bạn:\n"
                    "- Trả lời câu hỏi về nội quy, quy trình nội bộ\n"
                    "- Nộp đơn xin nghỉ phép (gõ /xin-nghi)\n\n"
                    "Hãy đặt câu hỏi hoặc gõ /xin-nghi để bắt đầu!",
                    label="welcome",
                )

    async def on_message_activity(self, turn_context: TurnContext):
        """
        Xử lý tin nhắn text từ người dùng.

        Phân nhánh:
            - activity.value có action=submit_leave_request → xử lý submit card
            - text là lệnh nghỉ phép → gửi form card
            - text thông thường → gọi RAG chat
        """
        activity = turn_context.activity
        teams_user_id: str = (activity.from_property.id or "anonymous").strip()
        display_name: str = (activity.from_property.name or "Teams User").strip()
        text: str = (activity.text or "").strip()

        # Log đầy đủ để trace luồng — hiện trong `docker logs qlda_teams_bot`
        logger.info(
            "on_message_activity ENTER | user='%s' | id='%s' | text='%s' | value=%s",
            display_name, teams_user_id[:20], text[:80],
            type(activity.value).__name__,
        )

        # --- Path 1: Adaptive Card submit qua message (fallback một số client) ---
        if isinstance(activity.value, dict):
            if activity.value.get("action") == "submit_leave_request":
                logger.info("Routing → leave_request_submit (via message path)")
                await self._handle_leave_request_submit(
                    turn_context, activity.value, teams_user_id, display_name
                )
                return

        # --- Path 2: Lệnh nghỉ phép ---
        if text.lower() in LEAVE_COMMANDS:
            logger.info("Routing → send_leave_request_card")
            await self._send_leave_request_card(turn_context)
            return

        # --- Path 3: Chat thông thường ---
        logger.info("Routing → handle_chat")
        await self._handle_chat(turn_context, teams_user_id, display_name, text)

    async def on_invoke_activity(self, turn_context: TurnContext) -> InvokeResponse:
        """
        Xử lý Invoke activity — Adaptive Card submission trong Teams.

        Teams gửi invoke khi user bấm Action.Submit.
        activity.value chứa dữ liệu form + data field của button.
        """
        activity = turn_context.activity
        value = activity.value or {}
        teams_user_id: str = (activity.from_property.id or "anonymous").strip()
        display_name: str = (activity.from_property.name or "Teams User").strip()

        logger.info(
            "on_invoke_activity | name='%s' | value_type=%s | user='%s'",
            activity.name, type(value).__name__, display_name,
        )

        if isinstance(value, dict) and value.get("action") == "submit_leave_request":
            logger.info("Routing → leave_request_submit (via invoke path)")
            await self._handle_leave_request_submit(
                turn_context, value, teams_user_id, display_name
            )
            return InvokeResponse(status=200)

        return InvokeResponse(status=200)

    # =========================================================================
    # PRIVATE: RAG Chat
    # =========================================================================

    async def _handle_chat(
        self,
        turn_context: TurnContext,
        teams_user_id: str,
        display_name: str,
        message: str,
    ):
        """
        Gọi /api/chat (SSE), tích lũy response, gửi về Teams.

        FIX: Typing indicator nằm trong try/except riêng biệt.
        Nếu gửi typing thất bại → log warning và TIẾP TỤC xử lý chat.
        Không để typing indicator crash toàn bộ handler nữa (bug #2 cũ).
        """
        if not message:
            await _safe_send(
                turn_context,
                "Xin chào! Bạn cần hỗ trợ gì? Gõ /xin-nghi để nộp đơn nghỉ phép.",
                label="empty_message_reply",
            )
            return

        # FIX: Typing indicator trong try/except riêng — KHÔNG để nó crash handler
        # Nếu send_activity(typing) thất bại (auth issue, network...) → log + tiếp tục
        try:
            await turn_context.send_activity(Activity(type=ActivityTypes.typing))
            logger.debug("Typing indicator sent OK")
        except Exception as typing_err:
            # Log cẩn thận — đây thường là manh mối chính cho "silent bot"
            print(
                f"[TYPING FAILED] {type(typing_err).__name__}: {typing_err}\n"
                f"{''.join(tb.format_exception(type(typing_err), typing_err, typing_err.__traceback__))}",
                file=sys.stderr,
                flush=True,
            )
            logger.warning(
                "Không gửi được typing indicator: %s — tiếp tục xử lý chat...",
                typing_err,
            )
            # TIẾP TỤC — không return ở đây

        # Gọi Backend RAG chat
        logger.info("Calling backend_client.chat() for user '%s'...", display_name)
        try:
            response_text, _ = await backend_client.chat(
                teams_user_id=teams_user_id,
                display_name=display_name,
                message=message,
            )
            logger.info(
                "backend_client.chat() returned %d chars for user '%s'",
                len(response_text), display_name,
            )
        except Exception as chat_err:
            logger.error(
                "backend_client.chat() exception | user=%s | %s: %s",
                display_name, type(chat_err).__name__, chat_err,
                exc_info=(type(chat_err), chat_err, chat_err.__traceback__),
            )
            response_text = "Xin loi, he thong AI dang gap su co. Vui long thu lai sau."

        # FIX: send_activity cho response cuối cũng được bảo vệ
        sent = await _safe_send(turn_context, response_text, label="chat_response")
        if not sent:
            logger.error(
                "Không gửi được chat response về Teams cho user '%s'", display_name
            )

    # =========================================================================
    # PRIVATE: Gửi form Adaptive Card
    # =========================================================================

    async def _send_leave_request_card(self, turn_context: TurnContext):
        """Gửi Adaptive Card form xin nghỉ phép đến người dùng."""
        try:
            card = _load_card("leave_request_card.json")
        except FileNotFoundError:
            logger.error("Không tìm thấy file: cards/leave_request_card.json")
            await _safe_send(
                turn_context,
                "Loi: Khong tai duoc form nghi phep. Vui long lien he Admin.",
                label="card_not_found",
            )
            return

        attachment = _make_card_attachment(card)
        sent = await _safe_send(
            turn_context,
            Activity(type=ActivityTypes.message, attachments=[attachment]),
            label="leave_request_card",
        )
        if sent:
            logger.info(
                "Leave Request Card sent to: %s",
                turn_context.activity.from_property.name,
            )

    # =========================================================================
    # PRIVATE: Xử lý submit đơn nghỉ phép
    # =========================================================================

    async def _handle_leave_request_submit(
        self,
        turn_context: TurnContext,
        form_data: dict,
        teams_user_id: str,
        display_name: str,
    ):
        """
        Nhận dữ liệu Adaptive Card submission → validate → POST /api/leave-requests.
        """
        start_date: str = (form_data.get("start_date") or "").strip()
        end_date: str = (form_data.get("end_date") or "").strip()
        reason: str = (form_data.get("reason") or "").strip()

        logger.info(
            "Leave request submit | user='%s' | %s → %s | reason='%.60s'",
            display_name, start_date, end_date, reason,
        )

        # Validate phía bot
        missing = []
        if not start_date:
            missing.append("Ngay bat dau")
        if not end_date:
            missing.append("Ngay ket thuc")
        if not reason:
            missing.append("Ly do nghi phep")

        if missing:
            await _safe_send(
                turn_context,
                f"Vui long dien day du cac truong: {', '.join(missing)}.",
                label="validation_error",
            )
            return

        try:
            result = await backend_client.create_leave_request(
                teams_user_id=teams_user_id,
                display_name=display_name,
                start_date=start_date,
                end_date=end_date,
                reason=reason,
            )

            request_id = result.get("id", "N/A")
            msg = (
                f"Don xin nghi phep da duoc gui thanh cong!\n\n"
                f"Ma don: {request_id}\n"
                f"Tu ngay: {start_date} den ngay: {end_date}\n"
                f"Ly do: {reason}\n\n"
                f"Don dang cho phe duyet. Ban se duoc thong bao khi co ket qua."
            )
            await _safe_send(turn_context, msg, label="leave_request_success")
            logger.info("Don nghi phep tao OK: id=%s | user=%s", request_id, display_name)

        except ValueError as e:
            await _safe_send(
                turn_context, f"Loi du lieu: {e}", label="leave_validation_error"
            )

        except Exception as e:
            logger.error(
                "Loi tao don nghi phep | user=%s | %s: %s",
                display_name, type(e).__name__, e,
                exc_info=(type(e), e, e.__traceback__),
            )
            await _safe_send(
                turn_context,
                "Khong the gui don luc nay. Vui long thu lai hoac lien he phong Nhan su.",
                label="leave_request_error",
            )
