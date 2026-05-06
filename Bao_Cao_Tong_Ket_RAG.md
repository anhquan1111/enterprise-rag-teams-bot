# BÁO CÁO TỔNG KẾT KỸ THUẬT
# Hệ thống AI Trợ lý Hành chính Văn phòng (RAG On-Premise)

| Thông tin | Chi tiết |
|---|---|
| **Tên dự án** | Hệ thống AI Trợ lý Hành chính Văn phòng |
| **Mã dự án** | QLDA_Code (môn Quản lý Dự án) |
| **Phạm vi báo cáo** | Phase 1 → Phase 5 đã hoàn thiện |
| **Kiến trúc** | Microservices On-Premise (Docker Compose) |
| **Ngày phát hành** | 2026-05-06 |
| **Vai trò người soạn** | Senior System Architect |

---

## TÓM TẮT ĐIỀU HÀNH (Executive Summary)

Dự án xây dựng thành công một hệ thống **Retrieval-Augmented Generation (RAG)** chạy hoàn toàn **on-premise** nhằm giải đáp các câu hỏi về quy chế nội bộ và xử lý quy trình hành chính (xin nghỉ phép) cho doanh nghiệp. Toàn bộ dữ liệu — bao gồm tài liệu nội bộ, lịch sử hội thoại, và quá trình suy luận của mô hình ngôn ngữ — không bao giờ rời khỏi hạ tầng nội bộ, đáp ứng yêu cầu nghiêm ngặt về **bảo mật dữ liệu và tuân thủ pháp lý**.

Kiến trúc gồm **9 microservices** orchestrated bằng Docker Compose: PostgreSQL (metadata), Redis (broker), ChromaDB (vector dense), LocalRecall + Postgres-pgvector (BM25 keyword), Ollama (LLM Qwen2.5:7B + embedding nomic-embed-text), FastAPI backend, Celery worker, và Microsoft Teams Bot. Đặc trưng kỹ thuật nổi bật: **hybrid retrieval** kết hợp dense vector và BM25 thông qua **Reciprocal Rank Fusion (RRF)**, **TOC-aware filtering** loại trừ chunks mục lục gây nhiễu, **streaming SSE** cho trải nghiệm hội thoại tức thời, và tích hợp **Adaptive Cards v1.5** cho quy trình xin nghỉ phép qua Microsoft Teams.

Hệ thống đã chứng minh tính khả thi của RAG tiếng Việt cấp doanh nghiệp với phần cứng phổ thông, nhưng đồng thời cũng bộc lộ các giới hạn chính cần đầu tư tiếp: thông lượng generation thấp do CPU-only, chunking cố định gây hallucination ở ranh giới Điều/Khoản, và chưa có hệ thống đánh giá định lượng (evaluation harness).

---

## 1. KIẾN TRÚC HỆ THỐNG & TECH STACK

### 1.1. Sơ đồ kiến trúc 3 tầng

```
┌────────────────────────────────────────────────────────────────────────┐
│                     TẦNG 3 — INTERFACE / CLIENT                        │
│  ┌──────────────────────────┐    ┌────────────────────────────────┐   │
│  │   Microsoft Teams Bot    │    │   FastAPI Swagger UI / cURL    │   │
│  │   (qlda_teams_bot:3978)  │    │   (developer & admin tools)    │   │
│  └────────────┬─────────────┘    └──────────────┬─────────────────┘   │
└───────────────┼──────────────────────────────────┼─────────────────────┘
                │  HTTPS (Azure Bot Service)        │  HTTP/REST + SSE
                │  HTTP (internal Docker network)   │
┌───────────────┼──────────────────────────────────┼─────────────────────┐
│               ▼          TẦNG 2 — APPLICATION    ▼                     │
│  ┌──────────────────────────────────────────────────────────────────┐ │
│  │   FastAPI Backend (qlda_backend:8000)                            │ │
│  │   • Routers: /auth, /api/users, /api/documents,                  │ │
│  │     /api/leave-requests, /api/chat (SSE)                         │ │
│  │   • Mock JWT (HS256, 480m) — mô phỏng Azure AD                   │ │
│  │   • Hybrid Retrieval: asyncio.gather(Chroma, LocalRecall)        │ │
│  │   • RRF merge (k=60) + TOC filter + Soft-prompt builder          │ │
│  └────────┬─────────────────────────────────┬───────────────────────┘ │
│           │                                 │                          │
│  ┌────────▼─────────────┐         ┌─────────▼──────────────────────┐   │
│  │  Celery Worker       │         │  Async tasks (Redis broker)    │   │
│  │  (qlda_celery_worker)│◄────────│  process_document_task         │   │
│  │  --concurrency=2     │         │  index_to_localrecall_task     │   │
│  └────────┬─────────────┘         └────────────────────────────────┘   │
└───────────┼────────────────────────────────────────────────────────────┘
            │
┌───────────┼────────────────────────────────────────────────────────────┐
│           ▼              TẦNG 1 — INFRASTRUCTURE & DATA                │
│                                                                         │
│  ┌────────────────┐  ┌─────────┐  ┌──────────┐  ┌─────────────────┐    │
│  │  PostgreSQL    │  │  Redis  │  │ ChromaDB │  │  Ollama LLM     │    │
│  │  (metadata)    │  │ (broker)│  │ (vector) │  │  qwen2.5:7b     │    │
│  │  qlda_postgres │  │qlda_redi│  │qlda_chro │  │  +nomic-embed   │    │
│  │  :5432         │  │  :6379  │  │ (intern.)│  │  :11434         │    │
│  └────────────────┘  └─────────┘  └──────────┘  └─────────────────┘    │
│                                                                         │
│  ┌──────────────────────────────┐  ┌────────────────────────────────┐  │
│  │  LocalRecall_DB              │  │  LocalRecall (BM25 Engine)     │  │
│  │  Postgres 18 + pgvector      │◄─┤  Hybrid: BM25 0.6 / Vec 0.4    │  │
│  │  + pg_textsearch (BM25)      │  │  qlda_localrecall:8080         │  │
│  │  + pgvectorscale (DiskANN)   │  │                                │  │
│  │  + timescaledb               │  │                                │  │
│  └──────────────────────────────┘  └────────────────────────────────┘  │
│                                                                         │
└────────────────────────────────────────────────────────────────────────┘
```

### 1.2. Bảng tổng hợp 9 services

| # | Service (hostname) | Image | Host Port | Vai trò chính | Phụ thuộc |
|---|---|---|---|---|---|
| 1 | `postgres` | postgres:15-alpine | 5432 | DB metadata: users, documents, leave_requests, chat_sessions | — |
| 2 | `redis` | redis:7-alpine | 6379 | Celery broker + result backend (appendonly yes) | — |
| 3 | `chromadb` | chromadb/chroma:latest | (nội bộ) | Vector DB chính (HNSW cosine) — **không expose host** | — |
| 4 | `ollama` | ollama/ollama:latest | 11434 | LLM `qwen2.5:7b` + embedding `nomic-embed-text` (auto-pull) | — |
| 5 | `localrecall_db` | qlda_localrecall_db:pg18 (build) | (nội bộ) | Postgres 18 + pgvector + pg_textsearch + pgvectorscale + timescaledb | — |
| 6 | `localrecall` | quay.io/mudler/localrecall | 8080 | BM25 keyword + vector hybrid (weight 0.6/0.4) | ollama, localrecall_db |
| 7 | `backend` | ./backend (FastAPI) | 8000 | API Gateway + RAG flow + Auth | postgres, redis |
| 8 | `celery_worker` | ./backend (cùng image) | — | Ingestion pipeline async | postgres, redis |
| 9 | `teams_bot` | ./bot (botbuilder 4.16.2) | 3978 | Microsoft Teams Bot Framework | backend |

