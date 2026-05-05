# MASTER PLAN: HỆ THỐNG AI TRỢ LÝ HÀNH CHÍNH VĂN PHÒNG

**Dành cho AI Coder (Claude Code)**: Bạn đang đóng vai trò là Senior Full-stack AI Engineer. Nhiệm vụ của bạn là xây dựng hệ thống này từng bước (Step-by-step). TUYỆT ĐỐI tuân thủ kiến trúc và công nghệ dưới đây. Không tự ý thay đổi thư viện hay stack công nghệ trừ khi có sự cho phép.

## 1. TỔNG QUAN KIẾN TRÚC (MICROSERVICES)

Hệ thống gồm các thành phần chạy độc lập trên Docker:

- **Tầng 1: Infrastructure & DBs:** LocalRecall (RAG Engine), Ollama (LLM - Qwen2.5-7B), ChromaDB (Vector), PostgreSQL (Data), Redis (Cache/Message Broker).
    
- **Tầng 2: Backend API (Custom Python):** FastAPI + Celery. Đây là nơi chứa toàn bộ logic nghiệp vụ (Chunking, Re-ranking, SSO, DB CRUD).
    
- **Tầng 3: Bot Client:** Microsoft Teams Bot Framework (Python SDK).
    

**Quy tắc CỐT LÕI:**

- KHÔNG sửa source code của `mudler/LocalRecall`. Hãy coi nó là một service độc lập, gọi qua REST API.
    
- Tự build Semantic Chunking (LangChain) và Re-ranking ở Backend, sau đó mới đẩy Vector vào LocalRecall/ChromaDB.
    
- Toàn bộ giao tiếp LLM phải qua cơ chế Streaming (Server-Sent Events).
    

## 2. QUY TRÌNH TRIỂN KHAI (ROADMAP)

AI Coder hãy thực hiện từng Giai đoạn (Phase) một. Phải test thành công Phase trước mới chuyển sang Phase sau.

### [PHASE 1]: THIẾT LẬP HẠ TẦNG (INFRASTRUCTURE)

**Mục tiêu:** Dựng file `docker-compose.yml` để chạy toàn bộ các dịch vụ có sẵn.

1. Tạo thư mục dự án chuẩn.
    
2. Viết `docker-compose.yml` bao gồm các services:
    
    - `postgres`: (PostgreSQL 15) Lưu dữ liệu bảng (User, Session, LeaveRequest).
        
    - `redis`: (Redis 7) Làm Celery Broker và Semantic Cache.
        
    - `chromadb`: Vector database.
        
    - `ollama`: Chạy image `ollama/ollama`. Map port 11434. Thêm script tự động pull model `qwen2.5:7b` khi khởi động.
        
    - `localrecall`: Pull image `quay.io/mudler/localrecall`. Trỏ `OPENAI_BASE_URL` về `ollama:11434` và `VECTOR_ENGINE` về `chromem` hoặc `postgres`.
        
3. Khởi chạy và đảm bảo các container giao tiếp được với nhau trong cùng một `network`.
    

### [PHASE 2]: KHỞI TẠO BACKEND & DATABASE (FASTAPI)

**Mục tiêu:** Xây dựng core Backend bằng Python (FastAPI).

1. Tạo thư mục `/backend`. Khởi tạo `requirements.txt` (fastapi, uvicorn, sqlalchemy, psycopg2-binary, redis, celery, langchain, v.v.).
    
2. Cấu hình SQLAlchemy kết nối với PostgreSQL.
    
3. Tạo các Models/Tables (ERD):
    
    - `Users`: id, email, display_name, department, role (admin/user).
        
    - `ChatSessions`: id, user_id, context.
        
    - `LeaveRequests`: id, user_id, start_date, end_date, reason, status.
        
    - `Documents`: id, filename, status (pending/done).
        
4. Viết các API CRUD cơ bản.
    
5. Setup Auth Middleware: Giả lập (Mock) JWT Token giải mã thông tin từ Azure AD (trích xuất email, name, roles).
    

### [PHASE 3]: DATA INGESTION PIPELINE (CELERY & LANGCHAIN)

