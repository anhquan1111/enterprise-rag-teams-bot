#!/usr/bin/env python3
"""
seed_data.py - Upload tài liệu PDF/DOCX vào hệ thống RAG.

CÁCH DÙNG:
    1. Đặt các file PDF / DOCX vào thư mục  ./data/
    2. Đảm bảo backend đang chạy:  docker compose up -d --build
    3. Chạy script:                 python seed_data.py

YÊU CẦU (trên máy host, không cần Docker):
    pip install requests

BIẾN MÔI TRƯỜNG (tuỳ chọn):
    BACKEND_URL   — mặc định: http://localhost:8000
    ADMIN_EMAIL   — mặc định: admin@company.com
    ADMIN_NAME    — mặc định: System Admin
"""

import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("[LOI] Thieu thu vien 'requests'. Cai dat: pip install requests")
    sys.exit(1)

# =============================================================================
# CẤU HÌNH
# =============================================================================
BACKEND_URL: str = os.getenv("BACKEND_URL", "http://localhost:8000")
DATA_DIR: Path = Path(__file__).parent / "data"
SUPPORTED_EXT: frozenset = frozenset({".pdf", ".docx", ".doc"})

ADMIN_EMAIL: str = os.getenv("ADMIN_EMAIL", "admin@company.com")
ADMIN_NAME: str = os.getenv("ADMIN_NAME", "System Admin")


# =============================================================================
# HELPERS
# =============================================================================

def _print_section(title: str):
    width = 60
    print(f"\n{'='*width}")
    print(f"  {title}")
    print(f"{'='*width}")


def get_auth_token(session: requests.Session) -> str:
    """Đăng nhập bằng mock-login, trả về JWT access_token."""
    resp = session.post(
        f"{BACKEND_URL}/auth/mock-login",
        json={
            "email": ADMIN_EMAIL,
            "full_name": ADMIN_NAME,
            "department": "IT",
            "role": "admin",
        },
        timeout=30,
    )
    resp.raise_for_status()
    token = resp.json()["access_token"]
    print(f"  [OK] Dang nhap thanh cong: {ADMIN_EMAIL}")
    return token