> **Quy tắc Docker bắt buộc:** Khi gọi API liên-container trong cùng `qlda_network`, **luôn dùng tên service** (key trong `services:`) làm hostname — KHÔNG dùng `container_name`. Ví dụ: `http://ollama:11434`, KHÔNG phải `http://qlda_ollama:11434`.

### 1.3. Vai trò chi tiết từng container

**`postgres` — Nguồn chân lý cho Metadata**
- PostgreSQL 15-alpine: dung lượng nhỏ (~80MB), khởi động nhanh, phù hợp môi trường on-premise.
- Bốn bảng chính: `users` (UUID PK + Enum role), `documents` (status enum: pending/processing/done/failed), `leave_requests`, `chat_sessions` (cột `context_json` lưu lịch sử multi-turn dạng JSON).
- Healthcheck `pg_isready` chu kỳ 10s; volume bền vững `qlda_postgres_data` đảm bảo dữ liệu không mất khi restart.
- Khởi tạo idempotent qua `Base.metadata.create_all()` + `ALTER TABLE … ADD COLUMN IF NOT EXISTS` trong lifespan FastAPI.

**`redis` — Message Broker & Result Backend**
- Redis 7-alpine với `appendonly yes` + `appendfsync everysec` → durability ở mức "fsync mỗi giây" (cân bằng tốc độ và an toàn).
- Phục vụ kép: vừa là **broker** (queue task Celery) vừa là **result backend** (lưu trạng thái task).
- Nếu mở rộng: có thể tách result backend sang Postgres khi traffic tăng.

**`chromadb` — Vector Database chính**
- Pin `chromadb/chroma:latest` (hiện 1.4.3) khớp major với client Python `chromadb>=1.0.0,<2.0.0` trong `backend/requirements.txt` — **không khớp major sẽ ném `KeyError: '_type'`** vì server 1.x bỏ field discriminator.
- Cosine similarity: collection metadata `{"hnsw:space": "cosine"}` cho khoảng cách góc giữa vector — phù hợp với embedding `nomic-embed-text` (768 dim).
- **KHÔNG expose port ra host** (chỉ truy cập nội bộ qua `chromadb:8000`) → giảm bề mặt tấn công.
- Idempotent ingestion: trước khi `collection.add()`, task Celery gọi `collection.delete(where={"doc_id": {"$eq": doc_id}})` để loại chunks cũ → re-ingest cùng tài liệu không sinh duplicate.

**`ollama` — Local LLM Server**
- Chứa **hai model**: `qwen2.5:7b` (generation, 4.7GB GGUF Q4_K_M) và `nomic-embed-text` (embedding 137M params, 768 dim).
- **Auto-pull entrypoint** (`docker-compose.yml:97-129`): khi container khởi động, kiểm tra volume `ollama_data`; nếu thiếu model thì `ollama pull` tự động — giúp deploy "one-command" trên máy mới.
- Healthcheck `ollama list`, `start_period: 120s` để chờ pull model lần đầu.
- Cấu hình quan trọng: `num_ctx=8192`, timeout HTTP `read=180s` (xem Section 3).

**`localrecall_db` — Postgres dedicated cho LocalRecall**
- Build từ `./docker/localrecall_db/Dockerfile.pgsql` để mirror image chính thức của LocalRecall — chứa **4 extension đặc biệt**:
  - `pg_textsearch` (Timescale BM25, **bắt buộc**, phải build từ source, chỉ hỗ trợ PG17/18)
  - `pgvector` (dense vector ANN)
  - `pgvectorscale` (DiskANN — index vector tốc độ cao)
  - `timescaledb` (preload bắt buộc cho `pgvectorscale`)
- Build lần đầu mất 10–15 phút (Rust compile cho `pgvectorscale`); sau đó cached.
- **Tách hoàn toàn** khỏi `postgres` chính → bảo đảm an toàn dữ liệu nghiệp vụ và cô lập sự cố.

**`localrecall` — BM25 Hybrid Engine**
- `quay.io/mudler/localrecall:latest`, kết nối Ollama qua OpenAI-compatible API (`OPENAI_BASE_URL=http://ollama:11434/v1`).
- Cấu hình hybrid: `HYBRID_SEARCH_BM25_WEIGHT=0.6`, `HYBRID_SEARCH_VECTOR_WEIGHT=0.4` — ưu tiên BM25 vì **ChromaDB đã đảm nhiệm phần dense vector** trong kiến trúc tổng. LocalRecall đóng vai trò "exact keyword matcher" trong RRF fusion phía backend.
- **CHỈ dùng cho retrieval**, KHÔNG dùng cho ingestion (xem giải thích ở Section 2.3).

**`backend` — FastAPI Application**
- FastAPI 0.115.5 + SQLAlchemy 2.0.35 + Pydantic 2.9.2 (extras `[email]` cho `EmailStr`).
- Routers: `/auth` (mock JWT), `/api/users`, `/api/documents`, `/api/leave-requests`, `/api/chat` (SSE streaming).
- Hot-reload trong development qua mount `./backend:/app` + `uvicorn --reload`.
- Healthcheck `GET /health` (chu kỳ 30s, `start_period: 40s` cho lần init DB lần đầu).

**`celery_worker` — Async Task Processor**
- Dùng chung Dockerfile với `backend`, chỉ override CMD: `celery -A celery_app worker --loglevel=info --concurrency=2`.
- Cấu hình quan trọng: `task_acks_late=True` (ack sau khi hoàn thành — chống mất task khi crash), `worker_prefetch_multiplier=1` (1 task/lúc — phù hợp tác vụ nặng), `timezone="Asia/Ho_Chi_Minh"`.
- Volume `uploads_data` chia sẻ với `backend` để cùng đọc/ghi file tạm.

**`teams_bot` — Microsoft Teams Bot Framework**
- aiohttp server port 3978, route `POST /api/messages`.
- Sử dụng **modern `CloudAdapter`** + `ConfigurationBotFrameworkAuthentication` (botbuilder 4.16.2) — KHÔNG dùng legacy `BotFrameworkAdapter` (không hỗ trợ SingleTenant Azure AD).
- Trong development: kết nối Teams qua `ngrok http 3978` → URL `https://xxx.ngrok-free.app/api/messages` đăng ký ở Teams Developer Portal.

### 1.4. Sơ đồ luồng dữ liệu

**Luồng Ingestion (upload tài liệu):**
```
Client → POST /api/documents/upload (multipart)
       → Backend: lưu file vào uploads_data + tạo Document(status=pending)
       → Celery enqueue: process_document_task.delay(doc_id) → HTTP 202
       → Worker: extract (PyMuPDF/python-docx) → chunk (Recursive 2000/200)
              → batch embed (Ollama /api/embed, sub-batch 100)
              → bulk insert ChromaDB (1 HTTP call) → fire-and-forget LR index
              → Update Document(status=done, chunk_count=N)
```

