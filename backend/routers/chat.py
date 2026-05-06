"""
chat.py - RAG Chatbot Endpoint (Phase 4 + Phase 6 Hybrid Search)

Luồng xử lý chính (RAG Flow):
    1. Tìm kiếm context SONG SONG từ ChromaDB (dense vector) + LocalRecall (BM25)
    2. Trộn kết quả bằng Reciprocal Rank Fusion (RRF) — dedupe theo sha1 prefix
    3. Xây dựng RAG Prompt (context + lịch sử + câu hỏi)
    4. Gọi Ollama với stream=True, yield từng token qua SSE
    5. Lưu cặp (câu hỏi - câu trả lời) vào bảng chat_sessions
"""

import asyncio
import hashlib
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
# CONSTANTS — Hybrid Search Tuning
# =============================================================================

_EMBED_MODEL_SEARCH = "nomic-embed-text"  # Phải khớp với model dùng khi ingestion

# Mỗi engine retrieve top-K rộng để RRF có đủ candidates trộn; sau RRF chọn top-N
_RETRIEVAL_TOP_K = 10           # Số chunks lấy từ MỖI engine (Chroma + LR)
_RRF_TOP_N = 5                  # Số chunks sau merge — đưa vào prompt builder
_RRF_K = 60                     # Hằng số RRF chuẩn (Cormack 2009)
_LR_SEARCH_TIMEOUT = 15.0       # LR chậm > 15s → degrade về Chroma-only


# =============================================================================
# HELPER: Tìm kiếm dense vector từ ChromaDB
# =============================================================================

