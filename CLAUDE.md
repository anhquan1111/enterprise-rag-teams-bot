Xin chào, bạn sẽ đóng vai trò là một Senior Full-stack AI Engineer. Chúng ta đang phát triển dự án "Hệ thống AI Trợ lý Hành chính Văn phòng" chạy hoàn toàn On-premise bằng kiến trúc Microservices trên Docker. 

Dưới đây là BỐI CẢNH DỰ ÁN và TÌNH TRẠNG HIỆN TẠI. Hãy đọc kỹ để đồng bộ ngữ cảnh trước khi chúng ta tiếp tục code:

### 1. KIẾN TRÚC VÀ CÁC THÀNH PHẦN ĐÃ HOÀN THIỆN
Chúng ta đã hoàn thành xuất sắc Giai đoạn 1 (Hạ tầng) và Giai đoạn 2 (Backend & Database). Hệ thống đang chạy ổn định qua `docker-compose` với các services sau:

> **QUY TẮC BẮT BUỘC — Docker Hostname:** Khi gọi API giữa các container trong cùng `qlda_network`, LUÔN LUÔN dùng **tên service** (key trong `services:` của `docker-compose.yml`) làm hostname — KHÔNG dùng `container_name`. Ví dụ: `http://ollama:11434`, KHÔNG phải `http://qlda_ollama:11434`. Tên service là tên Docker DNS thực sự; `container_name` chỉ là nhãn hiển thị.

| Service name (hostname trong Docker) | Container name (chỉ để nhận biết) | Host Port | Mô tả |
|---|---|---|---|
| `postgres` | `qlda_postgres` | 5432 | Database chính — bảng `users`, `documents`, `leave_requests`, `chat_sessions` |
| `redis` | `qlda_redis` | 6379 | Cache và Message Broker cho Celery |
| `ollama` | `qlda_ollama` | 11434 | Local LLM `qwen2.5:7b` (generate) + `nomic-embed-text` (embedding) — auto-pull cả 2 model khi khởi động |
| `localrecall` | `qlda_localrecall` | 8080 | RAG Engine — **HIỆN KHÔNG ĐƯỢC GỌI**. Pipeline đã chuyển sang query/insert ChromaDB trực tiếp. Container vẫn chạy nhưng backend/celery không phụ thuộc. |
| `chromadb` | `qlda_chromadb` | KHÔNG expose | Vector DB **được dùng làm storage chính**. Chỉ truy cập nội bộ qua `chromadb:8000`. |
| `backend` | `qlda_backend` | 8000 | FastAPI App — routers, models, schemas, Mock JWT Auth |
| `celery_worker` | `qlda_celery_worker` | — | Background worker cho Data Ingestion Pipeline |

### 2. GIAI ĐOẠN 3 ĐÃ HOÀN THÀNH: DATA INGESTION PIPELINE (Celery + ChromaDB direct)
**Pipeline thực tế** (đã bypass LocalRecall):
1. Extract text (PyMuPDF cho PDF, python-docx cho DOCX bao gồm cả bảng).
2. Chunk bằng `RecursiveCharacterTextSplitter(chunk_size=2000, chunk_overlap=200)`; loại chunk ngắn < 50 ký tự.
3. **Batch embed TẤT CẢ chunks** trong 1 (hoặc vài) HTTP call tới `POST {OLLAMA_HOST}/api/embed` với `model=nomic-embed-text` và `input=[chunks...]`. Sub-batch 100 chunks/request, retry 3 lần (5s/10s/20s).
4. **Bulk insert** vào ChromaDB qua `chromadb.HttpClient(host=CHROMADB_HOST, port=CHROMADB_PORT)`: `collection.add(ids, documents, embeddings, metadatas)` — TẤT CẢ chunks trong 1 lần. Collection metadata `{"hnsw:space": "cosine"}`.
5. Update Postgres `status=done`, `chunk_count=N`, `vector_collection_name=qlda_documents`.
6. `finally`: luôn xoá file tạm trong `UPLOAD_DIR`.