**Luồng Inference (hỏi đáp):**
```
Client → POST /api/chat (Bearer JWT) {message, session_id}
       → Backend: load history (ChatSession.context_json)
              → asyncio.gather(_search_chromadb, _search_localrecall) [10 + 10 chunks]
              → _filter_low_quality (loại TOC) → _rrf_merge (k=60, top_n=5)
              → _build_rag_prompt (system + 5 chunks + 6 history + question)
              → _stream_ollama (qwen2.5:7b, num_ctx=8192) → SSE
       → Client: parse SSE events {token,...} {error,...} {event:"done", session_id}
```

### 1.5. Networking & Volumes

- **1 bridge network duy nhất**: `qlda_network` cô lập toàn bộ stack khỏi mạng host và container khác trên máy.
- **7 named volumes** bền vững:
  - `qlda_postgres_data`, `qlda_redis_data`, `qlda_chromadb_data`, `qlda_ollama_data`, `qlda_localrecall_data`, `qlda_localrecall_db_data`, `qlda_uploads_data`.
- **Quy tắc vận hành**: dùng `docker compose down` (KHÔNG `-v`) cho stop bình thường; chỉ `down -v` khi reset toàn bộ — vì `-v` xóa volume Chroma sẽ gây **drift** với metadata trong Postgres (xem Section 4.4).

### 1.6. Tech Stack tổng hợp

| Lớp | Công nghệ | Phiên bản pin |
|---|---|---|
| Web framework | FastAPI | 0.115.5 |
| ASGI server | uvicorn[standard] | 0.32.1 |
| ORM | SQLAlchemy | 2.0.35 |
| DB driver | psycopg2-binary | 2.9.10 |
| Validation | Pydantic | 2.9.2 (extras `[email]`) |
| Auth | python-jose[cryptography] | 3.3.0 |
| Task queue | Celery | 5.4.0 |
| RAG | langchain + langchain-text-splitters | 0.3.7 / 0.3.2 |
| PDF/DOCX | pymupdf, python-docx | 1.24.9 / 1.1.2 |
| HTTP client | httpx | 0.28.0 |
| Vector DB client | chromadb | >=1.0.0,<2.0.0 |
| Bot Framework | botbuilder-* | 4.16.2 (đồng bộ 4 package) |

---

## 2. CÁC GIAI ĐOẠN ĐÃ HOÀN THÀNH (Milestones)

| Phase | Trạng thái | Output chính |
|---|---|---|
| 1. Hạ tầng | ✓ Hoàn tất | 7 services hạ tầng + healthcheck + auto-pull model |
| 2. Backend & DB | ✓ Hoàn tất | FastAPI + 4 bảng ORM + Mock JWT |
| 3. Data Ingestion | ✓ Hoàn tất | Celery pipeline (bypass LocalRecall, batch embed) |
| 4. RAG Engine | ✓ Hoàn tất | Hybrid retrieval + RRF + TOC filter + SSE |
| 5. Teams Bot | ✓ Hoàn tất | CloudAdapter + Adaptive Cards v1.5 |

### 2.1. Giai đoạn 1 — Hạ tầng Docker & Networking

- Khởi tạo 7 services hạ tầng (Postgres, Redis, ChromaDB, Ollama, LocalRecall_DB, LocalRecall, Backend) với healthcheck riêng cho từng service — `depends_on` dùng `condition: service_healthy` đảm bảo thứ tự khởi động đúng.
- **Auto-pull model** Qwen2.5:7B + nomic-embed-text qua entrypoint tùy chỉnh của container `ollama`. Kiểm tra existence trên volume trước khi pull → idempotent: chạy lại `docker compose up` lần thứ hai sẽ skip pull (~3 phút).
- **Build custom image** cho `localrecall_db` để cài 4 extension đặc biệt — không có image upstream nào sẵn (cần `pg_textsearch` build từ source). Lần đầu build mất 10–15 phút (Rust compile cho `pgvectorscale`), sau đó cached.
- Network bridge `qlda_network` + 7 volumes named bền vững. Quy tắc: `docker compose down` (giữ data) cho stop bình thường, `docker compose down -v` chỉ khi reset toàn bộ.

### 2.2. Giai đoạn 2 — Backend & Database

- **4 bảng SQLAlchemy** với UUID primary keys (chống enumeration attack):
  - `users` (Enum role: admin/user, email unique, department)
  - `documents` (Enum status: pending/processing/done/failed, error_message, chunk_count, vector_collection_name, localrecall_indexed)
  - `leave_requests` (start_date, end_date, reason, Enum status: pending/approved/rejected, reviewed_by FK)
  - `chat_sessions` (context_json — lưu lịch sử multi-turn dạng `[{"role": "user|assistant", "content": "..."}]`)
- **Mock JWT Auth** (HS256, TTL 480 phút) qua endpoint `POST /auth/mock-login` — mô phỏng Azure AD code flow để giai đoạn sau swap MSAL không phải refactor router. Token chứa `sub` (user_id), `email`, `role`.
- **Pydantic 2.9.2 extras `[email]`** dùng `EmailStr` validation — phát hiện sớm các email không hợp lệ. Lưu ý kỹ thuật: RFC 6761 reject các "special-use" TLD (`.local`, `.localhost`, `.test`, `.invalid`) → bot phải sinh email user dạng `teams_<id>@company.com` (không phải `@company.local`).

### 2.3. Giai đoạn 3 — Data Ingestion Pipeline

**Quy trình 7 bước (`backend/tasks.py:process_document_task`):**

1. **Lấy Document record** từ Postgres → cập nhật `status="processing"`.
2. **Fire-and-forget** `index_to_localrecall_task.delay()` (non-fatal — lỗi ở đây không thay đổi status chính của document).
3. **Extract text**: PyMuPDF cho PDF (`fitz.TEXT_PRESERVE_WHITESPACE` để giữ bảng tiếng Việt), python-docx cho DOCX (đọc cả paragraphs và tables qua `" | ".join(row.cells)`).
4. **Chunk**: `RecursiveCharacterTextSplitter(chunk_size=2000, chunk_overlap=200, separators=["\n\n","\n",".","!","?"," ",""])`. Loại chunk ngắn < 50 ký tự.
5. **Batch embed**: gửi sub-batch 100 chunks/request tới `POST {OLLAMA_HOST}/api/embed` với `model=nomic-embed-text` và `input=[chunks...]`. Retry 3 lần exponential backoff (5s/10s/20s) khi `TimeoutException` hoặc `ConnectError`.
6. **Bulk insert ChromaDB** (1 HTTP call):
   ```python
   collection.delete(where={"doc_id": {"$eq": doc_id}})  # idempotent
   collection.add(ids=[...], documents=chunks, embeddings=embeddings,
                  metadatas=[{"source": filename, "doc_id": doc_id, "chunk_index": i}])
   ```
7. **Update Postgres**: `status="done"`, `chunk_count=N`, `vector_collection_name="qlda_documents"`. Trong `finally` block: xóa file tạm trong `UPLOAD_DIR` (graceful cleanup ngay cả khi fail).

