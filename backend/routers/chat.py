"""
chat.py - RAG Chatbot Endpoint (Phase 4)

Luồng xử lý chính (RAG Flow):
    1. Tìm kiếm context từ LocalRecall (vector search)
    2. Xây dựng RAG Prompt (context + lịch sử + câu hỏi)
    3. Gọi Ollama với stream=True, yield từng token qua SSE
    4. Lưu cặp (câu hỏi - câu trả lời) vào bảng chat_sessions
"""

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import chromadb
import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from auth import get_current_user
from config import settings
from database import get_db
from models import ChatSession, User
from schemas import ChatRequest

logger = logging.getLogger(__name__)
router = APIRouter()


# =============================================================================
# HELPER: Tìm kiếm tài liệu liên quan từ ChromaDB (trực tiếp, không qua LocalRecall)
# =============================================================================

_EMBED_MODEL_SEARCH = "nomic-embed-text"  # Phải khớp với model dùng khi ingestion


async def _search_chromadb(query: str, top_k: int = 5) -> list[str]:
    """
    Tìm kiếm semantic trong ChromaDB:
    1. Embed câu query bằng Ollama /api/embed (async)
    2. Query ChromaDB với embedding đó (sync client chạy trong thread executor)

    Returns:
        Danh sách nội dung text của top_k chunks liên quan nhất (rỗng nếu lỗi).
    """
    # --- Bước 1: Embed câu query (async, không block event loop) ---
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            embed_resp = await client.post(
                f"{settings.OLLAMA_HOST}/api/embed",
                json={"model": _EMBED_MODEL_SEARCH, "input": [query]},
            )
            embed_resp.raise_for_status()
            query_embedding: list[float] = embed_resp.json()["embeddings"][0]
    except Exception as exc:
        logger.error("Lỗi embed query với Ollama: %s", exc)
        return []

    # --- Bước 2: Query ChromaDB (sync client, chạy trong thread để không block) ---
    def _sync_chromadb_query() -> list[str]:
        chroma_client = chromadb.HttpClient(
            host=settings.CHROMADB_HOST,
            port=settings.CHROMADB_PORT,
        )
        try:
            collection = chroma_client.get_collection(settings.LOCALRECALL_COLLECTION)
        except Exception:
            logger.warning(
                "Collection '%s' chưa tồn tại trong ChromaDB. Chưa có tài liệu nào được index.",
                settings.LOCALRECALL_COLLECTION,
            )
            return []

        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            include=["documents"],
        )
        return results.get("documents", [[]])[0]

    try:
        loop = asyncio.get_running_loop()
        chunks: list[str] = await loop.run_in_executor(None, _sync_chromadb_query)
        logger.info(
            "ChromaDB trả về %d chunks cho query: '%.50s'",
            len(chunks), query,
        )
        return [c for c in chunks if c and c.strip()]
    except Exception as exc:
        logger.error("Lỗi ChromaDB search: %s", exc)
        return []


# =============================================================================
# HELPER: Xây dựng RAG Prompt tiếng Việt
# =============================================================================

def _build_rag_prompt(
    context_chunks: list[str],
    question: str,
    history: list[dict],
) -> str:
    """
    Ghép system instruction + tài liệu RAG + lịch sử hội thoại + câu hỏi.

    Args:
        context_chunks: Tối đa 3 đoạn tài liệu liên quan từ LocalRecall.
        question:       Câu hỏi hiện tại của người dùng.
        history:        Lịch sử hội thoại từ ChatSession.context_json.
    """
    system_part = (
        "Bạn là trợ lý hành chính AI của văn phòng. "
        "Nhiệm vụ của bạn là trả lời câu hỏi của nhân viên dựa trên tài liệu nội bộ. "
        "Hãy trả lời ngắn gọn, chính xác và lịch sự bằng tiếng Việt."
    )

    if context_chunks:
        formatted = "\n\n---\n\n".join(
            f"[Tài liệu {i + 1}]:\n{chunk}"
            for i, chunk in enumerate(context_chunks[:3])
        )
        context_part = (
            f"\n\n[Tài liệu nội bộ liên quan]:\n{formatted}\n\n"
            "Dựa vào các tài liệu trên, hãy trả lời câu hỏi một cách chính xác. "
            "Nếu tài liệu không chứa thông tin liên quan, hãy nói: "
            "'Tôi không tìm thấy thông tin này trong tài liệu nội bộ.'"
        )
    else:
        context_part = (
            "\n\n[Lưu ý]: Không tìm thấy tài liệu nội bộ liên quan đến câu hỏi này. "
            "Hãy thông báo cho người dùng và gợi ý họ liên hệ phòng ban liên quan."
        )

    # Giữ tối đa 6 messages gần nhất để tránh prompt quá dài
    history_part = ""
    if history:
        recent = history[-6:]
        lines = []
        for msg in recent:
            label = "Nhân viên" if msg.get("role") == "user" else "Trợ lý AI"
            lines.append(f"{label}: {msg.get('content', '')}")
        history_part = "\n\n[Lịch sử hội thoại gần đây]:\n" + "\n".join(lines)

    return (
        f"{system_part}"
        f"{context_part}"
        f"{history_part}"
        f"\n\n[Câu hỏi]: {question}"
        f"\n\n[Trả lời]:"
    )


# =============================================================================
# HELPER: Stream từng token từ Ollama
# =============================================================================