Các files chính:
- **`backend/celery_app.py`:** Celery app với Redis broker, timezone Asia/Ho_Chi_Minh, `task_acks_late=True`.
- **`backend/tasks.py`:** `process_document_task` thực hiện pipeline trên. Idempotent: trước khi `add()` sẽ `collection.delete(where={"doc_id": {"$eq": doc_id}})` để xóa chunks cũ nếu task chạy lại.
- **`backend/routers/documents.py`:** `POST /api/documents/upload` (HTTP 202), đứng TRƯỚC route `/{doc_id}`. Lưu file `UPLOAD_DIR/{uuid}_{filename}`, tạo DB record status=pending, `process_document_task.delay()`.
- **`backend/requirements.txt`:** `pymupdf`, `python-docx`, `langchain-text-splitters`, `chromadb>=1.0.0,<2.0.0`.
- **`docker-compose.yml`:** Service `celery_worker` dùng chung Dockerfile với backend (override CMD), volume `uploads_data` chia sẻ giữa backend & worker.

**Tại sao bypass LocalRecall:** LocalRecall xử lý từng embedding tuần tự qua Ollama nội bộ — không có batch endpoint. Gọi `Ollama /api/embed` thẳng với mảng inputs nhanh hơn ~50× cho file lớn.

### 3. GIAI ĐOẠN 4 ĐÃ HOÀN THÀNH: RAG ENGINE VÀ LOGIC CHATBOT
Các files mới được tạo/sửa:
- **`backend/routers/chat.py` (MỚI):** Router chính cho Phase 4. Chứa toàn bộ RAG flow và SSE streaming:
  - `_search_chromadb(query, top_k)`: Embed query bằng Ollama `/api/embed` (model `nomic-embed-text`, async qua `httpx.AsyncClient`), rồi `chromadb.HttpClient.collection.query(query_embeddings=[...], n_results=top_k)`. Sync ChromaDB call chạy trong `loop.run_in_executor` để không block event loop. Trả `[]` nếu collection chưa tồn tại — không crash endpoint.
  - `_build_rag_prompt(chunks, question, history)`: Ghép system instruction (tiếng Việt) + tối đa 3 document chunks + 6 messages lịch sử gần nhất + câu hỏi hiện tại thành một prompt hoàn chỉnh.
  - `_stream_ollama(prompt)`: Async generator gọi `POST /api/generate` trên Ollama (model `qwen2.5:7b`) với `"stream": True`. Parse từng dòng JSON newline-delimited, yield field `"response"` cho đến khi `"done": true`.
  - `_save_chat_history(db, user_id, session_id, ...)`: Sync helper append cặp `(user, assistant)` messages vào `ChatSession.context_json`. Tạo session mới nếu `session_id=None` hoặc không tìm thấy.
  - `POST /api/chat`: Endpoint chính protected bởi `get_current_user`. Tải history từ session cũ → search ChromaDB → build prompt → trả về `StreamingResponse(media_type="text/event-stream")`. Lưu DB trong `finally` block của generator, luôn gửi event `{"event": "done", "session_id": "..."}` cuối stream.
- **`backend/schemas.py` (SỬA):** Thêm `ChatRequest` schema (`message: str`, `session_id: Optional[UUID]`).
- **`backend/main.py` (SỬA):** Đăng ký `chat.router` tại prefix `/api/chat`.

**SSE Response Format (client cần parse):**
```
data: {"token": "Xin "}
data: {"token": "chào!"}
data: {"error": "Ollama không khả dụng"}   ← chỉ khi có lỗi
data: {"event": "done", "session_id": "uuid"}  ← luôn là event cuối cùng
```
**Multi-turn conversation:** Client lưu `session_id` từ event `done` và truyền lại vào field `session_id` của request tiếp theo.