**Quyết định kiến trúc trọng yếu — Bypass LocalRecall cho Ingestion:**
LocalRecall xử lý **từng embedding tuần tự** qua Ollama nội bộ (không có batch endpoint). Đo thực tế: file 50 trang PDF mất ~15 phút qua LocalRecall vs ~18 giây qua Ollama `/api/embed` trực tiếp với batch 100 — **nhanh ~50×**. Vì vậy hệ thống ingest thẳng vào ChromaDB; LocalRecall chỉ index song song để hỗ trợ BM25 ở khâu retrieval.

**HTTP 202 Accepted Pattern**: endpoint `POST /api/documents/upload` trả 202 ngay sau khi enqueue task — client poll `GET /api/documents/{id}` để theo dõi `status` chuyển từ `pending` → `processing` → `done`/`failed`.

### 2.4. Giai đoạn 4 — RAG Engine (Hybrid Retrieval + RRF + SSE)

Được triển khai trong `backend/routers/chat.py` với các thành phần kỹ thuật chính:

**(a) Hybrid Retrieval song song**
```python
chroma_results, lr_results = await asyncio.gather(
    _search_chromadb(query, top_k=10),       # Dense cosine (768-dim)
    _search_localrecall(query, top_k=10),    # BM25 keyword (LocalRecall hybrid)
)
```
Hai engine chạy đồng thời (không tuần tự) → giảm latency tổng. ChromaDB query bọc trong `loop.run_in_executor` (vì SDK Chroma sync) để không block event loop FastAPI.

**(b) Defensive Parser cho LocalRecall**
- LocalRecall response có shape không nhất quán giữa các phiên bản: chấp nhận cả `list`, `dict["results"|"hits"|"matches"]`, và alias field `Content`/`content`/`Text`.
- HTTP 404 (collection chưa tồn tại) log **WARNING** chứ không **ERROR** — vì là trạng thái hợp lệ khi LR chưa kịp index → graceful degrade về Chroma-only.
- Timeout 15s cho LR; mọi failure (timeout, 4xx/5xx, JSON invalid) → trả `[]` thay vì crash endpoint.
- Fallback ranking: nếu LR không trả `score` → dùng `max(0.05, 1.0 - i*0.05)` theo position.

**(c) TOC Filter (loại chunks mục lục) — áp TRƯỚC RRF**
`_is_toc_chunk(text)` phát hiện theo 2 dấu hiệu:
- `dot_ratio > 0.20` (tỉ lệ ký tự `.` dày đặc — đặc trưng dotted-leader của mục lục)
- HOẶC `≥3 heading "Điều N."` liên tiếp + `alphanumeric_ratio < 0.5`

Fallback: nếu **TẤT CẢ** chunks bị filter (case user hỏi đúng về "mục lục") → giữ nguyên list gốc.

**(d) RRF Merge (Cormack et al. 2009)**
$$
\text{score}(d) = \sum_{r \in \text{rankers}} \frac{1}{k + \text{rank}(d, r)}
$$
- `k = 60` (giá trị đề xuất trong paper gốc)
- Dedupe bằng **SHA1 của 200 ký tự đầu** (`text.strip()[:200]`) → bắt được cùng đoạn văn dù ID khác (chunker LR và Chroma cắt khác nhau)
- `top_n = 5` chunks cuối cùng đưa vào prompt

**(e) Soft Prompt Builder**
Hệ thống đã từng dùng câu refusal cứng `"Tôi không tìm thấy thông tin này..."` trong system prompt. Quan sát: qwen2.5:7b "học" template và **lặp lại nguyên văn** ngay cả khi context có thông tin một phần. Đã thay bằng **5 hướng dẫn mềm**:
1. Đọc kỹ TẤT CẢ đoạn — thông tin có thể nằm rải rác → tổng hợp lại.
2. Trích dẫn cụ thể "Theo Điều 6, …".
3. Nếu chỉ một phần → trả lời phần biết, nêu phần thiếu (KHÔNG từ chối hoàn toàn).
4. Nếu giống mục lục (dots + trang) → BỎ QUA đoạn đó.
5. CHỈ từ chối khi TẤT CẢ đoạn không liên quan → gợi ý liên hệ phòng ban.

Số chunks vào prompt nâng từ 3 → 5 (`_PROMPT_CONTEXT_CHUNKS=5`) nhờ `num_ctx=8192` đủ chỗ.

**(f) SSE Streaming**
Endpoint trả `StreamingResponse(media_type="text/event-stream")` với 3 loại event:
```
data: {"token": "Theo "}
data: {"token": "Điều "}
data: {"error": "Ollama không khả dụng"}      ← chỉ khi có lỗi
data: {"event": "done", "session_id": "uuid"}  ← luôn là event cuối
```
- Multi-turn: client lưu `session_id` từ event `done` → truyền lại trong request kế tiếp → backend load 6 messages gần nhất từ `ChatSession.context_json`.
- Lưu DB nằm trong `finally` block của async generator — đảm bảo ghi history kể cả khi client disconnect giữa stream.

### 2.5. Giai đoạn 5 — Microsoft Teams Bot & Adaptive Cards

**Kiến trúc tích hợp:**
```
Teams Client → Azure Bot Service → ngrok HTTPS → qlda_teams_bot:3978
qlda_teams_bot ↔ Azure Bot Service (OAuth, MSAL, APP_ID/PASSWORD)
qlda_teams_bot → http://backend:8000/api/chat (SSE) + /auth/mock-login
```

**Modern CloudAdapter (botbuilder 4.16.2) — 3 lưu ý kỹ thuật quan trọng:**

1. **Import path đúng**: `from botbuilder.integration.aiohttp import CloudAdapter, ConfigurationBotFrameworkAuthentication` — không phải từ `botbuilder.core` (chỉ có `CloudAdapterBase` abstract) hay `botframework.connector.auth`.

2. **Argument order ngược legacy**: `CloudAdapter.process_activity(auth_header, activity, callback)` — KHÁC `BotFrameworkAdapter.process_activity(activity, auth_header, callback)`.

3. **Tên thuộc tính config**: SDK Python đọc qua `getattr(configuration, "APP_ID")` v.v. Tên đúng (đã verify bằng cách đọc source `ConfigurationServiceClientCredentialFactory`):
   - `APP_ID` (không phải `MicrosoftAppId` hay `MICROSOFT_APP_ID`)
   - `APP_PASSWORD`
   - `APP_TYPE` (`MultiTenant`/`SingleTenant`/`UserAssignedMSI`)
   - `APP_TENANTID` (**MỘT TỪ** — không có gạch dưới giữa TENANT và ID)
   
   Sai tên → factory fallback `app_id=None` → 401 `Invalid AppId passed on token`. Code dùng `SimpleNamespace(APP_ID=…, APP_PASSWORD=…, APP_TYPE=…, APP_TENANTID=…)` cho gọn.

**Adaptive Card v1.5 Xin Nghỉ Phép** (`bot/cards/leave_request_card.json`):
- 3 inputs: `Input.Date` (start_date, isRequired), `Input.Date` (end_date), `Input.Text` (reason, multiline).
- `Action.Submit` với `data: {"action": "submit_leave_request"}`.
- Teams gửi submit dưới dạng **invoke activity** (không phải message) → xử lý ở `on_invoke_activity`. Có fallback path qua `on_message_activity` cho client cũ (kiểm tra `activity.value`).

