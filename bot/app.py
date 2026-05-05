"""
app.py - Entry point cho Microsoft Teams Bot Service.

Port: 3978 | Route: POST /api/messages

THAY ĐỔI (Debug fixes):
    - _on_adapter_error: in FULL traceback ra stderr với flush=True để luôn
      visible trong `docker logs qlda_teams_bot` dù không gửi được về Teams.
    - handle_messages: log activity type + text trước khi xử lý.
    - InvokeResponse body: serialize bằng json.dumps() thay vì str() để đảm
      bảo JSON hợp lệ (str() dùng single-quote format của Python, không phải JSON).
    - Logging level = DEBUG để thấy toàn bộ trace khi debug.
"""

import json
import logging
import os
import sys
import traceback as tb
from types import SimpleNamespace

from aiohttp import web
from aiohttp.web import Request, Response
from botbuilder.core import TurnContext
# Verified trong botbuilder-integration-aiohttp 4.16.2: cả CloudAdapter và
# ConfigurationBotFrameworkAuthentication đều export từ
# `botbuilder.integration.aiohttp` — đây cũng là vị trí chính thức trong
# Microsoft Bot Framework Python samples.
# (botbuilder.core chỉ có CloudAdapterBase abstract; botframework.connector.auth
# có các primitive thấp hơn nhưng KHÔNG có ConfigurationBotFrameworkAuthentication.)
from botbuilder.integration.aiohttp import (
    CloudAdapter,
    ConfigurationBotFrameworkAuthentication,
)
from botbuilder.schema import Activity

from bot_activity_handler import TeamsBot

# =============================================================================
# LOGGING — StreamHandler ra stdout để docker logs luôn bắt được
# =============================================================================
_handler = logging.StreamHandler(sys.stdout)
_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
logging.basicConfig(level=logging.DEBUG, handlers=[_handler])
logger = logging.getLogger(__name__)

# =============================================================================
# BOT FRAMEWORK ADAPTER — Modern CloudAdapter
#
# 401 Unauthorized fix:
#   Legacy BotFrameworkAdapter KHÔNG hiểu các trường MicrosoftAppType /
#   MicrosoftAppTenantId. Nó luôn dùng MultiTenant + endpoint mặc định
#   `login.microsoftonline.com/botframework.com`. Với app SingleTenant
#   tạo từ Teams Developer Portal, OAuth-token request không khớp issuer →
#   Azure trả 401 khi bot gọi `client.conversations.reply_to_activity`.
#
#   ConfigurationBotFrameworkAuthentication đọc 4 thuộc tính SCREAMING_SNAKE
#   từ một config object qua attribute access:
#       MICROSOFT_APP_ID
#       MICROSOFT_APP_PASSWORD
#       MICROSOFT_APP_TYPE       ("MultiTenant" | "SingleTenant" | "UserAssignedMSI")
#       MICROSOFT_APP_TENANT_ID  (bắt buộc khi APP_TYPE != MultiTenant)
#   Sau đó nó tự build credentials factory đúng cho từng loại app.
# =============================================================================
APP_ID = os.getenv("MICROSOFT_APP_ID", "").strip()
APP_PASSWORD = os.getenv("MICROSOFT_APP_PASSWORD", "").strip()
APP_TYPE = os.getenv("MICROSOFT_APP_TYPE", "MultiTenant").strip()
APP_TENANT_ID = os.getenv("MICROSOFT_APP_TENANT_ID", "").strip()


# Config object cho ConfigurationBotFrameworkAuthentication.
#
# QUAN TRỌNG — tên thuộc tính phải KHỚP CHÍNH XÁC với cái SDK Python check
# qua hasattr/getattr trong ConfigurationServiceClientCredentialFactory:
#     configuration.APP_ID
#     configuration.APP_PASSWORD
#     configuration.APP_TYPE
#     configuration.APP_TENANTID    ← MỘT TỪ, không có gạch dưới giữa TENANT và ID
# Đây KHÁC convention C# (MicrosoftAppId) và KHÁC tên biến môi trường
# (MICROSOFT_APP_*). Dùng sai tên → factory fallback và app_id = None →
# `Unauthorized. Invalid AppId passed on token: ...`.
# Đã verify bằng cách đọc source của package trong image.
_bot_config = SimpleNamespace(
    APP_ID=APP_ID,
    APP_PASSWORD=APP_PASSWORD,
    APP_TYPE=APP_TYPE,
    APP_TENANTID=APP_TENANT_ID,
)