### 4. GIAI ĐOẠN 5 ĐÃ HOÀN THÀNH: MICROSOFT TEAMS BOT & ADAPTIVE CARDS
Các files mới được tạo/sửa:
- **`bot/app.py` (MỚI):** `aiohttp` server lắng nghe port 3978. Route `POST /api/messages` nhận webhook từ Azure Bot Service / Emulator. **Modern adapter:** `CloudAdapter` + `ConfigurationBotFrameworkAuthentication` (cả hai import từ `botbuilder.integration.aiohttp` — KHÔNG phải `botbuilder.core` cũng KHÔNG phải `botframework.connector.auth`). Config object là `SimpleNamespace` với 4 attribute đúng tên SDK đọc: `APP_ID`, `APP_PASSWORD`, `APP_TYPE`, `APP_TENANTID` (xem mục 5 để biết tại sao tên này). `_on_adapter_error` in FULL traceback ra `sys.stderr` với `flush=True` để luôn visible trong `docker logs qlda_teams_bot`. InvokeResponse body serialize bằng `json.dumps()`. Logging level = DEBUG. Startup banner in `APP_ID[:4]`, `APP_PWD: SET/MISSING`, `APP_TYPE`, `APP_TENANT[:4]` để xác nhận `.env` đã load.
- **`bot/bot_activity_handler.py` (MỚI):** `TeamsBot(ActivityHandler)`. Helper `_safe_send()` bảo vệ mọi `turn_context.send_activity` bằng try/except+log. `on_message_activity` phân nhánh: card submit fallback → `/xin-nghi` card → RAG chat. `on_invoke_activity` xử lý Adaptive Card submit (invoke path chính). `_handle_chat`: typing indicator trong try/except riêng — nếu thất bại thì log+tiếp tục (không crash handler). `_handle_leave_request_submit`: validate → gọi `/api/leave-requests` → phản hồi kết quả.
- **`bot/backend_client.py` (MỚI):** `BackendClient` singleton. Token cache per Teams user ID (tạo email ổn định `teams_{id}@company.com` từ `from_property.id` — **PHẢI dùng TLD `.com`**, KHÔNG `.local`, xem mục 5). Session cache per Teams user ID cho multi-turn. `chat()` tiêu thụ toàn bộ SSE stream, retry 1 lần khi 401. `create_leave_request()` POST lên `/api/leave-requests`.
- **`bot/cards/leave_request_card.json` (MỚI):** Adaptive Card v1.5 — `Input.Date` (start_date, end_date), `Input.Text` (reason, multiline), `Action.Submit` với `data: {"action": "submit_leave_request"}`.
- **`bot/Dockerfile` (MỚI):** `python:3.11-slim`, port 3978.
- **`bot/requirements.txt` (MỚI):** `botbuilder-core==4.16.2`, `botbuilder-integration-aiohttp==4.16.2`, `botbuilder-schema==4.16.2`, `botframework-connector==4.16.2`, `httpx==0.27.0`, `python-dotenv==1.0.1`. **Cả 4 package botbuilder/botframework PHẢI cùng version** để tránh resolver kéo phiên bản lệch (CloudAdapter cần ≥ 4.14, nhưng pin 4.16.2 ổn định nhất).
- **`docker-compose.yml` (SỬA):** Service `teams_bot` (build `./bot`, port 3978, `depends_on: backend: healthy`, healthcheck dùng Python urllib thay vì curl). Forward 4 biến `MICROSOFT_APP_ID/PASSWORD/TYPE/TENANT_ID` từ `.env`. **KHÔNG có volume mount `./bot:/app`** — mọi thay đổi `bot/*.py` PHẢI `docker compose build teams_bot` rồi `up -d` (không hot-reload).
- **`.env` (SỬA):** Thêm `BOT_PORT=3978`, `MICROSOFT_APP_ID`, `MICROSOFT_APP_PASSWORD`, `MICROSOFT_APP_TYPE` (`MultiTenant`/`SingleTenant`/`UserAssignedMSI`), `MICROSOFT_APP_TENANT_ID` (chỉ cần khi không phải MultiTenant).
- **`seed_data.py` (MỚI, root):** Script chạy trên host, scan `./data/` tìm PDF/DOCX, mock-login lấy JWT, POST từng file lên `/api/documents/upload`, poll trạng thái Celery cho đến khi `done`.
- **`data/` (MỚI, root):** Thư mục chứa tài liệu PDF/DOCX để seed vào RAG.

**Teams Bot Architecture:**
- **Incoming:** Teams → ngrok (port 3978) → `qlda_teams_bot` container → `POST /api/messages`
- **Outgoing:** `qlda_teams_bot` → Azure Bot Service (HTTPS, auth bằng MSAL + APP_ID/PASSWORD) → Teams hiển thị reply
- **Internal:** `qlda_teams_bot` → `http://backend:8000/api/chat` (SSE, tiêu thụ toàn bộ stream) + `http://backend:8000/auth/mock-login` (lấy JWT per Teams user)