**SSE Consumption ở Bot**:
Teams **không hỗ trợ native streaming** → bot tiêu thụ TOÀN BỘ stream SSE (timeout 120s), tích lũy tokens vào buffer, gửi 1 lần về Teams. Typing indicator gửi trước trong try/except riêng (nếu fail → log + tiếp tục, không crash handler) → UX tốt hơn.

**Token & Session Cache per Teams User**:
- Email mapping: `teams_<id_clean>@company.com` (chuẩn hóa Teams ID + TLD `.com` công khai).
- `BackendClient` singleton với `_token_cache: dict[str, str]` và `_session_cache: dict[str, str]`.
- Auto retry **1 lần** khi backend trả 401 → invalidate token cache + refetch JWT → retry request.

**Fail-safe Pattern — `_on_adapter_error`**:
In **full traceback** ra `sys.stderr` với `flush=True` (luôn visible trong `docker logs qlda_teams_bot`). Lưu ý: `logger.error(..., exc_info=True)` ngoài except-block trả `(None, None, None)` → phải truyền `exc_info=(type(e), e, e.__traceback__)` hoặc `traceback.format_exception()` trực tiếp.

---

## 3. PHÂN TÍCH MÔ HÌNH AI (Qwen2.5:7B)

### 3.1. Vì sao chọn Qwen2.5:7B?

| Tiêu chí | Đánh giá |
|---|---|
| **Tiếng Việt** | Train trên 18T tokens với corpus tiếng Việt đáng kể; chính thức hỗ trợ 29 ngôn ngữ. Qua test thực tế, vượt Llama-3-8B về độ tự nhiên và đúng ngữ pháp tiếng Việt. |
| **Context window** | Hỗ trợ tối đa 128K tokens — dư an toàn cho RAG (chỉ dùng 8K). |
| **License** | Apache-2.0 — dùng on-premise miễn phí, không hạn chế thương mại. |
| **Kích thước** | 7B params (4.7GB Q4_K_M GGUF) — vừa đủ chạy CPU và GPU consumer cấp 12GB VRAM. |
| **Hệ sinh thái** | Hỗ trợ tốt trên Ollama (one-line `ollama pull qwen2.5:7b`); cập nhật thường xuyên từ Alibaba. |
| **Instruction tuning** | Bản instruct được tinh chỉnh tốt, nghe theo system prompt — phù hợp pattern RAG. |

So sánh ngắn với các lựa chọn khác:
- **Llama 3.1 8B**: tiếng Việt khá nhưng kém Qwen, license Llama Community (hạn chế khi >700M MAU).
- **Mistral 7B**: nhỏ gọn, tốc độ tốt nhưng tiếng Việt yếu rõ rệt.
- **Vistral / PhoGPT**: tiếng Việt tự nhiên hơn nhưng hệ sinh thái yếu, ít update.

→ **Qwen2.5:7B là điểm cân bằng tối ưu giữa chất lượng tiếng Việt, license, và phần cứng.**

### 3.2. Cấu hình `num_ctx=8192` — Phân tích kỹ thuật

Mặc định Ollama dùng `num_ctx=2048` (kế thừa từ Llama). Với hệ thống RAG tiếng Việt, giá trị này **quá nhỏ**:

**Tính toán size prompt thực tế:**
| Thành phần | Ký tự | Tokens (~1 token / 2 ký tự VN) |
|---|---|---|
| System instruction (5 hướng dẫn mềm) | ~1200 | ~600 |
| 5 chunks × ~600 ký tự (sau RRF) | ~3000 | ~1500 |
| 6 messages history (3 lượt user + 3 lượt assistant, trung bình ~500 ký tự/msg) | ~3000 | ~1500 |
| Question hiện tại | ~100 | ~50 |
| **Tổng input** | **~7300** | **~3650** |
| Output budget (cho LLM trả lời) | ~3000 ký tự | ~1500 |
| **Tổng cần thiết** | | **~5150 tokens** |

→ Với `num_ctx=2048`: **prompt bị truncate phần đầu** (vì Ollama cắt từ đầu khi vượt) → mất system instruction → LLM lệch hướng, trả lời sai context.

→ Với `num_ctx=8192`: dư margin ~3000 tokens — an toàn cho mọi câu hỏi tiêu biểu, kể cả khi user hỏi câu phức tạp với nhiều history.

**Vì sao không 16K/32K?**
- Mỗi token KV cache ~0.5–1 MB trên qwen2.5:7B Q4 → 8192 tokens ≈ 6 GB RAM extra; 32K sẽ ~24 GB → vượt RAM laptop dev (16–32GB).
- LLM scan KV cache mỗi token output → context lớn = chậm hơn (O(n²) attention).
- Trade-off chọn 8192: đủ rộng cho 99% câu hỏi thực tế, không phá ngân sách RAM.

`num_ctx=8192` được set qua `settings.OLLAMA_NUM_CTX` (override được trong `.env`), truyền vào payload `/api/generate` trong field `options.num_ctx`.

### 3.3. Khả năng tiếng Việt thực tế

**Điểm mạnh quan sát được:**
- Nhận diện cấu trúc văn bản hành chính: hiểu và trích đúng "Điều 5, Khoản 2, Điểm a" theo ngữ cảnh.
- Tự nhiên trong văn phong hành chính: dùng đại từ phù hợp ("anh/chị", "Quý Công ty"), không dịch máy thô.
- Encoding Unicode hoàn chỉnh: không lỗi diacritic/thanh điệu (đ, ă, â, ê, ô, ơ, ư).

**Hạn chế đã quan sát:**
- "Lặp lại template": từng học theo câu refusal cứng → đã fix bằng prompt mềm (xem 2.4.e).
- Đôi khi dịch máy khái niệm thuần Việt ("phép năm" → "annual leave" trong câu trả lời) — cần thêm few-shot example.
- Không phân biệt rõ "Điều" (article) và "Khoản" (clause) khi câu hỏi mơ hồ — phụ thuộc vào quality của chunk retrieved.

### 3.4. Tiêu thụ tài nguyên phần cứng

| Chỉ số | Giá trị thực tế (CPU laptop) | Giá trị (GPU RTX 3060 12GB) |
|---|---|---|
| Cold-start (load weights) | 20–40s | 5–10s |
| First-token latency (prompt 4–6KB) | 5–15s | 1–3s |
| Throughput generation | 3–8 tokens/s | 25–40 tokens/s |
| RAM/VRAM (model + KV cache 8K) | ~8 GB RAM | ~7 GB VRAM |
| Disk (GGUF Q4_K_M) | 4.7 GB | 4.7 GB |
| End-to-end response (RAG question) | 60–150s | 8–20s |

**Embedding `nomic-embed-text`:**
- 137M params, 768 dim output, ~300 MB RAM.
- Batch 100 chunks: 3–5s trên CPU, <1s trên GPU.
- Lý do chọn: nhỏ gọn, đa ngôn ngữ tốt, license Apache-2.0, performance bench (MTEB) cao trong nhóm <500M.