def get_existing_filenames(session: requests.Session, token: str) -> set:
    """
    Lấy danh sách tên file đã tồn tại trong DB từ GET /api/documents/.
    Dùng để kiểm tra idempotency — bỏ qua file đã upload trước đó.

    Returns:
        Set tên file đã có trong DB (bao gồm cả trạng thái pending/processing/done/failed).
    """
    try:
        resp = session.get(
            f"{BACKEND_URL}/api/documents/",
            headers={"Authorization": f"Bearer {token}"},
            params={"page_size": 200},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        filenames = {item["filename"] for item in data.get("items", [])}
        if filenames:
            print(f"  [INFO] DB da co {len(filenames)} tai lieu: {', '.join(sorted(filenames))}")
        return filenames
    except Exception as exc:
        print(f"  [WARN] Khong lay duoc danh sach tai lieu hien tai: {exc}")
        return set()


def upload_file(session: requests.Session, file_path: Path, token: str) -> dict:
    """Upload mot file len backend (POST /api/documents/upload)."""
    with open(file_path, "rb") as fh:
        resp = session.post(
            f"{BACKEND_URL}/api/documents/upload",
            files={"file": (file_path.name, fh, "application/octet-stream")},
            headers={"Authorization": f"Bearer {token}"},
            timeout=120,
        )
    resp.raise_for_status()
    return resp.json()


def get_document_status(session: requests.Session, doc_id: str, token: str) -> str:
    """Kiem tra trang thai xu ly cua mot tai lieu."""
    resp = session.get(
        f"{BACKEND_URL}/api/documents/{doc_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("status", "unknown")


def wait_for_processing(
    session: requests.Session,
    doc_id: str,
    token: str,
    filename: str,
    max_wait: int = 1800,
    poll_interval: int = 10,
) -> str:
    """
    Poll trang thai doc_id cho den khi 'done' hoac 'failed', hoac het timeout.
    max_wait=1800 (30 phut) cho phep xu ly file lon.

    Returns: trang thai cuoi cung ('done', 'failed', 'timeout')
    """
    elapsed = 0
    last_status = "pending"
    while elapsed < max_wait:
        try:
            status = get_document_status(session, doc_id, token)
            last_status = status
            if status in ("done", "failed"):
                print()  # Xuat xuong dong sau dong \r
                return status
            mins, secs = divmod(elapsed, 60)
            print(
                f"  [{mins:02d}:{secs:02d}] {filename[:35]:<35} status={status:<12}",
                end="\r", flush=True,
            )
        except Exception as exc:
            print(f"\n  [WARN] Poll loi: {exc}")
        time.sleep(poll_interval)
        elapsed += poll_interval

    print()  # Xuong dong
    print(
        f"  [TIMEOUT] {filename} chua xong sau {max_wait//60} phut "
        f"(status cuoi: {last_status}). Kiem tra: docker logs qlda_celery_worker --tail=30"
    )
    return "timeout"


# =============================================================================
# MAIN
# =============================================================================

def main():
    _print_section("SEED DATA — HE THONG AI TRO LY HANH CHINH")

    # --- Kiem tra thu muc data ---
    if not DATA_DIR.exists():
        DATA_DIR.mkdir(parents=True)
        print(f"\n  [INFO] Da tao thu muc: {DATA_DIR.resolve()}")

    files = sorted(
        f for f in DATA_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in SUPPORTED_EXT
    )

    if not files:
        print(f"\n  [INFO] Khong tim thay file nao trong: {DATA_DIR.resolve()}")
        print(f"  [INFO] Ho tro dinh dang: {', '.join(sorted(SUPPORTED_EXT))}")
        print("\n  Huong dan:")
        print(f"    1. Copy cac file PDF / DOCX vao thu muc: {DATA_DIR.resolve()}")
        print( "    2. Chay lai script nay: python seed_data.py")
        return

    print(f"\n  Backend : {BACKEND_URL}")
    print(f"  Thu muc : {DATA_DIR.resolve()}")
    print(f"\n  Tim thay {len(files)} file:")
    for f in files:
        size_kb = f.stat().st_size / 1024
        print(f"    - {f.name:40s} ({size_kb:>8.1f} KB)")

    # --- Ket noi va dang nhap ---
    _print_section("BUOC 1: Ket noi Backend")
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    try:
        health = session.get(f"{BACKEND_URL}/health", timeout=10)
        health.raise_for_status()
        print(f"  [OK] Backend online: {health.json()}")
    except requests.ConnectionError:
        print(f"\n  [LOI] Khong ket noi duoc Backend tai: {BACKEND_URL}")
        print("  [LOI] Dam bao 'docker compose up -d --build' da chay xong.")
        sys.exit(1)
    except Exception as exc:
        print(f"\n  [LOI] Healthcheck that bai: {exc}")
        sys.exit(1)

    try:
        token = get_auth_token(session)
    except requests.HTTPError as exc:
        print(f"\n  [LOI] Dang nhap that bai: HTTP {exc.response.status_code}")
        print(f"  Chi tiet: {exc.response.text[:300]}")
        sys.exit(1)
    except Exception as exc:
        print(f"\n  [LOI] Dang nhap that bai: {exc}")
        sys.exit(1)

    # --- Lay danh sach file da ton tai (Idempotency check) ---
    existing_filenames = get_existing_filenames(session, token)

    # --- Upload tung file (bo qua neu da ton tai) ---
    _print_section(f"BUOC 2: Upload {len(files)} tai lieu")
    results = []

    for idx, file_path in enumerate(files, 1):
        size_kb = file_path.stat().st_size / 1024
        print(f"\n  [{idx}/{len(files)}] {file_path.name} ({size_kb:.1f} KB)")

        # --- Idempotency: bo qua neu da upload truoc do ---
        if file_path.name in existing_filenames:
            print(f"         → [SKIP] '{file_path.name}' da ton tai trong DB. Bo qua.")
            results.append({"file": file_path.name, "doc_id": None, "status": "skipped"})
            continue

        try:
            resp = upload_file(session, file_path, token)
            doc_id = resp["document_id"]
            task_id = resp["task_id"]
            print(f"         → HTTP 202 Accepted")
            print(f"         → Document ID : {doc_id}")
            print(f"         → Celery Task : {task_id}")
            results.append({"file": file_path.name, "doc_id": doc_id, "status": "accepted"})

            # Delay 2s giua cac lan upload de tranh overwhelm worker
            if idx < len(files):
                time.sleep(2)

        except requests.HTTPError as exc:
            code = exc.response.status_code
            detail = exc.response.text[:300]
            print(f"         → [LOI] HTTP {code}: {detail}")
            results.append({"file": file_path.name, "doc_id": None, "status": f"http_{code}"})

        except Exception as exc:
            print(f"         → [LOI] {type(exc).__name__}: {exc}")
            results.append({"file": file_path.name, "doc_id": None, "status": "error"})

    # --- Theo doi xu ly bat dong bo ---
    accepted = [r for r in results if r["doc_id"]]
    if accepted:
        _print_section("BUOC 3: Theo doi xu ly (Celery + Ollama + ChromaDB)")
        print(f"  {len(accepted)} tai lieu dang duoc xu ly bat dong bo.")
        print(f"  Poll moi 10 giay, toi da 30 phut moi file.\n")
        print(f"  Monitor real-time: docker logs qlda_celery_worker --follow --tail=20\n")

        for r in accepted:
            print(f"  Dang cho: {r['file']}")
            final_status = wait_for_processing(
                session, r["doc_id"], token, r["file"],
            )
            r["status"] = final_status
            icon = "DONE" if final_status == "done" else "FAIL" if final_status == "failed" else "TIMEOUT"
            print(f"  [{icon}] {r['file']:<45} (id={r['doc_id']})")

    # --- Ket qua tong hop ---
    _print_section("KET QUA")
    done_count    = sum(1 for r in results if r["status"] == "done")
    skipped_count = sum(1 for r in results if r["status"] == "skipped")
    error_count   = sum(
        1 for r in results
        if r["status"] in ("failed", "error", "timeout") or r["status"].startswith("http_")
    )

    print(f"  Tong so file    : {len(results)}")
    print(f"  Thanh cong      : {done_count}")
    print(f"  Da ton tai/skip : {skipped_count}")
    print(f"  That bai        : {error_count}")

    print("\n  Chi tiet:")
    for r in results:
        if r["status"] == "done":
            icon = "OK"
        elif r["status"] == "skipped":
            icon = "--"
        elif r["status"] in ("pending", "processing"):
            icon = "??"
        else:
            icon = "X"
        print(f"    [{icon}] {r['file']:<40s} status={r['status']}")

    if done_count > 0:
        print(f"\n  [SUCCESS] {done_count} tai lieu da duoc index vao ChromaDB.")
        print("  Chatbot co the su dung noi dung nay de tra loi cau hoi ngay bay gio!")

    if error_count > 0:
        print(f"\n  [WARN] {error_count} file that bai. Kiem tra logs:")
        print("    docker logs qlda_celery_worker --tail=50")
        print("    docker logs qlda_ollama --tail=20")


if __name__ == "__main__":
    main()