**Ngrok command:** `ngrok http 3978` → URL dạng `https://xxx.ngrok-free.app/api/messages` điền vào Teams Developer Portal → Bot Registration → Messaging endpoint.

**Container mới:**

| Service name | Container name | Host Port | Mô tả |
|---|---|---|---|
| `teams_bot` | `qlda_teams_bot` | 3978 | Teams Bot Framework server |

### 5. CÁC LƯU Ý KỸ THUẬT QUAN TRỌNG (ĐÃ FIX TỪ CÁC LỖI TRƯỚC ĐÓ)
- **Healthcheck Container:** Các image của ChromaDB và Ollama là bản minimal, không có sẵn `curl` hay `wget`. TUYỆT ĐỐI KHÔNG dùng `docker exec <container> curl...` để test bên trong container. Teams Bot dùng Python urllib trong healthcheck thay vì curl.
- **Teams Bot — Silent Bot Pattern:** Nếu bot nhận message (ngrok log HTTP 201) nhưng không phản hồi, kiểm tra `docker logs qlda_teams_bot`. Nguyên nhân gốc điển hình: `send_activity(typing)` thất bại (auth Azure/network) nhưng exception bị swallow bởi Bot Framework pipeline. Fix: typing indicator trong try/except riêng, `_on_adapter_error` in traceback ra stderr trước khi thử gửi về Teams.
- **Teams Bot — Token per User:** Mỗi Teams user có JWT token riêng (mock-login với email `teams_{cleanid}@company.com`). Token cache in-memory trong `BackendClient` singleton. Token TTL = `JWT_EXPIRE_MINUTES` (480 phút). Retry tự động khi gặp 401.
- **Teams Bot — Email TLD KHÔNG được là `.local`:** Pydantic `EmailStr` (qua `email-validator>=2.0`) reject các "special-use" TLD theo RFC 6761 (`.local`, `.localhost`, `.test`, `.invalid`, `.example`) — backend trả HTTP 422 ở field `email` trong `/auth/mock-login`. Đã từng dùng `@company.local` → fail; PHẢI dùng TLD công khai như `@company.com`.
- **Teams Bot — Modern CloudAdapter (4.14+):** Legacy `BotFrameworkAdapter` KHÔNG hiểu `MicrosoftAppType`/`MicrosoftAppTenantId` → 401 với SingleTenant app. Migrate sang `CloudAdapter` + `ConfigurationBotFrameworkAuthentication`. Lưu ý 3 điểm dễ sai:
  1. **Import path:** Cả 2 class export từ `botbuilder.integration.aiohttp` (đã verify trên 4.16.2). KHÔNG phải `botbuilder.core` (chỉ có `CloudAdapterBase` abstract). KHÔNG phải `botframework.connector.auth` (không re-export class này).
  2. **Argument order khác legacy:** `CloudAdapter.process_activity(auth_header, activity, callback)` — NGƯỢC với `BotFrameworkAdapter.process_activity(activity, auth_header, callback)`.
  3. **Tên thuộc tính config khác C# convention:** SDK Python đọc qua `getattr(configuration, "APP_ID")` v.v. trong `ConfigurationServiceClientCredentialFactory`. Đúng tên (đã verify bằng cách đọc source package): `APP_ID`, `APP_PASSWORD`, `APP_TYPE`, `APP_TENANTID` (MỘT TỪ — không có gạch dưới giữa TENANT và ID). Sai tên (`MicrosoftAppId`, `MICROSOFT_APP_ID`, `APP_TENANT_ID`...) → factory fallback `app_id=None` → `Unauthorized. Invalid AppId passed on token`. Dùng `SimpleNamespace(APP_ID=..., APP_PASSWORD=..., APP_TYPE=..., APP_TENANTID=...)` cho gọn.