**Vì sao timeout HTTP `read=180s`?**
- `httpx.Timeout(connect=10, read=180, write=10, pool=10)` — `read` áp cho **mỗi chunk SSE**, KHÔNG phải tổng response.
- Cold-start (20–40s) + first-token latency (5–15s) ở CPU có thể đẩy first-byte tới 60s+ → 180s là margin an toàn để không ném `httpx.ReadTimeout` giả khi LLM thực sự đang hoạt động.
- KHÔNG nên giảm dưới 120s.

### 3.5. Nhất quán Embedding giữa Ingestion và Query

`nomic-embed-text` được dùng ở **cả** ingestion (`tasks.py`) và query (`chat.py:_search_chromadb`). Đây là yêu cầu cứng: cosine similarity chỉ có nghĩa khi 2 vector cùng vector space → đổi model embedding sẽ phải re-ingest toàn bộ tài liệu. Quyết định này được commit ngay từ Phase 3 để tránh mismatch về sau.

---

## 4. KHÓ KHĂN & THÁCH THỨC (Challenges & Bottlenecks)

### 4.1. Hiệu năng phần cứng & Tốc độ Generation

**Vấn đề lớn nhất** của hệ thống hiện tại — ảnh hưởng trực tiếp đến trải nghiệm người dùng:

- LLM 7B chạy CPU-only → **throughput chỉ 3–8 tokens/s** (so với 25–40 tok/s trên GPU consumer). Câu trả lời RAG dài 200–400 tokens mất **30–80 giây** chỉ phần generation, cộng thêm 5–15s first-token latency và 20–40s cold-start (nếu Ollama vừa idle) → **end-to-end 60–150 giây cho một câu hỏi**.
- **Cold-start sau idle**: Ollama unload model khỏi RAM nếu không có request một thời gian → request đầu tiên sau idle phải reload weights. Đã giảm thiểu bằng `OLLAMA_GENERATE_TIMEOUT=180s` để tránh timeout giả.
- **Concurrency thấp**: Ollama mặc định không batch generation (mỗi prompt 1 forward pass) → user thứ 2 phải chờ user thứ 1 xong. Multi-user > 2 người sẽ đợi rõ rệt.
- **Đã tối ưu**: pipeline embed nhanh ~50× nhờ batch (50→100 chunks/request) so với LocalRecall sequential. Đây là nút thắt còn lại sau khi đã bypass LR cho ingestion.
- **Kết luận**: Tốc độ generation là rào cản số 1 cho việc đưa hệ thống lên production trên hạ tầng hiện tại — bắt buộc nâng cấp GPU (xem Section 5).

### 4.2. Độ chính xác Retrieval — TOC Trap (Mục lục thắng BM25)

**Mô tả vấn đề:**
BM25 tính score dựa trên TF-IDF của term — chunk mục lục tài liệu **literally chứa heading keyword** (ví dụ "Điều 6") + nhiều dấu chấm dotted-leader → BM25 cho điểm **rất cao** cho TOC khi user hỏi câu chứa heading.

Nhưng nội dung TOC chỉ là `"Điều 6. Tiêu đề ........ trang 8"` — **KHÔNG CÓ câu trả lời thật**. Nếu không filter, TOC thắng RRF, chiếm slot top-5 của prompt → LLM "đúng đắn" báo "không tìm thấy" vì context rỗng nội dung.

**Triệu chứng quan sát ban đầu** (trước khi fix):
- User hỏi "Quy định về thời gian nghỉ phép tại Điều 6 là gì?"
- Top-5 chunks: 4 đoạn TOC + 1 đoạn header chương → prompt rỗng nội dung
- LLM trả: "Tôi không tìm thấy thông tin này trong tài liệu" — về mặt kỹ thuật "đúng" nhưng vô dụng.

**Đã fix với `_is_toc_chunk(text)`** (`backend/routers/chat.py`):
- Phát hiện 2 dấu hiệu:
  - `dot_ratio > 0.20` (>20% ký tự là `.` — đặc trưng dotted-leader của TOC)
  - HOẶC `≥3 heading "Điều N."` liên tiếp + `alphanumeric_ratio < 0.5`
- Filter áp **TRƯỚC** RRF merge để TOC không chiếm slot top-N.
- Fallback an toàn: nếu **TẤT CẢ** chunks bị filter (case user hỏi "mục lục có những gì?") → giữ nguyên list gốc → không làm rỗng tay.

**Kết quả**: tỉ lệ trả lời sai do TOC trap giảm từ ~40% (đo qualitative trên 20 câu hỏi test) xuống gần 0% — vẫn còn các vấn đề khác (xem 4.3, 4.4).

### 4.3. Độ chính xác Retrieval — Chunk Boundary Hallucination

**Mô tả vấn đề:**
`RecursiveCharacterTextSplitter(chunk_size=2000, chunk_overlap=200)` cắt theo **ký tự cố định** → một chunk có thể chứa đoạn cuối Điều 5 + đầu Điều 6 (chunk "stitched"). Khi LLM tổng hợp, dễ "blend" thông tin của 2 điều khác nhau thành câu trả lời chéo — đây là dạng **hallucination chéo điều luật** đặc biệt nguy hiểm với văn bản hành chính có cấu trúc chặt (Điều/Khoản/Điểm).

**Ví dụ thực tế:**
- Tài liệu: Điều 5 quy định nghỉ phép năm, Điều 6 quy định nghỉ thai sản.
- Một chunk chứa: "...(cuối Điều 5) ...không quá 12 ngày/năm. **Điều 6.** Người lao động nữ được nghỉ..."
- User hỏi: "Số ngày nghỉ phép năm tối đa là bao nhiêu?"
- LLM có thể "blend" → trả: "Theo Điều 6, không quá 12 ngày/năm" — **sai cả số điều lẫn ngữ cảnh**.

**Hiện trạng:**
- Đã giảm thiểu bằng `chunk_overlap=200` (chunk kế tiếp lặp 200 ký tự cuối → giúp giữ context) + prompt yêu cầu LLM trích "Điều X, Khoản Y" cụ thể.
- **Chưa fix triệt để** — vì chunk-by-character không hiểu cấu trúc văn bản. Đề xuất Section 5.2: **Structure-aware / Semantic chunking**.

### 4.4. Hạn chế kiến trúc khác

**(a) ChromaDB ↔ Postgres Metadata Drift**
- `docker compose down -v` xóa volume `chromadb_data` (mất hết vector) NHƯNG bảng `documents` trong Postgres vẫn giữ rows với `status="done"`, `chunk_count=N`.
- Backend search trả 404 "collection không tồn tại"; Postgres bảo "đã ingest" → drift.
- Triệu chứng: hybrid retrieval `0→0` chunks, LLM trả "không tìm thấy" cho mọi câu hỏi.
- **Recovery thủ công** hiện tại: `DELETE FROM documents;` rồi chạy lại `python seed_data.py`.
- **Chưa có** cơ chế reconciliation tự động → đề xuất ở Section 5.6.

**(b) Không có Evaluation Harness**
- Chưa có **golden QA dataset** (50–100 câu hỏi điển hình + expected answers) để đo định lượng:
  - Recall@K (chunk đúng có nằm trong top-K không?)
  - MRR (Mean Reciprocal Rank)
  - Faithfulness (LLM có "bịa" thông tin không có trong context?)
  - Answer Relevance (câu trả lời có đúng vào câu hỏi?)