**Mục tiêu:** Xử lý tài liệu PDF/Word tải lên.

1. Viết API `POST /api/documents/upload` trên FastAPI (chỉ nhận file lưu tạm và trả về 202 Accepted).
    
2. Tạo Celery Task để xử lý ngầm (Background job):
    
    - **B1 (OCR/Extract):** Đọc text từ PDF (dùng `PyMuPDF` hoặc `pdfplumber`).
        
    - **B2 (Semantic Chunking):** Sử dụng `langchain.text_splitter` (ví dụ `RecursiveCharacterTextSplitter` hoặc Semantic Splitter) để băm văn bản thành các đoạn có ý nghĩa, không làm đứt câu.
        
    - **B3 (Embedding & Store):** Gọi REST API của `LocalRecall` (`POST /api/collections/{name}/upload` hoặc tương đương) để đẩy các đoạn văn bản (chunks) đã làm sạch vào hệ thống RAG.
        
3. Cập nhật status của `Documents` trong Postgres thành "Done".
    

### [PHASE 4]: RAG ENGINE VÀ LOGIC CHATBOT (THE BRAIN)

**Mục tiêu:** Luồng suy nghĩ của AI khi nhận câu hỏi.

1. Cấu hình Redis Semantic Cache: Nhận câu hỏi -> Hash -> Kiểm tra trong Redis. Nếu độ tương đồng > 98%, trả về câu trả lời đã lưu ngay lập tức (Skip LLM).
    
2. Viết API `POST /api/chat` (Nhận câu hỏi từ User):
    
    - Gọi API Search của `LocalRecall` để truy xuất Top 5 tài liệu liên quan nhất.
        
    - **Re-ranking (Quan trọng):** Viết logic (có thể dùng cross-encoder nhỏ hoặc prompt LLM) để chọn lọc lại Top 3 đoạn tài liệu thực sự chính xác nhất.
        
    - Build Prompt Template: Ghép "Ngữ cảnh (Top 3 Chunks)" + "Lịch sử chat" + "Câu hỏi hiện tại".
        
    - Gọi API tới Ollama (Qwen2.5) với stream=True.
        
    - Trả kết quả về cho Client dưới dạng Streaming (Server-Sent Events - SSE).
        
    - Lưu trữ cặp (Câu hỏi - Câu trả lời) vào Redis Cache và Postgres.
        

### [PHASE 5]: MICROSOFT TEAMS BOT & ADAPTIVE CARDS (THE SHELL)

**Mục tiêu:** Tích hợp giao diện người dùng trên MS Teams.

1. Tạo thư mục `/bot`. Cài đặt thư viện `botbuilder-core`, `botbuilder-integration-aiohttp`.
    
2. Tạo Bot Server lắng nghe webhook từ Azure Bot Service (Port 3978).
    
3. Lập trình xử lý tin nhắn (`on_message_activity`):
    
    - Bắt tin nhắn text thông thường -> Gọi API `/api/chat` (Phase 4) -> Trả về Teams dưới dạng chữ (mô phỏng Typing/Streaming nếu có thể).
        
4. Thiết kế & Tích hợp Adaptive Cards (JSON format):
    
    - Bắt lệnh `/xin-nghi` -> Gửi thẻ Adaptive Card Form Xin Nghỉ Phép.
        
    - Lắng nghe Action Submit từ Card -> Validate logic -> Gọi Backend lưu vào Postgres -> Cập nhật Card thành "Đã gửi đơn".
        
5. Viết chức năng Broadcast: Lặp qua danh sách user ID và gửi thẻ Thông báo (Announcement Card) tới 1:1 chat.
    

**LƯU Ý DÀNH CHO CLAUDE CODE:**

- Code phải theo tiêu chuẩn PEP8, có comments rõ ràng bằng tiếng Việt.
    
- Sử dụng biến môi trường (Environment Variables) trong file `.env` cho mọi kết nối DB, API Keys.
    
- Luôn kiểm tra logs và catch exception cẩn thận để hệ thống không bị crash.