- **Teams Bot — Adaptive Card Submit:** Teams gửi Adaptive Card submit dưới dạng `invoke` activity (không phải `message`). Data form merge với `data` field của `Action.Submit` button. Xử lý trong `on_invoke_activity`. Fallback path qua `on_message_activity` cho client cũ (kiểm tra `activity.value`).
- **Teams Bot — SSE Consumption:** Teams không hỗ trợ native streaming. Bot tiêu thụ TOÀN BỘ SSE stream từ `/api/chat` (tối đa 120s), tích lũy tokens, rồi gửi một lần về Teams. Typing indicator gửi trước để UX tốt hơn.
- **InvokeResponse Body:** Luôn dùng `json.dumps(body or {})` — `str(dict)` trong Python dùng single-quote không phải JSON chuẩn.
- **`_on_adapter_error` và `exc_info`:** `logger.error(..., exc_info=True)` NGOÀI except-block không bắt được traceback vì `sys.exc_info()` trả `(None, None, None)`. Phải dùng `exc_info=(type(e), e, e.__traceback__)` hoặc `traceback.format_exception()` trực tiếp.
- **Xung đột phiên bản thư viện:** Đã từng xảy ra xung đột dependency giữa LangChain và SQLAlchemy. Hãy cực kỳ cẩn thận và chọn phiên bản tương thích khi thêm thư viện vào `requirements.txt`.
- **Thiết kế Luồng dữ liệu RAG:** Backend dùng `langchain-text-splitters` (chỉ phần `RecursiveCharacterTextSplitter`) để chunk, embed bằng Ollama `/api/embed` (`nomic-embed-text`, batch), rồi đẩy thẳng vào ChromaDB qua `chromadb.HttpClient`. **KHÔNG** dùng LangChain `Chroma` vectorstore wrapper, **KHÔNG** dùng LocalRecall cho ingestion/search.
- **ChromaDB client/server version pin:** Client Python pin `chromadb>=1.0.0,<2.0.0` trong `backend/requirements.txt`; server `chromadb/chroma:latest` (hiện 1.4.3). **Phải cùng major version** — mismatch (vd. client 0.x ↔ server 1.x) sẽ ném `KeyError: '_type'` ở `CollectionConfigurationInternal.from_json` vì server 1.x bỏ field discriminator `_type`. Khi bump version một bên, bump bên còn lại.
- **Embedding vs Generation Models:** `nomic-embed-text` cho embedding (768-dim, dùng ở cả ingestion và query), `qwen2.5:7b` cho generation. Cả 2 được auto-pull bởi entrypoint của service `ollama` trong docker-compose. Model embedding ở ingestion và query PHẢI giống nhau (cosine similarity yêu cầu cùng vector space).
- **Celery Session:** Celery task tự tạo `SessionLocal()` trực tiếp — KHÔNG dùng `get_db()` generator của FastAPI vì không có request context.
- **Route Order:** Endpoint `/upload` PHẢI đứng TRƯỚC `/{doc_id}` trong router để FastAPI không parse chuỗi "upload" thành UUID.
- **Collection Vector:** Tất cả tài liệu dùng chung collection `qlda_documents` trên **ChromaDB** (hằng số `settings.LOCALRECALL_COLLECTION` — tên biến giữ legacy nhưng giá trị dùng cho ChromaDB). Field `Document.vector_collection_name` lưu tên collection. Metadata mỗi chunk gồm `source`, `doc_id`, `chunk_index` — filter qua `where={"doc_id": {"$eq": ...}}`.
- **Ollama Cold-start Timeout:** Model `qwen2.5:7b` cần 20–40 giây để load vào RAM lần đầu. `httpx.AsyncClient(timeout=60.0)` trong `_stream_ollama` là bắt buộc — KHÔNG giảm xuống dưới 60 giây.
- **SSE và DB Session:** `_save_chat_history` được gọi trong `finally` block của async generator `event_stream()`. Tại thời điểm đó, `db` session từ `Depends(get_db)` vẫn còn hợp lệ vì FastAPI chỉ chạy dependency cleanup SAU KHI generator đã exhaust hoàn toàn.
- **StreamingResponse không trả response_model:** Endpoint `POST /api/chat` khai báo `response_class=StreamingResponse` — Swagger UI sẽ không render schema response, đây là hành vi đúng của SSE endpoint.
- **Ollama API format:** `/api/generate` trả về newline-delimited JSON (mỗi dòng là một object `{"response": "token", "done": false}`). KHÔNG phải `data:` SSE format — phải tự wrap thành SSE khi yield ra client.