- Đánh giá hiện tại là **manual/qualitative** → không phát hiện regression khi thay đổi prompt/chunk size → đề xuất Section 5.5.

**(c) Token Cache Bot In-Memory**
- `BackendClient._token_cache` và `_session_cache` là **dict in-memory** → restart container `teams_bot` → mất hết token, user bị silent re-login (UX nhỏ giọt nhưng có thể bối rối user).
- Phù hợp dev nhưng prod cần **Redis-backed cache** với TTL khớp `JWT_EXPIRE_MINUTES` (480 phút).

**(d) Mock JWT — chưa phải Azure AD thật**
- Endpoint `POST /auth/mock-login` chấp nhận **bất kỳ email** → chỉ phù hợp dev. Prod cần:
  - MSAL Python validate token Azure AD qua JWKS endpoint
  - OAuth code flow / device flow
  - Refresh token rotation
- Đã thiết kế Mock theo cấu trúc dễ swap (router cô lập, schema giống Azure AD) — nhưng cần Phase 7 dành riêng.

**(e) LocalRecall Ingest Tuần tự**
- Files >20MB tốn 5–10 phút đẩy vào BM25 (LR không có batch endpoint). Chấp nhận được vì **fire-and-forget non-fatal** — user vẫn chat được (Chroma-only) khi LR chưa xong.
- Khi LR xong → hybrid retrieval tự động nâng cấp lên cả 2 engine.

**(f) Không có Rate Limiting / Circuit Breaker**
- 1 client spam `POST /api/chat` → block Ollama cho mọi user khác (Ollama không batch generation).
- Cần middleware (vd. `slowapi`) giới hạn 10 req/phút/user và circuit breaker khi Ollama timeout liên tiếp.

### 4.5. Tổng kết bảng các thách thức

| # | Thách thức | Mức độ tác động | Trạng thái |
|---|---|---|---|
| 1 | Tốc độ generation (CPU-only) | Cao | Đã giảm thiểu (timeout 180s); cần GPU |
| 2 | TOC trap trong BM25 | Cao | **Đã fix** (TOC filter + fallback) |
| 3 | Chunk boundary hallucination | Trung-Cao | Giảm thiểu (overlap 200); chưa fix triệt để |
| 4 | Chroma ↔ Postgres drift | Trung | Chỉ recovery manual |
| 5 | Thiếu evaluation harness | Trung | Chưa có |
| 6 | Token cache in-memory | Thấp-Trung | OK cho dev |
| 7 | Mock JWT | Trung (cho prod) | OK cho dev |
| 8 | Không rate limiting | Trung | Chưa có |

---

## 5. HƯỚNG NÂNG CẤP TƯƠNG LAI (Future Upgrades)

### 5.1. Bảng tổng quan ưu tiên

| # | Đề xuất | Ưu tiên | Effort | Impact dự kiến |
|---|---|---|---|---|
| 1 | Cross-Encoder Reranker | Cao | 1 tuần | +15% precision retrieval |
| 2 | Semantic / Hierarchical Chunking | Cao | 2 tuần | -30% hallucination |
| 3 | Metadata Filtering | Trung | 1 tuần | UX tốt hơn, query có scope |
| 4 | GPU Acceleration | Cao | Hạ tầng | 5–10× tốc độ generation |
| 5 | Evaluation Harness (RAGAS) | Trung | 1 tuần | Đo lường + chống regression |
| 6 | Production Hardening | Trung-Thấp | 2–4 tuần | Ổn định prod |

### 5.2. Cross-Encoder Reranker (Đề xuất ưu tiên số 1)

**Vấn đề hiện tại:**
RRF chỉ rank theo **position** trong 2 ranked list — không đánh giá semantic match thực sự giữa query và chunk. Top-5 sau RRF vẫn còn chunks lệch chủ đề.

**Đề xuất:**
Thêm một bước **rerank top-20 → top-5** bằng cross-encoder:

```
[Hybrid retrieval] → top-20 chunks → [Cross-Encoder Reranker] → top-5 → [LLM]
```

- Model đề xuất: **`bge-reranker-v2-m3`** (BAAI, đa ngôn ngữ, hỗ trợ tiếng Việt rất tốt, ~600M params).
- Cross-encoder đọc cặp `(query, chunk)` cùng lúc qua attention → score chính xác hơn bi-encoder dense (vốn encode query và doc độc lập).
- Triển khai: chạy reranker trên Ollama (model nhỏ) hoặc HF Transformers; latency ~50–200ms cho top-20 trên CPU, <30ms trên GPU.

**Lợi ích kỳ vọng**: Recall@5 tăng 10–25% (theo các benchmark BEIR), giảm hallucination từ chunks lệch chủ đề.

**Implementation sketch:**
```python
async def _rerank(query: str, candidates: list[str]) -> list[str]:
    pairs = [{"query": query, "passage": c} for c in candidates]
    scores = await rerank_client.score(pairs)  # bge-reranker
    sorted_idx = sorted(range(len(candidates)), key=lambda i: -scores[i])
    return [candidates[i] for i in sorted_idx[:5]]
```

### 5.3. Advanced Semantic / Hierarchical Chunking (Ưu tiên số 2)

**Vấn đề:** Chunking cố định 2000 ký tự gây hallucination chéo Điều luật (Section 4.3).

**Đề xuất 3 cấp:**

**(a) Structure-aware Chunking**: parse heading regex
```python
HEADING_RE = r"^(?:Điều\s+\d+\.|Khoản\s+\d+\.|Chương\s+[IVX]+|Mục\s+\d+\.)"
```
Mỗi Điều = 1 chunk parent + sub-chunks (nếu Điều quá dài, chia thành các Khoản).

**(b) Semantic Chunking** (LangChain `SemanticChunker`):
- Tính embedding của từng câu → đo cosine similarity giữa câu liền kề
- Cắt khi similarity drop dưới ngưỡng (vd. percentile 80) → ranh giới chunk = ranh giới ngữ nghĩa.

**(c) Hierarchical / Parent-Child (Small-to-Big retrieval)**:
- Lưu **chunk con** (chi tiết, ~500 ký tự) cho retrieval (dense vector chính xác hơn).
- Trả về **chunk cha** (toàn Điều, ~2000 ký tự) cho LLM context (đủ ngữ cảnh).
- Pattern này được khuyến nghị mạnh trong các công trình LangChain/LlamaIndex 2024–2025.

**Metadata kèm**: `article_number`, `chapter`, `section`, `effective_date` → cho phép filter + cite chính xác.

### 5.4. Metadata Filtering & Document-level Filters

**Hiện tại** chỉ filter `where={"doc_id": {"$eq": ...}}` cho mục đích idempotency.

**Đề xuất** — thêm các metadata fields:
- `effective_date` (Date): ngày hiệu lực — cho phép query "theo quy định mới nhất"
- `document_type` (Enum): `quy_che | huong_dan | thong_tu | quyet_dinh`
- `department` (string): "HR" / "Tài chính" / "IT"
- `version` (string): version của tài liệu
- `is_archived` (bool): để loại version cũ