# Validate sớm — fail-fast tốt hơn 401 mơ hồ sau khi bot đã chạy
if APP_TYPE.lower() == "singletenant" and not APP_TENANT_ID:
    raise RuntimeError(
        "MICROSOFT_APP_TYPE=SingleTenant nhưng MICROSOFT_APP_TENANT_ID rỗng. "
        "Lấy Tenant ID từ Entra ID Portal → App Registration → Overview → "
        "Directory (tenant) ID."
    )

bot_framework_authentication = ConfigurationBotFrameworkAuthentication(_bot_config)
adapter = CloudAdapter(bot_framework_authentication)


async def _on_adapter_error(context: TurnContext, error: Exception):
    """
    Handler lỗi của Bot Framework pipeline.

    FIX: In FULL stack trace ra stderr với flush=True để LUÔN xuất hiện
    trong `docker logs qlda_teams_bot`, kể cả khi không gửi được về Teams.
    `logger.error(..., exc_info=True)` bên ngoài except-block không bắt được
    traceback (sys.exc_info() trả về None) — phải dùng error.__traceback__.
    """
    # Tạo traceback string từ exception object (không phụ thuộc sys.exc_info)
    trace_lines = tb.format_exception(type(error), error, error.__traceback__)
    trace_str = "".join(trace_lines)

    # In thẳng ra stderr với flush=True — không thể bị mất dù logging bị lỗi
    print("\n" + "=" * 70, file=sys.stderr, flush=True)
    print("[BOT PIPELINE ERROR] Exception trong Bot Framework:", file=sys.stderr, flush=True)
    print(f"  Type   : {type(error).__name__}", file=sys.stderr, flush=True)
    print(f"  Message: {error}", file=sys.stderr, flush=True)
    try:
        print(f"  Activity type: {context.activity.type}", file=sys.stderr, flush=True)
        print(f"  From user    : {context.activity.from_property.name}", file=sys.stderr, flush=True)
        print(f"  Text snippet : {str(context.activity.text or '')[:80]}", file=sys.stderr, flush=True)
    except Exception:
        pass
    print(f"Stack trace:\n{trace_str}", file=sys.stderr, flush=True)
    print("=" * 70 + "\n", file=sys.stderr, flush=True)

    # Cũng log qua logging system (exc_info tuple để bắt đúng traceback)
    logger.error(
        "Bot pipeline exception | %s: %s",
        type(error).__name__, error,
        exc_info=(type(error), error, error.__traceback__),
    )

    # Thử gửi thông báo về Teams — CÓ THỂ THẤT BẠI nếu đây là nguyên nhân lỗi.
    # Bắt và log lỗi thứ cấp này thay vì dùng bare `pass`.
    try:
        await context.send_activity(
            "❌ Xảy ra lỗi nội bộ. Vui lòng thử lại sau vài giây."
        )
    except Exception as send_err:
        # Log lỗi thứ cấp — đây thường là manh mối quan trọng nhất
        print(
            f"[BOT PIPELINE ERROR] send_activity thất bại trong on_turn_error: "
            f"{type(send_err).__name__}: {send_err}",
            file=sys.stderr,
            flush=True,
        )


adapter.on_turn_error = _on_adapter_error

# Singleton bot handler
bot = TeamsBot()


# =============================================================================
# HTTP HANDLERS
# =============================================================================