async def _stream_ollama(prompt: str):
    """
    Async generator: gọi Ollama /api/generate với stream=True,
    yield từng token text nhận được.

    Raises:
        httpx.ConnectError: Không kết nối được Ollama.
        httpx.HTTPStatusError: Ollama trả về lỗi HTTP.
    """
    url = f"{settings.OLLAMA_HOST}/api/generate"
    payload = {
        "model": settings.OLLAMA_MODEL,
        "prompt": prompt,
        "stream": True,
        "options": {
            "temperature": 0.7,
            "top_p": 0.9,
        },
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        async with client.stream("POST", url, json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                try:
                    chunk_data = json.loads(line)
                    token = chunk_data.get("response", "")
                    if token:
                        yield token
                    if chunk_data.get("done", False):
                        break
                except json.JSONDecodeError:
                    logger.warning("Không parse được dòng Ollama: %.100s", line)


# =============================================================================
# HELPER: Lưu lịch sử chat vào DB
# =============================================================================

def _save_chat_history(
    db: Session,
    user_id: UUID,
    session_id: Optional[UUID],
    user_message: str,
    ai_response: str,
) -> ChatSession:
    """
    Append cặp (user_message, ai_response) vào ChatSession.
    Tạo session mới nếu session_id=None hoặc session không tồn tại.

    Returns:
        ChatSession đã được cập nhật/tạo mới.
    """
    now_str = datetime.now(timezone.utc).isoformat()
    new_messages = [
        {"role": "user",      "content": user_message, "timestamp": now_str},
        {"role": "assistant", "content": ai_response,  "timestamp": now_str},
    ]

    session = None
    if session_id:
        session = (
            db.query(ChatSession)
            .filter(
                ChatSession.id == session_id,
                ChatSession.user_id == user_id,
            )
            .first()
        )

    if session:
        existing = session.context_json or []
        session.context_json = existing + new_messages
    else:
        session = ChatSession(user_id=user_id, context_json=new_messages)
        db.add(session)

    db.commit()
    db.refresh(session)
    logger.info("Đã lưu chat history vào session: %s", session.id)
    return session


# =============================================================================
# ENDPOINT: POST /api/chat
# =============================================================================

@router.post(
    "",
    summary="Chat với AI Trợ Lý (RAG + Streaming SSE)",
    description=(
        "Endpoint chat chính — trả về **Server-Sent Events** stream.\n\n"
        "**Luồng xử lý:**\n"
        "1. Tìm kiếm tài liệu liên quan từ LocalRecall (vector search)\n"
        "2. Xây dựng RAG Prompt (tài liệu + lịch sử + câu hỏi)\n"
        "3. Stream từng token từ Ollama (qwen2.5:7b)\n"
        "4. Lưu lịch sử vào PostgreSQL\n\n"
        "**SSE Response format:**\n"
        "```\n"
        'data: {"token": "Xin "}\n\n'
        'data: {"token": "chào!"}\n\n'
        'data: {"event": "done", "session_id": "uuid"}\n\n'
        "```\n\n"
        "Truyền `session_id` từ lần trước để tiếp tục hội thoại multi-turn."
    ),
    response_class=StreamingResponse,
)
async def chat(
    request: ChatRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    RAG Chat endpoint — trả về SSE stream.
    """
    logger.info(
        "Chat request — user: %s | message: '%.60s...'",
        current_user.email, request.message
    )

    # --- 1. Tải lịch sử hội thoại từ session cũ (nếu có) ---
    existing_history: list[dict] = []
    if request.session_id:
        prev_session = (
            db.query(ChatSession)
            .filter(
                ChatSession.id == request.session_id,
                ChatSession.user_id == current_user.id,
            )
            .first()
        )
        if prev_session and prev_session.context_json:
            existing_history = prev_session.context_json

    # --- 2. Tìm kiếm context từ ChromaDB (embed query → cosine search) ---
    context_chunks = await _search_chromadb(request.message, top_k=5)

    # --- 3. Xây dựng RAG Prompt ---
    prompt = _build_rag_prompt(context_chunks[:3], request.message, existing_history)
    logger.debug("Prompt xây dựng xong, độ dài: %d ký tự", len(prompt))

    # --- 4. Stream từ Ollama + lưu DB ---
    # Dùng list để accumulate tokens trong closure (mutable object)
    accumulated: list[str] = []

    async def event_stream():
        """Async generator phát SSE events."""
        try:
            async for token in _stream_ollama(prompt):
                accumulated.append(token)
                yield f"data: {json.dumps({'token': token}, ensure_ascii=False)}\n\n"

        except httpx.ConnectError:
            err = "Không thể kết nối Ollama. Vui lòng thử lại sau."
            accumulated.append(f"[Lỗi: {err}]")
            yield f"data: {json.dumps({'error': err})}\n\n"

        except httpx.HTTPStatusError as e:
            err = f"Ollama trả về lỗi HTTP {e.response.status_code}."
            accumulated.append(f"[Lỗi: {err}]")
            yield f"data: {json.dumps({'error': err})}\n\n"

        except Exception as e:
            logger.error("Lỗi không xác định khi stream Ollama: %s", str(e))
            err = "Lỗi hệ thống. Vui lòng thử lại."
            accumulated.append(f"[Lỗi: {err}]")
            yield f"data: {json.dumps({'error': err})}\n\n"

        finally:
            # Lưu lịch sử chat vào DB sau khi stream kết thúc
            full_response = "".join(accumulated)
            saved_session_id: Optional[str] = None
            try:
                saved = _save_chat_history(
                    db=db,
                    user_id=current_user.id,
                    session_id=request.session_id,
                    user_message=request.message,
                    ai_response=full_response,
                )
                saved_session_id = str(saved.id)
            except Exception as db_err:
                logger.error("Lỗi lưu chat history: %s", str(db_err))

            # Event "done" báo hiệu kết thúc stream, kèm session_id để client dùng tiếp
            yield f"data: {json.dumps({'event': 'done', 'session_id': saved_session_id})}\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # Tắt buffering của Nginx proxy
        },
    )