**Use case**:
> "Theo quy chế HR mới nhất 2025, …"
→ Backend tự sinh filter:
```python
where={
  "$and": [
    {"document_type": "quy_che"},
    {"department": "HR"},
    {"effective_date": {"$gte": "2025-01-01"}},
    {"is_archived": False}
  ]
}
```

**Implementation**:
- Pre-extract metadata từ filename pattern (vd. `QC-HR-2025-v2.pdf`).
- Kết hợp **LLM-as-extractor** cho các trường khó (đọc 2 trang đầu tài liệu, trích metadata bằng prompt).
- Thêm UI/Bot command `/loc-tai-lieu` để user chỉ định scope.

### 5.5. GPU Acceleration (Ưu tiên số 4 — thay đổi hạ tầng)

**Vấn đề lớn nhất**: throughput 3–8 tok/s trên CPU.

**Đề xuất**:
- Chuyển sang server có GPU NVIDIA (min 12GB VRAM cho qwen2.5:7b Q4):
  - **Tối thiểu**: RTX 3060 12GB (~10–12 triệu VND)
  - **Khuyến nghị**: RTX 4070 / A4000 (~20–25 triệu VND)
  - **Production-grade**: A100/H100 (cloud rental ~$2–8/giờ)
- Throughput kỳ vọng: **25–60 tok/s** (5–10× nhanh hơn CPU) → end-to-end response **8–20 giây** cho câu hỏi RAG.

**Cấu hình Docker Compose** cho GPU:
```yaml
ollama:
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
```
Yêu cầu host cài `nvidia-container-toolkit`.

**Lựa chọn nâng cao** — thay Ollama bằng **vLLM** hoặc **TensorRT-LLM**:
- Continuous batching → throughput cao hơn 2–3× nữa khi nhiều user đồng thời.
- Hỗ trợ paged attention (KV cache hiệu quả hơn).
- Trade-off: phức tạp deploy hơn, cần config riêng cho từng model.

**Cân nhắc nâng cấp model song song**:
- Khi đã có GPU 24GB+ → có thể chạy **Qwen2.5-14B** (chất lượng tốt hơn 30–40% theo benchmark) hoặc **Qwen2.5-7B-Instruct + AWQ INT4** (cùng kích thước nhưng nhanh hơn).

### 5.6. Evaluation Harness & Continuous Improvement

**Đề xuất xây dựng**:

**(a) Golden QA Dataset** (50–100 câu hỏi):
- Phỏng vấn phòng nhân sự để thu thập câu hỏi điển hình
- Mỗi câu có: `question`, `expected_chunks` (list doc_id + article_id), `expected_answer_keywords` (5–10 keyword bắt buộc xuất hiện trong câu trả lời)

**(b) Metrics**:
- **Recall@K**: chunk đúng có nằm trong top-K không?
- **MRR**: vị trí trung bình của chunk đúng trong ranked list
- **Faithfulness** (LLM-as-judge): câu trả lời có "bịa" thông tin không có trong context?
- **Answer Relevance**: câu trả lời có đúng vào câu hỏi?
- **Latency P50/P95/P99**

**(c) Tool**: **RAGAS** (open source) — sinh tự động test set + đánh giá end-to-end.

**(d) CI tích hợp**:
- Mỗi PR thay đổi prompt/chunk size → chạy benchmark
- Không cho merge nếu Recall@5 giảm > 5% hoặc Faithfulness giảm > 3%
- Lưu kết quả qua thời gian → biểu đồ chất lượng → phát hiện regression sớm

### 5.7. Production Hardening (gói nâng cấp đa hạng mục)

**(a) Azure AD OAuth thật**:
- Thay `POST /auth/mock-login` bằng **MSAL Python**
- Token validation qua **JWKS endpoint** của Azure AD (cache JWKS keys 24h)
- Hỗ trợ refresh token rotation
- Code flow cho web, device flow cho CLI/bot

**(b) Redis-backed Bot Cache**:
- Thay in-memory dict (`BackendClient._token_cache`) → key `bot:token:{teams_id}` với TTL = 480 phút (khớp `JWT_EXPIRE_MINUTES`)
- Tương tự cho `_session_cache` → key `bot:session:{teams_id}` TTL 7 ngày
- Restart container không mất token → UX mượt

**(c) Rate Limiting**:
- Middleware FastAPI dùng `slowapi`:
  - `/api/chat`: 10 req/phút/user
  - `/api/documents/upload`: 5 req/phút/user
- Trả 429 Too Many Requests với `Retry-After` header

**(d) Observability — OpenTelemetry + Grafana**:
- Trace span cho từng bước RAG: `search_chroma`, `search_lr`, `rrf_merge`, `prompt_build`, `llm_stream` → đo bottleneck thực tế
- Metrics: request count, latency histogram per endpoint
- Logs centralized (Loki / ELK) → tìm kiếm xuyên service

**(e) Reconciliation Job (Celery beat)**:
- Mỗi đêm 02:00 chạy task: so `documents.status="done"` với `chromadb.list_collections()` + `count()` per doc_id
- Phát hiện drift → flag `Document.error_message="drift detected"` → admin re-ingest hoặc tự động enqueue `process_document_task`
- Email alert admin nếu drift > 5 documents

**(f) Document Version Control**:
- Cho phép upload lại file cùng tên → version mới (`Document.version = old.version + 1`), archive version cũ (`is_archived=True`)
- User query có thể chỉ định: "theo phiên bản nào?" hoặc mặc định lấy version mới nhất chưa archive

**(g) Multi-LLM Fallback**:
- Nếu Ollama timeout/crash → fallback sang **Claude Haiku 4.5** (qua API, nếu policy bảo mật cho phép) hoặc **Qwen2.5-3B** nhỏ hơn (dự phòng on-premise)
- Cron-based health check + auto-failover

---

## KẾT LUẬN

Hệ thống AI Trợ lý Hành chính Văn phòng đã hoàn thành **5 phase** với một kiến trúc microservices on-premise hoàn chỉnh, chứng minh tính khả thi của **RAG tiếng Việt cấp doanh nghiệp** trên hạ tầng phổ thông. Các điểm sáng kỹ thuật bao gồm: hybrid retrieval với RRF (Cormack 2009), TOC-aware filtering chống nhiễu mục lục, soft-prompt tránh hành vi "lặp template" của LLM, SSE streaming cho UX hội thoại tức thời, và tích hợp Microsoft Teams qua modern CloudAdapter + Adaptive Cards v1.5.

Các giới hạn đã xác định rõ và đều có lộ trình khắc phục cụ thể. **Ba ưu tiên đầu tư tiếp** theo thứ tự là (1) **GPU acceleration** để giải quyết nút thắt thông lượng generation, (2) **Cross-encoder reranker + structure-aware chunking** để nâng độ chính xác retrieval lên cấp production, và (3) **Evaluation harness (RAGAS)** để đo lường định lượng và chống regression. Sau khi triển khai 3 hạng mục này, hệ thống sẵn sàng cho giai đoạn pilot với phòng nhân sự và mở rộng sang các phòng ban khác.

Báo cáo này có thể được dùng làm tài liệu nền cho cuộc họp kick-off Phase 6 (Reranker + Semantic Chunking) và Phase 7 (Production Hardening + Azure AD).

---

*— Hết báo cáo —*