async def _search_chromadb(query: str, top_k: int = _RETRIEVAL_TOP_K) -> list[dict]:
    """
    Dense-vector search trên ChromaDB.

    Returns:
        list[dict]: mỗi dict {"id", "text", "score", "source": "chromadb"}.
        score = 1 - cosine_distance → cao hơn = liên quan hơn (cùng chiều với LR).
        Trả về [] nếu collection chưa tồn tại hoặc lỗi (graceful degradation).
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
    def _sync_chromadb_query() -> list[dict]:
        chroma_client = chromadb.HttpClient(
            host=settings.CHROMADB_HOST,
            port=settings.CHROMADB_PORT,
        )
        try:
            collection = chroma_client.get_collection(settings.LOCALRECALL_COLLECTION)
        except Exception:
            logger.warning(
                "Collection '%s' chưa tồn tại trong ChromaDB.",
                settings.LOCALRECALL_COLLECTION,
            )
            return []

        results = collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            include=["documents", "distances"],
        )
        ids = (results.get("ids") or [[]])[0]
        docs = (results.get("documents") or [[]])[0]
        dists = (results.get("distances") or [[]])[0]
        out: list[dict] = []
        for i in range(min(len(ids), len(docs))):
            text = docs[i]
            if not text or not text.strip():
                continue
            distance = dists[i] if i < len(dists) else 1.0
            out.append({
                "id": ids[i],
                "text": text,
                "score": 1.0 - float(distance),
                "source": "chromadb",
            })
        return out

    try:
        loop = asyncio.get_running_loop()
        chunks = await loop.run_in_executor(None, _sync_chromadb_query)
        logger.info("ChromaDB trả về %d chunks cho query: '%.50s'", len(chunks), query)
        return chunks
    except Exception as exc:
        logger.error("Lỗi ChromaDB search: %s", exc)
        return []


# =============================================================================
# HELPER: Tìm kiếm BM25 hybrid từ LocalRecall
# =============================================================================

# Log response shape của LocalRecall đúng 1 lần đầu để dễ verify field aliases
_lr_response_shape_logged = False


async def _search_localrecall(query: str, top_k: int = _RETRIEVAL_TOP_K) -> list[dict]:
    """
    Hybrid (BM25 + vector) search trên LocalRecall (postgres engine).

    LocalRecall response shape có thể khác nhau giữa các version → parser
    defensive: chấp nhận data như list, dict["results"|"hits"|"matches"], và
    alias field "content"|"text"|"chunk", "score"|"relevance"|"distance".

    Mọi failure mode (timeout, connect refused, HTTP 4xx/5xx, JSON parse error,
    unexpected shape, parser yields 0) được log VERBOSE với status code + body +
    exception traceback để dễ debug; sau đó mới trả [] để chat tiếp tục
    (graceful degradation về ChromaDB-only).
    """
    global _lr_response_shape_logged

    url = (
        f"{settings.LOCALRECALL_HOST}"
        f"/api/collections/{settings.LOCALRECALL_COLLECTION}/search"
    )
    payload = {"query": query, "max_results": top_k}

    # --- 1. HTTP request — phân biệt từng failure mode để log cụ thể ---
    try:
        async with httpx.AsyncClient(timeout=_LR_SEARCH_TIMEOUT) as client:
            resp = await client.post(url, json=payload)
    except httpx.TimeoutException as exc:
        logger.error(
            "LocalRecall search TIMEOUT (>%.1fs) tại %s | query='%.80s' | exc=%s",
            _LR_SEARCH_TIMEOUT, url, query, exc,
        )
        return []
    except httpx.ConnectError as exc:
        logger.error(
            "LocalRecall search CONNECT REFUSED tại %s | exc=%s "
            "— kiểm tra container `localrecall` đã up chưa (docker compose ps).",
            url, exc,
        )
        return []
    except httpx.RequestError as exc:
        # Read error, write error, protocol error, DNS, … — log full traceback
        logger.error(
            "LocalRecall search REQUEST ERROR tại %s | type=%s | exc=%s",
            url, type(exc).__name__, exc,
            exc_info=True,
        )
        return []
    except Exception as exc:
        logger.error(
            "LocalRecall search lỗi không xác định tại %s | exc=%s",
            url, exc, exc_info=True,
        )
        return []

    # --- 2. HTTP status — non-2xx phải log status code + body chính xác ---
    if resp.status_code >= 400:
        body_preview = (resp.text or "")[:1500]
        logger.error(
            "LocalRecall search HTTP %d từ %s | query='%.80s' | body=%s",
            resp.status_code, url, query, body_preview,
        )
        return []

    # --- 3. JSON parse — log raw text nếu không parse được ---
    try:
        data = resp.json()
    except (ValueError, json.JSONDecodeError) as exc:
        logger.error(
            "LocalRecall search JSON INVALID (HTTP %d) tại %s | exc=%s | raw_text=%.1500s",
            resp.status_code, url, exc, resp.text or "",
        )
        return []

    # --- 4. Log response shape lần đầu để verify field aliases ---
    if not _lr_response_shape_logged:
        try:
            sample = json.dumps(data, ensure_ascii=False)[:2000]
        except Exception:
            sample = repr(data)[:2000]
        logger.info("LocalRecall response shape (first call): %s", sample)
        _lr_response_shape_logged = True

    # --- 5. Parse results — track shape_used để log nếu parser yield 0 ---
    # LocalRecall (verified shape):
    #   {"success": true, "data": {"count": N, "results": [
    #       {"ID": "1", "Content": "...", "Metadata": {...}, "Embedding": null}
    #   ]}}
    # Field name viết HOA chữ đầu (Go exported-name convention). KHÔNG có score
    # field ở root → fallback rank-decay (RRF dùng rank-position, không phải
    # absolute score, nên decay vẫn merge đúng thứ tự).
    raw_results: list = []
    shape_used: str = "?"
    if isinstance(data, list):
        raw_results = data
        shape_used = "list"
    elif isinstance(data, dict):
        # Unwrap envelope {"success": ..., "data": {...}} nếu có
        envelope_inner = data.get("data") if isinstance(data.get("data"), dict) else None
        inner = envelope_inner if envelope_inner is not None else data
        envelope_prefix = "data." if envelope_inner is not None else ""
        for key in ("results", "Results", "hits", "Hits", "matches", "Matches"):
            value = inner.get(key)
            if isinstance(value, list):
                raw_results = value
                shape_used = f"dict.{envelope_prefix}{key}"
                break
        else:
            shape_used = f"dict(keys={list(data.keys())[:10]})"
    else:
        shape_used = f"unknown:{type(data).__name__}"

    def _pick(item: dict, *aliases):
        """Lookup field qua nhiều alias (PascalCase + lowercase). Trả None nếu thiếu/rỗng."""
        for k in aliases:
            v = item.get(k)
            if v is not None and v != "":
                return v
        return None

    out: list[dict] = []
    for i, item in enumerate(raw_results):
        if not isinstance(item, dict):
            continue
        # Text: "Content" (LR thực tế) → fallback text/chunk
        text = _pick(item, "Content", "content", "Text", "text", "Chunk", "chunk")
        if not text or not str(text).strip():
            continue
        # ID: "ID" (LR Go-style) → fallback id/chunk_id
        chunk_id = _pick(item, "ID", "id", "ChunkID", "chunk_id")
        # Score: LR KHÔNG trả score field → rank-decay làm fallback
        score_val = _pick(item, "Score", "score", "Relevance", "relevance")
        if score_val is not None:
            score = float(score_val)
        else:
            distance_val = _pick(item, "Distance", "distance")
            if distance_val is not None:
                score = 1.0 - float(distance_val)
            else:
                # Rank-decay: 1.0, 0.95, 0.90, … → top 10 vẫn > 0.5
                score = max(0.05, 1.0 - i * 0.05)
        out.append({
            "id": str(chunk_id) if chunk_id is not None else f"lr_{i}",
            "text": str(text),
            "score": score,
            "source": "localrecall",
        })

    # --- 6. Parser yielded 0 items — dump full response để debug shape ---
    if not out:
        try:
            full_dump = json.dumps(data, ensure_ascii=False)
        except Exception:
            full_dump = repr(data)
        logger.error(
            "LocalRecall trả 0 usable chunks cho query='%.80s' "
            "(HTTP %d, parsed_shape=%s, raw_results_count=%d). "
            "Full response (≤2500 chars): %s",
            query, resp.status_code, shape_used, len(raw_results), full_dump[:2500],
        )
    else:
        logger.info(
            "LocalRecall trả %d chunks (HTTP %d, parsed_shape=%s) cho query: '%.50s'",
            len(out), resp.status_code, shape_used, query,
        )
    return out


# =============================================================================
# HELPER: Reciprocal Rank Fusion merge
# =============================================================================

def _rrf_merge(
    result_lists: list[list[dict]],
    k: int = _RRF_K,
    top_n: int = _RRF_TOP_N,
) -> list[str]:
    """
    Reciprocal Rank Fusion (Cormack et al. 2009):
        score(d) = Σ over each ranked list r:  1 / (k + rank(d, r))
    rank bắt đầu từ 1.

    Dedupe by sha1(text.strip()[:200]) — chunker giữa Chroma vs LR khác nhau
    (chunk_size, overlap) → ID không match được; prefix-hash bắt được chunks
    chứa CÙNG đoạn văn (đầu giống nhau) và cộng dồn score → rank cao hơn.

    Returns:
        list[str]: top_n texts theo RRF score giảm dần (rỗng nếu cả 2 list rỗng).
    """
    scores: dict[str, float] = {}
    text_by_key: dict[str, str] = {}

    for results in result_lists:
        for rank, item in enumerate(results):
            text = item.get("text", "")
            if not text or not text.strip():
                continue
            key = hashlib.sha1(text.strip()[:200].encode("utf-8")).hexdigest()
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank + 1)
            if key not in text_by_key:
                text_by_key[key] = text

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return [text_by_key[key] for key, _ in ranked[:top_n]]


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
        "**Luồng xử lý (Hybrid RAG):**\n"
        "1. Tìm kiếm SONG SONG trên ChromaDB (dense vector) + LocalRecall (BM25 keyword)\n"
        "2. Trộn kết quả bằng Reciprocal Rank Fusion (RRF, k=60)\n"
        "3. Xây dựng RAG Prompt (top chunks + lịch sử + câu hỏi)\n"
        "4. Stream từng token từ Ollama (qwen2.5:7b)\n"
        "5. Lưu lịch sử vào PostgreSQL\n\n"
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

    # --- 2. Hybrid retrieval: ChromaDB (vector) + LocalRecall (BM25) song song ---
    chroma_results, lr_results = await asyncio.gather(
        _search_chromadb(request.message, top_k=_RETRIEVAL_TOP_K),
        _search_localrecall(request.message, top_k=_RETRIEVAL_TOP_K),
    )
    context_chunks = _rrf_merge([chroma_results, lr_results], top_n=_RRF_TOP_N)
    logger.info(
        "Hybrid retrieval: chroma=%d, localrecall=%d → RRF merged top %d.",
        len(chroma_results), len(lr_results), len(context_chunks),
    )

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
