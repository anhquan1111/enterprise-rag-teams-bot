-- =============================================================================
-- DEPRECATED — file này KHÔNG còn được mount vào localrecall_db container.
--
-- Lý do: LocalRecall yêu cầu extension `pg_textsearch` (Timescale BM25) chỉ
-- compile được trên PostgreSQL 17/18 — image `pgvector/pgvector:pg15` không
-- đáp ứng được. Đã chuyển sang build custom Dockerfile tại
-- ./docker/localrecall_db/Dockerfile.pgsql với entrypoint tự tạo extensions
-- (xem ./docker/localrecall_db/internal/init-db.sh).
--
-- File này được giữ lại để tham khảo lịch sử; có thể xóa an toàn.
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS vector;