async def handle_messages(req: Request) -> Response:
    """
    POST /api/messages — Nhận webhook từ Azure Bot Service / Emulator.

    Luồng:
        1. Validate Content-Type
        2. Parse JSON body thành Activity
        3. Adapter xác thực JWT token từ Azure (skip nếu APP_ID trống)
        4. Gọi bot.on_turn() để xử lý
        5. Trả 201 (message) hoặc InvokeResponse (card submit)
    """
    if req.content_type != "application/json":
        return Response(status=415, text="Content-Type phải là application/json")

    try:
        body = await req.json()
    except Exception as e:
        logger.error("Body không parse được JSON: %s", e)
        return Response(status=400, text=f"Bad Request: {e}")

    # Log activity nhận được để debug — luôn hiển thị trong docker logs
    activity_type = body.get("type", "unknown")
    activity_text = str(body.get("text") or "")[:80]
    from_name = body.get("from", {}).get("name", "?")
    logger.info(
        ">>> Incoming activity | type=%-20s | from=%-20s | text='%s'",
        activity_type, from_name, activity_text,
    )

    try:
        activity = Activity().deserialize(body)
    except Exception as e:
        logger.error("Không deserialize được Activity: %s", e)
        return Response(status=400, text=f"Invalid Activity: {e}")

    auth_header = req.headers.get("Authorization", "")

    try:
        # LƯU Ý: CloudAdapter có thứ tự tham số NGƯỢC với BotFrameworkAdapter cũ.
        #   Legacy:  process_activity(activity, auth_header, callback)
        #   Cloud :  process_activity(auth_header, activity, callback)
        invoke_response = await adapter.process_activity(
            auth_header, activity, bot.on_turn
        )

        logger.info(
            "<<< Activity processed | type=%s | has_invoke_response=%s",
            activity_type, invoke_response is not None,
        )

        # Invoke activities (card submit) cần trả về InvokeResponse body
        if invoke_response:
            # FIX: json.dumps() thay vì str() — Python str() dùng single-quote
            # không phải JSON chuẩn: str({"a":"b"}) → "{'a': 'b'}" (invalid JSON)
            body_str = json.dumps(invoke_response.body or {})
            return Response(
                status=invoke_response.status,
                content_type="application/json",
                text=body_str,
            )
        return Response(status=201)

    except Exception as e:
        # Exception này chỉ xảy ra ở tầng JWT validation hoặc infrastructure
        # (exception từ bot.on_turn được adapter bắt và route tới on_turn_error)
        trace_str = tb.format_exc()
        print(f"[CRITICAL] process_activity exception:\n{trace_str}", file=sys.stderr, flush=True)
        logger.error("process_activity exception: %s", e, exc_info=True)
        return Response(status=500, text=str(e))


async def handle_health(req: Request) -> Response:
    """GET /health — Health check cho Docker."""
    return Response(
        status=200,
        content_type="application/json",
        text='{"status": "ok", "service": "teams-bot"}',
    )


# =============================================================================
# KHỞI ĐỘNG SERVER
# =============================================================================

app = web.Application()
app.router.add_post("/api/messages", handle_messages)
app.router.add_get("/health", handle_health)

if __name__ == "__main__":
    port = int(os.getenv("BOT_PORT", "3978"))
    backend_url = os.getenv("BACKEND_API_URL", "http://backend:8000")

    # Debug-safe: chỉ in 4 ký tự đầu của APP_ID để xác nhận .env đã load.
    # KHÔNG BAO GIỜ log password.
    if APP_ID:
        app_id_label = f"'{APP_ID[:4]}...' (len={len(APP_ID)})"
    else:
        app_id_label = "'' (CHƯA CẤU HÌNH — sẽ chạy ở chế độ no-auth, Teams sẽ trả 401!)"

    pwd_status = "SET" if APP_PASSWORD else "MISSING"
    tenant_label = APP_TENANT_ID[:4] + "..." if APP_TENANT_ID else "(none)"

    # Dùng print+flush thay vì logger để đảm bảo xuất hiện đầu tiên
    print("\n" + "=" * 60, flush=True)
    print("  QLDA Teams Bot — Khởi động", flush=True)
    print(f"  Port       : {port}", flush=True)
    print(f"  Backend    : {backend_url}", flush=True)
    print(f"  APP_ID     : {app_id_label}", flush=True)
    print(f"  APP_PWD    : {pwd_status}", flush=True)
    print(f"  APP_TYPE   : {APP_TYPE}", flush=True)
    print(f"  APP_TENANT : {tenant_label}", flush=True)
    print(f"  Webhook    : POST http://0.0.0.0:{port}/api/messages", flush=True)
    print("=" * 60 + "\n", flush=True)

    # Cảnh báo sớm nếu cấu hình có vẻ sai — tránh "silent 401" sau khi nhận msg
    if APP_ID and not APP_PASSWORD:
        print(
            "[WARN] APP_ID đã set nhưng APP_PASSWORD trống — Azure sẽ trả 401 "
            "khi bot reply. Kiểm tra biến MICROSOFT_APP_PASSWORD trong .env.",
            file=sys.stderr, flush=True,
        )
    if APP_TYPE.lower() == "singletenant" and not APP_TENANT_ID:
        print(
            "[WARN] APP_TYPE=SingleTenant nhưng APP_TENANT_ID trống — sẽ raise "
            "RuntimeError. Đặt MICROSOFT_APP_TENANT_ID trong .env.",
            file=sys.stderr, flush=True,
        )

    web.run_app(app, host="0.0.0.0", port=port, access_log=logger)
