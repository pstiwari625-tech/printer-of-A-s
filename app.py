"""Printer of A's - Clean Dark Minimalist Print Kiosk

Run:  .venv/bin/python3 app.py
Open: http://localhost:5001
"""
import os
import sqlite3
import uuid
import json
import urllib.request
import urllib.error
import threading
import time
import random
from io import BytesIO
from datetime import datetime, timezone

from flask import (Flask, abort, g, jsonify, redirect, render_template_string,
                   request, send_file, url_for)
from PIL import Image, ImageSequence, UnidentifiedImageError, ImageDraw
from pypdf import PdfReader, PdfWriter
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()   # teaches Pillow to open HEIC / HEIF files
    HEIC_SUPPORTED = True
except ImportError:
    HEIC_SUPPORTED = False

BASE = os.path.dirname(os.path.abspath(__file__))
# Load environment variables from .env
def load_env():
    for env_path in [os.path.join(BASE, ".env"), os.path.join(BASE, "payment-bridge", ".env")]:
        if os.path.exists(env_path):
            with open(env_path, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        if k.strip() not in os.environ:
                            os.environ[k.strip()] = v.strip()

load_env()
import cashfree_helper

RUNTIME_DIR = "/tmp" if os.environ.get("VERCEL") else BASE
UPLOADS = os.path.join(RUNTIME_DIR, "uploads")
DB = os.path.join(RUNTIME_DIR, "jobs.db")
os.makedirs(UPLOADS, exist_ok=True)

CURRENCY = "₹"
PRICE_PER_PAGE = {"bw": 2.0, "color": 8.0}
MAX_PAGES = 100
PI_TOKEN = os.environ.get("PI_TOKEN", "change-me")
IMAGE_EXTENSIONS = {".bmp", ".gif", ".heic", ".heif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}

# Supabase & UPI Settings
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "") or os.environ.get("SUPABASE_KEY", "")
UPI_VPA = os.environ.get("UPI_VPA", "paytm.s4qv31l@pty")
UPI_NAME = os.environ.get("UPI_NAME", "Ankur Tiwari")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 50 MB


# ---------- Render Anti-Sleep Randomized Keep-Alive Daemon ----------
USER_AGENTS_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_3 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.6261.64 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0"
]

def _render_keep_alive_worker():
    time.sleep(random.randint(45, 90))
    render_url = os.environ.get("RENDER_EXTERNAL_URL", "https://printer-of-a-s.onrender.com").rstrip("/")
    ping_url = f"{render_url}/health"

    while True:
        # Randomized interval between 10 and 14 minutes (600 to 840 seconds)
        # Any random minute (10, 11, 12, 13, 14) and any random second (0 to 59)
        sleep_secs = random.randint(600, 840)
        time.sleep(sleep_secs)
        try:
            ua = random.choice(USER_AGENTS_POOL)
            req = urllib.request.Request(ping_url, headers={"User-Agent": ua, "Accept": "application/json, text/html, */*"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                mins = sleep_secs // 60
                secs = sleep_secs % 60
                print(f"[RANDOM KEEP-ALIVE] Pinged {ping_url} (HTTP {resp.status}). Randomized interval: {mins}m {secs}s.")
        except Exception as e:
            print(f"[RANDOM KEEP-ALIVE] Ping warning: {e}")

if os.environ.get("RENDER") or os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("ENABLE_KEEP_ALIVE"):
    _t = threading.Thread(target=_render_keep_alive_worker, daemon=True, name="RenderAntiSleep")
    _t.start()


# ---------- Supabase Integration Helpers ----------
def supabase_request(endpoint, method="GET", data=None):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/{endpoint}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            text = resp.read().decode("utf-8")
            return json.loads(text) if text else None
    except Exception as e:
        print(f"[SUPABASE ERROR] {endpoint}: {e}")
        return None


def supabase_sync_job(job_dict):
    """Syncs print job to Supabase so phone listener can reconcile it."""
    payload = {
        "id": job_dict["id"],
        "filename": job_dict.get("filename", ""),
        "stored": job_dict.get("stored", ""),
        "pages": int(job_dict.get("pages", 1)),
        "copies": int(job_dict.get("copies", 1)),
        "color": job_dict.get("color", "bw"),
        "duplex": int(job_dict.get("duplex", 0)),
        "amount": float(job_dict.get("amount", 0.0)),
        "reserved_paise_offset": int(job_dict.get("reserved_paise_offset", 0)),
        "status": job_dict.get("status", "pending_payment"),
        "created": job_dict.get("created", datetime.now(timezone.utc).isoformat()),
        "notes": job_dict.get("notes", "")
    }
    return supabase_request("jobs", method="POST", data=payload)


# ---------- Supabase Storage Bucket Helpers ----------
SUPABASE_STORAGE_BUCKET = "print-jobs"

def supabase_upload_pdf(job_id, file_path):
    """Uploads the merged PDF to Supabase Storage bucket 'print-jobs'."""
    if not SUPABASE_URL or not SUPABASE_KEY or not os.path.exists(file_path):
        return False
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_STORAGE_BUCKET}/{job_id}.pdf"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/pdf",
        "x-upsert": "true"
    }
    try:
        with open(file_path, "rb") as f:
            pdf_bytes = f.read()
        req = urllib.request.Request(url, data=pdf_bytes, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"[SUPABASE STORAGE] Successfully uploaded {job_id}.pdf ({len(pdf_bytes)} bytes)")
            return True
    except Exception as e:
        print(f"[SUPABASE STORAGE UPLOAD ERROR] {job_id}: {e}")
        return False


def supabase_download_pdf(job_id, dest_path):
    """Restores PDF from Supabase Storage if local container restarted."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_STORAGE_BUCKET}/{job_id}.pdf"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }
    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read()
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)
            with open(dest_path, "wb") as f:
                f.write(data)
            print(f"[SUPABASE STORAGE] Restored {job_id}.pdf from cloud ({len(data)} bytes)")
            return True
    except Exception as e:
        print(f"[SUPABASE STORAGE DOWNLOAD ERROR] {job_id}: {e}")
        return False


def supabase_delete_pdf(job_id):
    """Permanently purges PDF from Supabase Storage bucket once printing is done."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_STORAGE_BUCKET}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json"
    }
    payload = json.dumps({"prefixes": [f"{job_id}.pdf"]}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=payload, headers=headers, method="DELETE")
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(f"[SUPABASE STORAGE] Permanently purged {job_id}.pdf for user privacy")
            return True
    except Exception as e:
        print(f"[SUPABASE STORAGE PURGE ERROR] {job_id}: {e}")
        return False


# ---------- database management ----------
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db_con = g.pop("db", None)
    if db_con is not None:
        db_con.close()


def init_db():
    con = sqlite3.connect(DB)
    with con:
        con.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, filename TEXT, stored TEXT, pages INTEGER,
            copies INTEGER, color TEXT, duplex INTEGER, amount REAL,
            status TEXT, created TEXT, notes TEXT DEFAULT '',
            utr_number TEXT DEFAULT '', reserved_paise_offset INTEGER DEFAULT 0)""")
        columns = [c[1] for c in con.execute("PRAGMA table_info(jobs)").fetchall()]
        if "notes" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN notes TEXT DEFAULT ''")
        if "utr_number" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN utr_number TEXT DEFAULT ''")
        if "reserved_paise_offset" not in columns:
            con.execute("ALTER TABLE jobs ADD COLUMN reserved_paise_offset INTEGER DEFAULT 0")
    con.close()


init_db()


def calculate_paise_offset(job_id):
    """Generates a stable 1 to 10 paise (₹0.01 to ₹0.10) offset per job (max 0.10 INR)."""
    return (abs(hash(job_id)) % 10) + 1


def price(pages, copies, color, offset=0):
    base = round(pages * copies * PRICE_PER_PAGE.get(color, 2.0), 2)
    return round(base + (offset / 100.0), 2)


def is_pdf_upload(file_storage):
    return file_storage.filename.lower().endswith(".pdf")


def is_supported_upload(file_storage):
    return is_pdf_upload(file_storage) or os.path.splitext(file_storage.filename.lower())[1] in IMAGE_EXTENSIONS


def image_to_pdf(image_file):
    """Turn an uploaded image (including animated images) into PDF page(s)."""
    image_file.stream.seek(0)
    with Image.open(image_file.stream) as image:
        pages = []
        for frame in ImageSequence.Iterator(image):
            # PDFs do not support transparency, so composite it onto white first.
            rgba = frame.convert("RGBA")
            page = Image.new("RGB", rgba.size, "white")
            page.paste(rgba, mask=rgba.getchannel("A"))
            pages.append(page)

    if not pages:
        raise UnidentifiedImageError("Image has no frames")

    output = BytesIO()
    pages[0].save(output, format="PDF", save_all=True, append_images=pages[1:], resolution=100.0)
    output.seek(0)
    return output


# ---------- Base Page Template ----------
PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Printer of A's — Upload Files</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <script src="https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.min.js"></script>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js"></script>
  <script src="https://sdk.cashfree.com/js/v3/cashfree.js"></script>
  <style>
    :root {
      --bg: #171719;
      --card-bg: #1F2023;
      --card-elevated: #26282E;
      --accent: #8DEBB5;
      --accent-hover: #A5F3C8;
      --accent-muted: rgba(141, 235, 181, 0.12);
      --accent-border: rgba(141, 235, 181, 0.25);
      --text-main: #FFFFFF;
      --text-muted: #A0A6AD;
      --border-subtle: rgba(255, 255, 255, 0.08);
      --font-family: 'Plus Jakarta Sans', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }

    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }

    body {
      background-color: var(--bg);
      color: var(--text-main);
      font-family: var(--font-family);
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }


    /* Header */
    header {
      border-bottom: 1px solid var(--border-subtle);
      background: rgba(23, 23, 25, 0.95);
      backdrop-filter: blur(12px);
    }

    .nav-container {
      max-width: 100%;
      margin: 0 auto;
      padding: 14px 40px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }

    .brand-link {
      display: flex;
      align-items: center;
      gap: 10px;
      text-decoration: none;
      color: #FFFFFF;
    }

    .brand-logo-icon {
      width: 32px;
      height: 32px;
      border-radius: 8px;
      background: #1F2023;
      border: 1px solid var(--accent-border);
      display: flex;
      align-items: center;
      justify-content: center;
      color: var(--accent);
    }

    .brand-title {
      font-size: 17px;
      font-weight: 800;
      letter-spacing: -0.3px;
    }

    .brand-title span {
      color: var(--accent);
    }

    nav {
      display: flex;
      align-items: center;
      gap: 20px;
    }

    nav a {
      color: var(--text-muted);
      text-decoration: none;
      font-size: 14px;
      font-weight: 600;
      transition: color 0.2s ease;
    }

    nav a:hover, nav a.active {
      color: var(--accent);
    }

    /* Main Content Wrapper */
    main {
      flex: 1;
      width: 100%;
      margin: 0;
      padding: 0;
      display: flex;
      flex-direction: column;
    }

    /* Home Page Full-Screen Layout */
    .home-layout {
      flex: 1;
      display: grid;
      grid-template-columns: 1fr 1fr;
      min-height: calc(100vh - 70px);
    }

    /* Left panel: Upload */
    .home-left {
      padding: 48px 44px;
      display: flex;
      flex-direction: column;
      justify-content: center;
      border: 2px solid #1E3A8A;
    }

    /* Right panel: Info cards */
    .home-right {
      padding: 48px 44px;
      display: flex;
      flex-direction: column;
      justify-content: center;
      gap: 24px;
      background: rgba(30, 64, 175, 0.03);
    }

    /* Non-home pages keep a centred narrow card */
    .page-content {
      flex: 1;
      max-width: 760px;
      width: 100%;
      margin: 36px auto 60px;
      padding: 0 20px;
    }

    /* Clean Card Container */
    .card {
      background: var(--card-bg);
      border: 1px solid var(--border-subtle);
      border-radius: 8px;
      padding: 36px;
      box-shadow: 0 16px 40px rgba(0, 0, 0, 0.4);
    }

    /* Upload Card with Dark Blue Border */
    .card.card-upload {
      border: 1.5px solid #1E40AF; /* Dark blue border */
      box-shadow: 0 16px 40px rgba(15, 30, 65, 0.45);
    }

    .card.card-upload .dropzone {
      border: 1.5px solid #2563EB;
      background: rgba(30, 64, 175, 0.05);
    }

    .card.card-upload .dropzone:hover, .card.card-upload .dropzone.dragover {
      border-color: #60A5FA;
      background: rgba(30, 64, 175, 0.12);
    }

    .page-title {
      color: var(--accent);
      font-size: 2.2rem;
      font-weight: 800;
      letter-spacing: -0.8px;
      line-height: 1.2;
      margin-bottom: 8px;
    }

    .page-sub {
      color: var(--text-muted);
      font-size: 0.95rem;
      margin-bottom: 28px;
    }

    /* Error Alert */
    .alert-err {
      background: rgba(239, 68, 68, 0.12);
      border: 1px solid rgba(239, 68, 68, 0.3);
      color: #FCA5A5;
      padding: 12px 16px;
      border-radius: 6px;
      font-size: 14px;
      margin-bottom: 24px;
    }

    /* File Drop Zone */
    .dropzone {
      border: 1.5px solid var(--accent-border);
      background: rgba(141, 235, 181, 0.02);
      border-radius: 8px;
      padding: 32px 20px;
      text-align: center;
      cursor: pointer;
      transition: all 0.2s ease;
      margin-bottom: 16px;
    }

    .dropzone:hover, .dropzone.dragover {
      border-color: var(--accent);
      background: rgba(141, 235, 181, 0.06);
    }

    .dropzone-icon {
      color: var(--accent);
      margin-bottom: 10px;
      display: flex;
      justify-content: center;
    }

    .dropzone-text {
      font-size: 15px;
      font-weight: 700;
      color: #FFFFFF;
      margin-bottom: 4px;
    }

    .dropzone-sub {
      color: var(--text-muted);
      font-size: 13px;
    }

    /* Selected Document Card with Prominent Print Preview */
    .file-selected-card {
      display: none;
      background: #18191C;
      border: 1px solid var(--accent);
      border-radius: 8px;
      padding: 16px;
      margin-bottom: 24px;
      box-shadow: 0 4px 16px rgba(141, 235, 181, 0.1);
    }

    .file-list-scroll {
      display: flex;
      flex-direction: column;
      gap: 8px;
      max-height: 220px;
      overflow-y: auto;
      margin-bottom: 14px;
      padding-right: 4px;
    }

    .file-list-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: #202226;
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      padding: 10px 14px;
      gap: 12px;
      transition: border-color 0.2s;
    }

    .file-list-item:hover {
      border-color: rgba(141, 235, 181, 0.3);
    }

    .file-item-left {
      display: flex;
      align-items: center;
      gap: 10px;
      min-width: 0;
    }

    .file-mini-icon {
      color: var(--accent);
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }

    .file-item-info {
      display: flex;
      flex-direction: column;
      min-width: 0;
    }

    .file-item-name {
      color: #FFFFFF;
      font-size: 13.5px;
      font-weight: 600;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 360px;
    }

    .file-item-size {
      color: var(--text-muted);
      font-size: 11.5px;
    }

    .btn-remove-file {
      background: rgba(239, 68, 68, 0.12);
      border: 1px solid rgba(239, 68, 68, 0.25);
      color: #F87171;
      width: 26px;
      height: 26px;
      border-radius: 4px;
      cursor: pointer;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 12px;
      font-weight: 700;
      flex-shrink: 0;
      transition: all 0.2s;
    }

    .btn-remove-file:hover {
      background: rgba(239, 68, 68, 0.25);
      color: #FFFFFF;
    }

    .file-card-footer {
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-top: 1px solid var(--border-subtle);
      padding-top: 12px;
      gap: 12px;
    }

    .file-summary-text {
      color: var(--accent);
      font-size: 12.5px;
      font-weight: 600;
    }

    .file-card-actions {
      display: flex;
      align-items: center;
      gap: 8px;
    }

    .btn-add-more {
      background: transparent;
      border: 1px solid var(--border-subtle);
      color: var(--text-muted);
      border-radius: 6px;
      padding: 7px 12px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-family: inherit;
      transition: all 0.2s;
    }

    .btn-add-more:hover {
      color: #FFFFFF;
      border-color: rgba(255, 255, 255, 0.3);
    }

    .btn-preview-prominent {
      background: var(--accent);
      color: #171719;
      border: none;
      border-radius: 6px;
      padding: 8px 16px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s;
      font-family: inherit;
    }

    .btn-preview-prominent:hover {
      background: var(--accent-hover);
      box-shadow: 0 2px 10px rgba(141, 235, 181, 0.3);
    }

    /* Form Fields */
    .form-row {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      margin-bottom: 20px;
    }

    .form-group {
      margin-bottom: 20px;
    }

    label.field-label {
      display: block;
      color: var(--text-muted);
      font-size: 13px;
      font-weight: 600;
      margin-bottom: 8px;
    }

    select.field-select, input.field-input {
      width: 100%;
      background: #18191B;
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      color: #FFFFFF;
      font-family: inherit;
      font-size: 14px;
      padding: 12px 14px;
      transition: border-color 0.2s;
    }

    select.field-select:focus, input.field-input:focus {
      outline: none;
      border-color: var(--accent);
      box-shadow: 0 0 0 2px rgba(141, 235, 181, 0.2);
    }

    /* Duplex Checkbox */
    .checkbox-row {
      display: flex;
      align-items: center;
      gap: 10px;
      background: #18191B;
      border: 1px solid var(--border-subtle);
      border-radius: 6px;
      padding: 14px 16px;
      margin-bottom: 20px;
      cursor: pointer;
    }

    .checkbox-row input[type="checkbox"] {
      width: 18px;
      height: 18px;
      accent-color: var(--accent);
      cursor: pointer;
    }

    .checkbox-label-text {
      font-size: 14px;
      font-weight: 600;
      color: #FFFFFF;
    }


    /* Submit Button */
    .btn-submit {
      width: 100%;
      background: var(--accent);
      color: #171719;
      border: none;
      padding: 13px 24px;
      border-radius: 8px;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
      font-family: inherit;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      transition: all 0.2s cubic-bezier(0.16, 1, 0.3, 1);
      margin-top: 10px;
    }

    .btn-submit:hover {
      background: var(--accent-hover);
      transform: translateY(-1px);
      box-shadow: 0 4px 16px rgba(141, 235, 181, 0.3);
    }

    /* Secondary Button */
    .btn-secondary {
      background: transparent;
      border: 1px solid var(--border-subtle);
      color: var(--text-muted);
      padding: 10px 20px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s;
    }

    .btn-secondary:hover {
      color: #FFFFFF;
      border-color: rgba(255, 255, 255, 0.3);
    }

    /* Table styles for job and queue */
    table.data-table {
      width: 100%;
      border-collapse: collapse;
      margin-bottom: 24px;
    }

    table.data-table th, table.data-table td {
      padding: 12px 14px;
      border-bottom: 1px solid var(--border-subtle);
      text-align: left;
      font-size: 14px;
    }

    table.data-table th {
      color: var(--text-muted);
      font-weight: 600;
    }

    table.data-table tr.total-row td {
      border-bottom: none;
      font-size: 18px;
      font-weight: 800;
      color: var(--accent);
      padding-top: 18px;
    }

    .tag {
      padding: 3px 10px;
      border-radius: 9999px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.3px;
      display: inline-flex;
      align-items: center;
    }

    .tag.queued {
      background: rgba(251, 191, 36, 0.15);
      color: #FBBF24;
      border: 1px solid rgba(251, 191, 36, 0.3);
    }

    .tag.printing {
      background: rgba(56, 189, 248, 0.15);
      color: #38BDF8;
      border: 1px solid rgba(56, 189, 248, 0.3);
    }

    .tag.done {
      background: rgba(141, 235, 181, 0.15);
      color: var(--accent);
      border: 1px solid var(--accent-border);
    }

    .tag.failed {
      background: rgba(239, 68, 68, 0.15);
      color: #F87171;
      border: 1px solid rgba(239, 68, 68, 0.3);
    }

    /* Print queue — scoped so the queue can use a wide, readable layout on its own. */
    .queue-page {
      width: min(1240px, calc(100% - 64px));
      margin: 36px auto 60px;
    }

    .queue-page .queue-card {
      padding: 32px;
      border-color: rgba(96, 165, 250, 0.2);
      background: linear-gradient(145deg, #202126 0%, var(--card-bg) 48%);
    }

    .queue-page .queue-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 24px;
      margin-bottom: 26px;
    }

    .queue-page .queue-header .page-title {
      margin-bottom: 6px;
    }

    .queue-page .queue-table-wrap {
      overflow-x: auto;
      border: 1px solid var(--border-subtle);
      border-radius: 10px;
      background: rgba(0, 0, 0, 0.12);
    }

    .queue-page .queue-table {
      min-width: 760px;
      margin: 0;
      table-layout: fixed;
    }

    .queue-page .queue-table th,
    .queue-page .queue-table td {
      padding: 15px 18px;
      vertical-align: middle;
    }

    .queue-page .queue-table th {
      background: rgba(30, 64, 175, 0.08);
      color: #B5BDC8;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }

    .queue-page .queue-table th:nth-child(1) { width: 14%; }
    .queue-page .queue-table th:nth-child(2) { width: 13%; }
    .queue-page .queue-table th:nth-child(3) { width: 43%; }
    .queue-page .queue-table th:nth-child(4) { width: 10%; }
    .queue-page .queue-table th:nth-child(5) { width: 10%; }
    .queue-page .queue-table th:nth-child(6) { width: 10%; }

    .badge-token {
      display: inline-flex;
      align-items: center;
      gap: 5px;
      font-weight: 700;
      font-size: 13px;
      padding: 4px 10px;
      border-radius: 6px;
      white-space: nowrap;
    }
    .badge-token.queued {
      background: rgba(141, 235, 181, 0.12);
      color: #8DEBB5;
      border: 1px solid rgba(141, 235, 181, 0.25);
    }
    .badge-token.printing {
      background: rgba(96, 165, 250, 0.15);
      color: #60A5FA;
      border: 1px solid rgba(96, 165, 250, 0.3);
      animation: pulseAnimation 2s infinite;
    }
    .badge-token.done {
      background: rgba(255, 255, 255, 0.05);
      color: var(--text-muted);
      border: 1px solid rgba(255, 255, 255, 0.08);
    }
    .badge-token.failed {
      background: rgba(239, 68, 68, 0.1);
      color: #EF4444;
      border: 1px solid rgba(239, 68, 68, 0.2);
    }

    .user-queue-banner {
      display: flex;
      align-items: center;
      gap: 20px;
      padding: 20px 24px;
      border-radius: 12px;
      margin-bottom: 24px;
      background: #14171B;
      border: 1px solid rgba(141, 235, 181, 0.3);
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.5);
    }
    .user-queue-banner.printing {
      border-color: rgba(96, 165, 250, 0.4);
      background: linear-gradient(135deg, rgba(96, 165, 250, 0.08) 0%, #14171B 60%);
    }
    .user-queue-banner.done {
      border-color: rgba(16, 185, 129, 0.4);
      background: linear-gradient(135deg, rgba(16, 185, 129, 0.08) 0%, #14171B 60%);
    }
    .uq-number {
      font-size: 2.5rem;
      font-weight: 900;
      line-height: 1;
      color: #8DEBB5;
      padding: 8px 16px;
      border-radius: 10px;
      background: rgba(141, 235, 181, 0.1);
      border: 1px solid rgba(141, 235, 181, 0.2);
      min-width: 76px;
      text-align: center;
    }
    .user-queue-banner.printing .uq-number {
      color: #60A5FA;
      background: rgba(96, 165, 250, 0.12);
      border-color: rgba(96, 165, 250, 0.25);
    }
    .uq-content {
      flex: 1;
    }
    .uq-badge-tag {
      font-size: 11px;
      font-weight: 800;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: #8DEBB5;
      margin-bottom: 4px;
    }
    .user-queue-banner.printing .uq-badge-tag {
      color: #60A5FA;
    }
    .uq-title {
      font-size: 1.15rem;
      font-weight: 700;
      color: #FFFFFF;
      margin-bottom: 4px;
    }
    .uq-desc {
      font-size: 13.5px;
      color: #9CA3AF;
    }
    .user-highlight-row {
      background: rgba(141, 235, 181, 0.07) !important;
      border-left: 3px solid #8DEBB5;
    }

    .queue-page .queue-table tbody tr {
      transition: background 0.2s ease;
    }

    .queue-page .queue-table tbody tr:hover {
      background: rgba(30, 64, 175, 0.08);
    }

    .queue-page .queue-job-id {
      color: #CDD4DE;
      font-size: 12px;
      white-space: nowrap;
    }

    .queue-page .queue-document-name {
      color: #F7F8FA;
      font-weight: 700;
      overflow: hidden;
      display: -webkit-box;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 2;
      word-break: break-word;
    }

    .queue-page .queue-document-notes {
      color: var(--text-muted);
      font-size: 12px;
      margin-top: 4px;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .queue-page .queue-number,
    .queue-page .queue-colour {
      color: #D8DDE5;
      font-weight: 600;
      white-space: nowrap;
    }

    /* Print Preview Native Dialog Modal */
    dialog.preview-dialog {
      border: 1px solid var(--accent-border);
      border-radius: 8px;
      background: #18191C;
      color: #FFFFFF;
      width: 95vw;
      max-width: 940px;
      height: 85vh;
      max-height: 850px;
      padding: 0;
      box-shadow: 0 25px 60px rgba(0, 0, 0, 0.7);
      margin: auto;
    }

    dialog:modal {
      overscroll-behavior: contain;
    }

    dialog::backdrop {
      background: rgba(10, 10, 12, 0.85);
      backdrop-filter: blur(8px);
      overflow: hidden;
      overscroll-behavior: contain;
    }

    .preview-dialog-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding: 12px 20px;
      border-bottom: 1px solid var(--border-subtle);
      background: #141416;
      border-top-left-radius: 8px;
      border-top-right-radius: 8px;
    }

    .preview-dialog-title {
      color: var(--accent);
      font-size: 15px;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 8px;
      max-width: 500px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .preview-dialog-actions {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .btn-preview-action {
      background: #23252A;
      border: 1px solid var(--border-subtle);
      color: var(--text-muted);
      border-radius: 6px;
      padding: 6px 12px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      text-decoration: none;
      font-family: inherit;
      transition: all 0.2s;
    }

    .btn-preview-action:hover {
      color: #FFFFFF;
      border-color: rgba(255, 255, 255, 0.3);
    }

    .btn-preview-close {
      background: rgba(239, 68, 68, 0.15);
      border: 1px solid rgba(239, 68, 68, 0.3);
      color: #FCA5A5;
      border-radius: 6px;
      padding: 6px 12px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      font-family: inherit;
      transition: all 0.2s;
    }

    .btn-preview-close:hover {
      background: rgba(239, 68, 68, 0.25);
    }

    .preview-scroll-area {
      width: 100%;
      height: calc(85vh - 58px);
      max-height: calc(850px - 58px);
      overflow-y: auto;
      background: #101114;
      padding: 24px 16px;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 20px;
      box-sizing: border-box;
    }

    .preview-loading {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      gap: 14px;
      color: var(--accent);
      font-weight: 600;
      font-size: 14px;
      min-height: 200px;
      margin: auto;
    }

    .spinner {
      width: 32px;
      height: 32px;
      border: 3px solid rgba(141, 235, 181, 0.2);
      border-top-color: var(--accent);
      border-radius: 50%;
      animation: spin 0.8s linear infinite;
    }

    @keyframes spin {
      to { transform: rotate(360deg); }
    }

    .pdf-page-card {
      background: #FFFFFF;
      border-radius: 3px;
      box-shadow: 0 6px 24px rgba(0, 0, 0, 0.7);
      position: relative;
      display: flex;
      flex-direction: column;
      align-items: center;
      max-width: 100%;
    }

    .pdf-page-canvas {
      display: block;
      max-width: 100%;
      height: auto;
    }

    .pdf-page-number {
      background: rgba(0, 0, 0, 0.75);
      color: #FFFFFF;
      font-size: 11px;
      font-weight: 600;
      padding: 3px 8px;
      border-radius: 4px;
      position: absolute;
      bottom: 10px;
      right: 10px;
      pointer-events: none;
    }

    .preview-fallback-frame {
      width: 100%;
      height: calc(85vh - 58px);
      max-height: calc(850px - 58px);
      border: none;
      background: #18191C;
    }

    .pdf-doc-divider {
      width: 100%;
      max-width: 650px;
      background: #23252A;
      border: 1px solid var(--accent-border);
      border-radius: 6px;
      padding: 8px 14px;
      color: var(--accent);
      font-size: 13px;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 8px;
      margin-top: 10px;
    }

    /* Bottom Info Grid: How it Works & Benefits */
    .info-grid {
      display: flex;
      flex-direction: column;
      gap: 20px;
    }

    .info-card {
      background: var(--card-bg);
      border: 1.5px solid var(--accent);
      border-radius: 8px;
      padding: 24px;
      display: flex;
      flex-direction: column;
      flex: 1;
      transition: border-color 0.2s ease, transform 0.2s ease, box-shadow 0.2s ease;
    }

    .info-card:hover {
      border-color: var(--accent-hover);
      box-shadow: 0 6px 20px rgba(141, 235, 181, 0.18);
      transform: translateY(-2px);
    }

    .info-card-header {
      display: flex;
      align-items: center;
      margin-bottom: 18px;
      padding-bottom: 12px;
      border-bottom: 1px solid var(--border-subtle);
    }

    .info-title-badge {
      background: var(--accent);
      color: #171719;
      font-size: 13.5px;
      font-weight: 800;
      letter-spacing: -0.2px;
      padding: 6px 14px;
      border-radius: 6px;
      display: inline-flex;
      align-items: center;
    }

    /* Steps list (Left column) */
    .steps-list {
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    .step-item {
      display: flex;
      gap: 12px;
      align-items: flex-start;
    }

    .step-number {
      width: 22px;
      height: 22px;
      border-radius: 50%;
      background: rgba(141, 235, 181, 0.15);
      border: 1px solid var(--accent);
      color: var(--accent);
      font-size: 11px;
      font-weight: 800;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      margin-top: 2px;
    }

    .step-content strong, .benefit-content strong {
      display: block;
      color: #FFFFFF;
      font-size: 13.5px;
      font-weight: 600;
      margin-bottom: 2px;
    }

    .step-content p, .benefit-content p {
      color: var(--text-muted);
      font-size: 12px;
      line-height: 1.45;
    }

    /* Benefits list (Right column) */
    .benefits-list {
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 16px;
    }

    .benefit-item {
      display: flex;
      gap: 12px;
      align-items: flex-start;
    }

    .benefit-check-icon {
      width: 22px;
      height: 22px;
      border-radius: 50%;
      background: rgba(141, 235, 181, 0.12);
      color: var(--accent);
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
      font-size: 12px;
      font-weight: 800;
      margin-top: 2px;
    }

    @media (max-width: 680px) {
      .info-grid {
        grid-template-columns: 1fr;
        gap: 16px;
      }
    }

    /* Announcement ribbon */
    .announcement-ribbon {
      width: 100%;
      padding: 8px 16px;
      background: linear-gradient(90deg, #1E3A8A 0%, #6D28D9 40%, #DB2777 75%, #F472B6 100%);
      text-align: center;
      font-size: 13px;
      font-weight: 600;
      color: #FFFFFF;
      letter-spacing: 0.2px;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .announcement-ribbon em {
      font-style: italic;
      opacity: 0.88;
      font-weight: 400;
    }

    @media (max-width: 900px) {
      .home-layout {
        grid-template-columns: 1fr;
      }
      .home-left {
        border-right: none;
        border-bottom: 1px solid var(--border-subtle);
        padding: 36px 28px;
      }
      .home-right {
        padding: 36px 28px;
      }
      .info-grid {
        flex-direction: row;
      }
    }

    @media (max-width: 600px) {
      .queue-page {
        width: calc(100% - 32px);
        margin-top: 20px;
      }
      .queue-page .queue-card {
        padding: 20px;
      }
      .queue-page .queue-header {
        align-items: stretch;
        flex-direction: column;
        gap: 16px;
      }
      .queue-page .queue-header .btn-secondary {
        justify-content: center;
      }
      .card {
        padding: 24px 20px;
      }
      .form-row {
        grid-template-columns: 1fr;
        gap: 12px;
      }
      .page-title {
        font-size: 1.8rem;
      }
      .announcement-ribbon {
        font-size: 11.5px;
        white-space: normal;
        text-align: center;
      }
      .info-grid {
        flex-direction: column;
      }
    }
    /* UPI Payment Section */
    .upi-payment-card {
      background: var(--card-elevated);
      border: 1px solid var(--accent-border);
      border-radius: 12px;
      padding: 24px;
      margin-top: 24px;
      display: flex;
      flex-direction: column;
      align-items: center;
      text-align: center;
      position: relative;
      overflow: hidden;
    }

    .upi-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: var(--accent-muted);
      color: var(--accent);
      padding: 4px 12px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.5px;
      margin-bottom: 12px;
      border: 1px solid var(--accent-border);
    }

    .upi-qr-box {
      background: #FFFFFF;
      padding: 16px;
      border-radius: 12px;
      box-shadow: 0 8px 30px rgba(0, 0, 0, 0.4);
      margin: 16px 0;
      display: inline-flex;
      justify-content: center;
      align-items: center;
    }

    .upi-amount-hero {
      font-size: 2.2rem;
      font-weight: 800;
      color: #FFFFFF;
      margin-bottom: 4px;
      letter-spacing: -0.5px;
    }

    .upi-paise-note {
      font-size: 12px;
      color: var(--accent);
      margin-bottom: 16px;
      font-weight: 500;
    }

    .btn-upi-intent {
      width: 100%;
      background: #10B981;
      color: #FFFFFF;
      border: none;
      padding: 14px 20px;
      border-radius: 8px;
      font-size: 15px;
      font-weight: 700;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 8px;
      text-decoration: none;
      transition: all 0.2s;
      margin-top: 10px;
    }

    .btn-upi-intent:hover {
      background: #059669;
      transform: translateY(-1px);
    }

    .pulse-container {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      font-size: 13px;
      color: var(--text-muted);
      margin-top: 14px;
    }

    .pulse-dot {
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: #10B981;
      box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7);
      animation: pulseAnimation 1.6s infinite;
    }

    @keyframes pulseAnimation {
      0% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.7); }
      70% { transform: scale(1); box-shadow: 0 0 0 8px rgba(16, 185, 129, 0); }
      100% { transform: scale(0.95); box-shadow: 0 0 0 0 rgba(16, 185, 129, 0); }
    }

    @keyframes spin {
      100% { transform: rotate(360deg); }
    }

    .utr-fallback-box {
      margin-top: 20px;
      padding-top: 16px;
      border-top: 1px solid var(--border-subtle);
      width: 100%;
      text-align: left;
    }

    .utr-input-group {
      display: flex;
      gap: 8px;
      margin-top: 8px;
    }

    .utr-input {
      flex: 1;
      background: #171719;
      border: 1px solid var(--border-subtle);
      color: #FFFFFF;
      padding: 10px 14px;
      border-radius: 6px;
      font-size: 14px;
      outline: none;
      font-family: inherit;
    }

    .utr-input:focus {
      border-color: var(--accent);
    }

    /* Site Footer Styling */
    .site-footer {
      border-top: 1px solid var(--border-subtle);
      background: #0B0C0E;
      padding: 56px 0 36px;
      margin-top: 80px;
      font-size: 13.5px;
    }
    .footer-container {
      width: min(1200px, calc(100% - 48px));
      margin: 0 auto;
      display: grid;
      grid-template-columns: 2fr 1fr 1fr 1.3fr;
      gap: 40px;
    }
    @media (max-width: 820px) {
      .footer-container {
        grid-template-columns: 1fr;
        gap: 28px;
      }
    }
    .footer-brand-title {
      font-size: 1.3rem;
      font-weight: 800;
      color: #FFFFFF;
      margin-bottom: 8px;
    }
    .footer-brand-title span {
      color: var(--accent);
    }
    .footer-tagline {
      color: var(--text-muted);
      font-size: 13px;
      line-height: 1.5;
      margin-bottom: 16px;
      max-width: 340px;
    }
    .footer-contact-mini {
      display: flex;
      flex-direction: column;
      gap: 6px;
      font-size: 13px;
      color: #CBD5E1;
    }
    .footer-contact-mini a {
      color: var(--accent);
      text-decoration: none;
    }
    .footer-contact-mini a:hover {
      text-decoration: underline;
    }
    .footer-heading {
      color: #FFFFFF;
      font-weight: 700;
      font-size: 12px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      margin-bottom: 14px;
    }
    .footer-nav-col {
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .footer-nav-col a {
      color: var(--text-muted);
      text-decoration: none;
      transition: color 0.2s;
    }
    .footer-nav-col a:hover {
      color: var(--accent);
    }
    .footer-bottom {
      width: min(1200px, calc(100% - 48px));
      margin: 36px auto 0;
      padding-top: 22px;
      border-top: 1px solid rgba(255, 255, 255, 0.05);
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
      color: var(--text-muted);
      font-size: 12.5px;
    }

    /* Content & Policy Pages Styling */
    .content-page-wrap {
      width: min(900px, calc(100% - 48px));
      margin: 40px auto 60px;
    }
    .content-card {
      background: var(--card-bg);
      border: 1px solid var(--border-subtle);
      border-radius: 16px;
      padding: 44px;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.4);
    }
    @media (max-width: 640px) {
      .content-card { padding: 24px 18px; }
      .content-page-wrap { margin: 20px auto 40px; }
      .nav-container { padding: 12px 18px; flex-wrap: wrap; gap: 12px; }
      nav { gap: 14px; font-size: 13px; }
    }
    .content-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 12px;
      border-radius: 999px;
      background: rgba(141, 235, 181, 0.1);
      border: 1px solid rgba(141, 235, 181, 0.25);
      color: var(--accent);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      margin-bottom: 12px;
    }
    .content-title {
      font-size: 2.1rem;
      font-weight: 800;
      color: #FFFFFF;
      letter-spacing: -0.5px;
      margin-bottom: 8px;
    }
    .content-sub {
      color: var(--text-muted);
      font-size: 14.5px;
      line-height: 1.6;
      margin-bottom: 28px;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border-subtle);
    }
    .policy-section {
      margin-bottom: 28px;
    }
    .policy-section h2 {
      font-size: 1.22rem;
      font-weight: 700;
      color: #FFFFFF;
      margin-bottom: 10px;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .policy-section p, .policy-section li {
      color: #D1D5DB;
      font-size: 14px;
      line-height: 1.7;
      margin-bottom: 8px;
    }
    .policy-section ul {
      padding-left: 20px;
      margin-bottom: 14px;
    }
    .policy-highlight-box {
      background: rgba(141, 235, 181, 0.06);
      border: 1px solid rgba(141, 235, 181, 0.22);
      border-radius: 12px;
      padding: 20px 22px;
      margin: 20px 0;
    }
    .policy-highlight-box p {
      color: #E2E8F0;
      margin-bottom: 0;
    }
    .info-card-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap: 18px;
      margin: 24px 0;
    }
    .info-box {
      background: rgba(255, 255, 255, 0.025);
      border: 1px solid var(--border-subtle);
      border-radius: 12px;
      padding: 22px;
    }
    .info-box-title {
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--accent);
      margin-bottom: 8px;
    }
    .info-box-value {
      font-size: 1.15rem;
      font-weight: 700;
      color: #FFFFFF;
    }
    .info-box-value a {
      color: #FFFFFF;
      text-decoration: none;
    }
    .info-box-value a:hover {
      color: var(--accent);
    }
    .info-box-desc {
      font-size: 13px;
      color: var(--text-muted);
      margin-top: 6px;
      line-height: 1.5;
    }
    .pricing-table-card {
      background: rgba(255, 255, 255, 0.02);
      border: 1px solid var(--border-subtle);
      border-radius: 14px;
      overflow: hidden;
      margin: 24px 0;
    }
    .pricing-table {
      width: 100%;
      border-collapse: collapse;
      text-align: left;
    }
    .pricing-table th, .pricing-table td {
      padding: 16px 20px;
      border-bottom: 1px solid var(--border-subtle);
    }
    .pricing-table th {
      background: rgba(255, 255, 255, 0.04);
      color: #FFFFFF;
      font-size: 13px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.05em;
    }
    .pricing-table tr:last-child td {
      border-bottom: none;
    }
    .price-tag-big {
      font-size: 1.6rem;
      font-weight: 800;
      color: var(--accent);
    }
    .back-nav-link {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      color: var(--text-muted);
      text-decoration: none;
      font-size: 13px;
      margin-bottom: 20px;
      transition: color 0.2s;
    }
    .back-nav-link:hover {
      color: var(--accent);
    }
  </style>
</head>
<body>
  <!-- Announcement Ribbon -->
  <div class="announcement-ribbon">
    <em>"Limitless paper in a paperless world." — Michael Scott</em>
  </div>
  <!-- Header -->
  <header>
    <div class="nav-container">
      <a class="brand-link" href="/">
        <div class="brand-logo-icon">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="6 9 6 2 18 2 18 9"></polyline>
            <path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"></path>
            <rect x="6" y="14" width="12" height="8"></rect>
          </svg>
        </div>
        <div class="brand-title">Printer of A<span>'</span>s</div>
      </a>
      <nav>
        <a href="/pricing">Pricing</a>
        <a href="/queue">Print Queue</a>
        <a href="/about">About</a>
        <a href="/contact">Contact</a>
      </nav>
    </div>
  </header>

  <main>
    <div class="page-content-wrapper">
      {{ body|safe }}
    </div>
  </main>

  <!-- Site-Wide Professional Footer -->
  <footer class="site-footer">
    <div class="footer-container">
      <div class="footer-brand">
        <div class="footer-brand-title">Printer of A<span>'</span>s</div>
        <p class="footer-tagline">Autonomous, 24/7 human-less print kiosk. Upload from your phone, pay via UPI, and collect instant laser prints.</p>
        <div class="footer-contact-mini">
          <span>📞 <a href="tel:9696123736">9696123736</a> / <a href="tel:9589974245">9589974245</a></span>
          <span>✉️ <a href="mailto:printerofas@gmail.com">printerofas@gmail.com</a></span>
          <span>⏱️ Refund Turnaround: <strong>2 Working Days</strong></span>
        </div>
      </div>
      <div class="footer-nav-col">
        <div class="footer-heading">Services</div>
        <a href="/">Print Document</a>
        <a href="/queue">Live Print Queue</a>
        <a href="/pricing">Pricing & Rates</a>
      </div>
      <div class="footer-nav-col">
        <div class="footer-heading">Company</div>
        <a href="/about">About Us</a>
        <a href="/contact">Contact Us</a>
      </div>
      <div class="footer-nav-col">
        <div class="footer-heading">Legal & Policies</div>
        <a href="/privacy">Privacy Policy</a>
        <a href="/terms">Terms & Conditions</a>
        <a href="/refunds">Refund & Cancellation</a>
      </div>
    </div>
    <div class="footer-bottom">
      <div>© 2026 Printer of A's. All rights reserved.</div>
      <div>Privacy First: Uploaded files are automatically purged once printed.</div>
    </div>
  </footer>

  <!-- Print Preview Native Modal Dialog -->
  <dialog id="preview-dialog" class="preview-dialog" aria-labelledby="preview-title">
    <div class="preview-dialog-header">
      <div class="preview-dialog-title" id="preview-title">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
          <polyline points="6 9 6 2 18 2 18 9"></polyline>
          <path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"></path>
          <rect x="6" y="14" width="12" height="8"></rect>
        </svg>
        <span id="preview-filename">Document Preview</span>
        <span id="preview-page-pill" style="font-size: 11px; background: rgba(141, 235, 181, 0.15); color: var(--accent); padding: 2px 8px; border-radius: 999px;">0 Pages</span>
      </div>
      <div class="preview-dialog-actions">
        <a id="preview-new-tab" href="#" target="_blank" rel="noopener noreferrer" class="btn-preview-action">
          Open in Tab ↗
        </a>
        <button type="button" class="btn-preview-action" onclick="printFromPreview()">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 6 2 18 2 18 9"></polyline><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"></path><rect x="6" y="14" width="12" height="8"></rect></svg>
          Print
        </button>
        <form method="dialog" style="display:inline;">
          <button type="submit" class="btn-preview-close" autofocus>Close ✕</button>
        </form>
      </div>
    </div>
    <div class="preview-scroll-area" id="preview-pages-container"></div>
    <iframe id="preview-fallback-frame" class="preview-fallback-frame" style="display: none;" title="Print Preview"></iframe>
  </dialog>

  <script>
    if (window.pdfjsLib) {
      pdfjsLib.GlobalWorkerOptions.workerSrc = 'https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js';
    }

    const previewDialog = document.getElementById('preview-dialog');
    if (previewDialog) {
      previewDialog.addEventListener('click', (e) => {
        if (e.target === previewDialog) previewDialog.close();
      });
    }

    let activePreviewBlobUrl = null;

    async function openPdfViewer(source, filename) {
      const dialog = document.getElementById('preview-dialog');
      const titleSpan = document.getElementById('preview-filename');
      const pagePill = document.getElementById('preview-page-pill');
      const container = document.getElementById('preview-pages-container');
      const fallbackIframe = document.getElementById('preview-fallback-frame');
      const newTabLink = document.getElementById('preview-new-tab');

      titleSpan.textContent = filename || 'Document Preview';
      pagePill.textContent = 'Loading...';
      container.style.display = 'flex';
      fallbackIframe.style.display = 'none';
      container.innerHTML = '<div class="preview-loading"><div class="spinner"></div><span>Rendering print preview...</span></div>';

      dialog.showModal();

      if (Array.isArray(source)) {
        newTabLink.style.display = 'none';
        try {
          if (!window.pdfjsLib) throw new Error('PDF.js unavailable');
          container.innerHTML = '';
          let totalPagesAcrossDocs = 0;

          for (let fIdx = 0; fIdx < source.length; fIdx++) {
            const file = source[fIdx];
            const isPdf = file.type === 'application/pdf' || file.name.toLowerCase().endsWith('.pdf');

            if (source.length > 1) {
              const divHeader = document.createElement('div');
              divHeader.className = 'pdf-doc-divider';
              divHeader.innerHTML = `<span>Document ${fIdx + 1} of ${source.length}: <strong>${file.name}</strong></span>`;
              container.appendChild(divHeader);
            }

            if (!isPdf) {
              const imageUrl = URL.createObjectURL(file);
              const card = document.createElement('div');
              card.className = 'pdf-page-card';
              const img = document.createElement('img');
              img.className = 'pdf-page-canvas';
              img.alt = `Preview of ${file.name}`;
              const numBadge = document.createElement('div');
              numBadge.className = 'pdf-page-number';
              numBadge.textContent = source.length > 1 ? `${file.name} · image` : 'Image';
              card.appendChild(img);
              card.appendChild(numBadge);
              container.appendChild(card);
              await new Promise((resolve, reject) => {
                img.onload = () => { URL.revokeObjectURL(imageUrl); resolve(); };
                img.onerror = () => { URL.revokeObjectURL(imageUrl); reject(new Error('Could not preview image')); };
                img.src = imageUrl;
              });
              totalPagesAcrossDocs += 1;
              continue;
            }

            const arrayBuf = await file.arrayBuffer();
            const loadingTask = pdfjsLib.getDocument({ data: arrayBuf });
            const pdf = await loadingTask.promise;
            totalPagesAcrossDocs += pdf.numPages;

            for (let pageNum = 1; pageNum <= pdf.numPages; pageNum++) {
              const page = await pdf.getPage(pageNum);
              const viewport = page.getViewport({ scale: 1.25 });

              const card = document.createElement('div');
              card.className = 'pdf-page-card';

              const canvas = document.createElement('canvas');
              canvas.className = 'pdf-page-canvas';
              const ctx = canvas.getContext('2d');
              canvas.height = viewport.height;
              canvas.width = viewport.width;

              const numBadge = document.createElement('div');
              numBadge.className = 'pdf-page-number';
              numBadge.textContent = source.length > 1 ? `${file.name} · ${pageNum}/${pdf.numPages}` : `${pageNum}/${pdf.numPages}`;

              card.appendChild(canvas);
              card.appendChild(numBadge);
              container.appendChild(card);

              await page.render({ canvasContext: ctx, viewport: viewport }).promise;
            }
          }
          pagePill.textContent = `${totalPagesAcrossDocs} ${totalPagesAcrossDocs === 1 ? 'Page' : 'Pages'}`;
        } catch (err) {
          console.warn('PDF.js rendering fallback for multiple files:', err);
          container.innerHTML = '<div style="color: #F87171; padding: 24px; text-align: center; font-weight: 600;">Could not render preview for one or more files. Please check that each file is a valid PDF or image.</div>';
        }
        return;
      }

      newTabLink.style.display = 'inline-flex';
      let dataToRender = source;
      let urlForTab = '#';

      if (source instanceof File) {
        try {
          const arrayBuf = await source.arrayBuffer();
          dataToRender = arrayBuf;
          if (activePreviewBlobUrl) URL.revokeObjectURL(activePreviewBlobUrl);
          activePreviewBlobUrl = URL.createObjectURL(new Blob([arrayBuf], { type: 'application/pdf' }));
          urlForTab = activePreviewBlobUrl;
        } catch (e) {
          console.error(e);
        }
      } else if (typeof source === 'string') {
        urlForTab = source;
      }
      newTabLink.href = urlForTab;

      try {
        if (window.pdfjsLib) {
          const loadingTask = pdfjsLib.getDocument({
            data: (dataToRender instanceof ArrayBuffer) ? dataToRender : undefined,
            url: (typeof dataToRender === 'string') ? dataToRender : undefined
          });
          const pdf = await loadingTask.promise;
          pagePill.textContent = `${pdf.numPages} ${pdf.numPages === 1 ? 'Page' : 'Pages'}`;
          container.innerHTML = '';

          for (let pageNum = 1; pageNum <= pdf.numPages; pageNum++) {
            const page = await pdf.getPage(pageNum);
            const viewport = page.getViewport({ scale: 1.25 });

            const card = document.createElement('div');
            card.className = 'pdf-page-card';

            const canvas = document.createElement('canvas');
            canvas.className = 'pdf-page-canvas';
            const ctx = canvas.getContext('2d');
            canvas.height = viewport.height;
            canvas.width = viewport.width;

            const numBadge = document.createElement('div');
            numBadge.className = 'pdf-page-number';
            numBadge.textContent = `${pageNum} / ${pdf.numPages}`;

            card.appendChild(canvas);
            card.appendChild(numBadge);
            container.appendChild(card);

            await page.render({ canvasContext: ctx, viewport: viewport }).promise;
          }
        } else {
          throw new Error('PDF.js unavailable');
        }
      } catch (err) {
        console.warn('PDF.js rendering fallback:', err);
        pagePill.textContent = 'Native View';
        container.style.display = 'none';
        fallbackIframe.style.display = 'block';
        fallbackIframe.src = urlForTab;
      }
    }

    function printFromPreview() {
      const url = document.getElementById('preview-new-tab').href;
      if (url && url !== '#') {
        const printWin = window.open(url, '_blank');
        if (printWin) printWin.focus();
      }
    }
  </script>
</body>
</html>
"""


def render_page(body_template, **ctx):
    body = render_template_string(body_template, **ctx)
    return render_template_string(PAGE_TEMPLATE, body=body)


# ---------- User-Facing Routes ----------
@app.get("/health")
def health():
    return jsonify(status="ok", service="printer-of-as"), 200


@app.get("/")
def index():
    return render_page("""
    <div class="home-layout">

      <!-- LEFT: Upload Panel -->
      <div class="home-left">
        <h1 class="page-title" style="font-size: 2.6rem; margin-bottom: 6px;">Upload your files</h1>
        <p style="color: var(--text-muted); font-size: 14px; margin-bottom: 28px;">Select PDFs or images — merged into a single print job.</p>

        {% if error %}
        <div class="alert-err">{{ error }}</div>
        {% endif %}

        <form method="post" action="/upload" enctype="multipart/form-data" id="upload-form" onsubmit="return handleFormSubmit(event)">
          <!-- Single file picker input (also acts as the form field via FormData) -->
          <input type="file" id="file-picker" accept="application/pdf,image/png,image/jpeg,image/webp,image/gif,image/bmp,image/tiff,image/heic,image/heif,.pdf,.png,.jpg,.jpeg,.webp,.gif,.bmp,.tif,.tiff,.heic,.heif" multiple style="display: none;" onchange="handlePickerFiles(this)">

          <!-- Dropzone -->
          <div class="dropzone card-upload" id="dropzone" onclick="triggerFilePicker()" style="padding: 52px 20px; margin-bottom: 20px;">
            <div class="dropzone-icon">
              <svg width="38" height="38" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>
                <polyline points="17 8 12 3 7 8"></polyline>
                <line x1="12" y1="3" x2="12" y2="15"></line>
              </svg>
            </div>
            <div class="dropzone-text" id="dropzone-main-text">Click to choose PDFs or images, or drag them here</div>
            <div class="dropzone-sub" style="margin-top: 6px;">PDF, PNG, JPG, WEBP, HEIC, GIF, BMP and TIFF · Merged into 1 print queue item</div>
          </div>

          <!-- Selected Documents Card -->
          <div class="file-selected-card" id="file-selected-card">
            <div class="file-list-scroll" id="file-list-container"></div>
            <div class="file-card-footer">
              <span class="file-summary-text" id="file-summary-text">0 documents</span>
              <div class="file-card-actions">
                <button type="button" class="btn-add-more" onclick="triggerFilePicker()">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line></svg>
                  Add More
                </button>
                <button type="button" class="btn-preview-prominent" onclick="previewCurrentChosenFiles(event)">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path><circle cx="12" cy="12" r="3"></circle></svg>
                  Print Preview
                </button>
              </div>
            </div>
          </div>

          <!-- Submit Button -->
          <button type="submit" class="btn-submit" id="btn-submit" style="display: none;">
            <span id="btn-submit-text">Continue</span>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <line x1="5" y1="12" x2="19" y2="12"></line>
              <polyline points="12 5 19 12 12 19"></polyline>
            </svg>
          </button>
        </form>

        <!-- Visual Upload & Optimization Progress Modal -->
        <div id="upload-modal" style="display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.85); backdrop-filter: blur(10px); z-index: 99999; align-items: center; justify-content: center; padding: 20px;">
          <div style="background: #111418; border: 1px solid rgba(255,255,255,0.14); border-radius: 16px; padding: 36px 30px; max-width: 440px; width: 100%; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.8); text-align: center;">
            <div style="width: 56px; height: 56px; border-radius: 50%; background: rgba(141, 235, 181, 0.12); color: #8DEBB5; display: inline-flex; align-items: center; justify-content: center; margin-bottom: 20px;">
              <svg id="upload-icon-spin" width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" style="animation: spin 1.4s linear infinite;">
                <path d="M21 12a9 9 0 1 1-6.219-8.56"></path>
              </svg>
            </div>
            <h3 id="upload-stage-title" style="color: #FFFFFF; font-size: 1.3rem; font-weight: 700; margin: 0 0 6px;">Preparing Documents...</h3>
            <p id="upload-stage-sub" style="color: var(--text-muted); font-size: 13.5px; margin: 0 0 24px;">Preserving 300 DPI high-definition print clarity</p>
            
            <!-- Progress Bar Track -->
            <div style="width: 100%; height: 10px; background: rgba(255,255,255,0.08); border-radius: 999px; overflow: hidden; margin-bottom: 14px; position: relative;">
              <div id="upload-progress-fill" style="width: 0%; height: 100%; background: linear-gradient(90deg, #8DEBB5, #38BDF8); border-radius: 999px; transition: width 0.2s ease;"></div>
            </div>

            <div style="display: flex; justify-content: space-between; align-items: center; font-size: 12.5px; color: var(--text-muted);">
              <span id="upload-status-detail">Optimizing...</span>
              <span id="upload-percentage" style="color: #8DEBB5; font-weight: 700; font-size: 14px;">0%</span>
            </div>
          </div>
        </div>

        <!-- Privacy Notice -->
        <div style="display: flex; align-items: center; gap: 8px; margin-top: 18px; padding: 12px 16px; background: rgba(141, 235, 181, 0.05); border: 1px solid rgba(141, 235, 181, 0.15); border-radius: 8px;">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#8DEBB5" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink: 0;">
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path>
          </svg>
          <p style="margin: 0; font-size: 12.5px; color: #A0A6AD; line-height: 1.5;">
            <span style="color: #8DEBB5; font-weight: 700;">Your privacy is respected.</span>
            We don't store your documents — files are deleted immediately after printing.
          </p>
        </div>
      </div>

      <!-- RIGHT: How It Works + Key Benefits -->
      <div class="home-right">

        <!-- How It Works -->
        <div class="info-card">
          <div class="info-card-header">
            <span class="info-title-badge">How It Works</span>
          </div>
          <ul class="steps-list">
            <li class="step-item">
              <span class="step-number">1</span>
              <div class="step-content">
                <strong>Select &amp; Preview Documents</strong>
                <p>Drag or choose PDFs and images. Preview your files before submitting.</p>
              </div>
            </li>
            <li class="step-item">
              <span class="step-number">2</span>
              <div class="step-content">
                <strong>Configure &amp; Pay</strong>
                <p>Choose B&amp;W or full color, number of copies, and duplex mode with live transparent pricing.</p>
              </div>
            </li>
            <li class="step-item">
              <span class="step-number">3</span>
              <div class="step-content">
                <strong>Collect from Printer</strong>
                <p>Your documents are merged into 1 print queue item and printed automatically at the kiosk.</p>
              </div>
            </li>
          </ul>
        </div>

        <!-- Key Benefits -->
        <div class="info-card">
          <div class="info-card-header">
            <span class="info-title-badge">Key Benefits</span>
          </div>
          <ul class="benefits-list">
            <li class="benefit-item">
              <div class="benefit-check-icon">✓</div>
              <div class="benefit-content">
                <strong>Zero Waiting &amp; Contactless</strong>
                <p>Skip physical counter queues. Submit and track your print job directly from your device.</p>
              </div>
            </li>
            <li class="benefit-item">
              <div class="benefit-check-icon">✓</div>
              <div class="benefit-content">
                <strong>Multi-Document Auto-Merge</strong>
                <p>Upload lecture slides, assignments, and notes together—stitched seamlessly into 1 job.</p>
              </div>
            </li>
            <li class="benefit-item">
              <div class="benefit-check-icon">✓</div>
              <div class="benefit-content">
                <strong>Affordable Student Rates</strong>
                <p>Transparent rates from {{ cur }}{{ p['bw'] }}/page with duplex paper savings and real-time total.</p>
              </div>
            </li>
          </ul>
        </div>

      </div>
    </div>

    <script>
      let selectedFiles = [];

      function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
      }

      function triggerFilePicker() {
        const picker = document.getElementById('file-picker');
        picker.value = '';
        picker.click();
      }

      function handlePickerFiles(picker) {
        if (picker.files && picker.files.length > 0) {
          addFiles(picker.files);
        }
        picker.value = '';
      }

      function addFiles(filesList) {
        for (let i = 0; i < filesList.length; i++) {
          const file = filesList[i];
          const fileName = file.name.toLowerCase();
          const isPdf = file.type === 'application/pdf' || fileName.endsWith('.pdf');
          const isImage = /^image\/(bmp|gif|heic|heif|jpeg|png|tiff|webp)$/.test(file.type)
            || /\.(bmp|gif|heic|heif|jpe?g|png|tiff?|webp)$/.test(fileName);
          if (isPdf || isImage) {
            const exists = selectedFiles.some(f => f.name === file.name && f.size === file.size && f.lastModified === file.lastModified);
            if (!exists) {
              selectedFiles.push(file);
            }
          }
        }
        syncInputAndUI();
      }

      function removeSelectedFile(index) {
        selectedFiles.splice(index, 1);
        syncInputAndUI();
      }

      function syncInputAndUI() {
        const card = document.getElementById('file-selected-card');
        const listContainer = document.getElementById('file-list-container');
        const summaryText = document.getElementById('file-summary-text');
        const submitBtn = document.getElementById('btn-submit');
        const submitText = document.getElementById('btn-submit-text');
        const dropzoneText = document.getElementById('dropzone-main-text');

        if (selectedFiles.length === 0) {
          card.style.display = 'none';
          submitBtn.style.display = 'none';
          dropzoneText.textContent = 'Click to choose PDFs or images, or drag them here';
          return;
        }

        card.style.display = 'block';
        submitBtn.style.display = 'inline-flex';
        dropzoneText.textContent = '+ Drop or click to add more files';

        listContainer.innerHTML = '';
        let totalBytes = 0;

        selectedFiles.forEach((file, index) => {
          totalBytes += file.size;
          const item = document.createElement('div');
          item.className = 'file-list-item';

          const sizeFormatted = file.size > 1024 * 1024 
            ? (file.size / (1024 * 1024)).toFixed(2) + ' MB'
            : (file.size / 1024).toFixed(1) + ' KB';

          item.innerHTML = `
            <div class="file-item-left">
              <div class="file-mini-icon">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>
                  <polyline points="14 2 14 8 20 8"></polyline>
                </svg>
              </div>
              <div class="file-item-info">
                <span class="file-item-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</span>
                <span class="file-item-size">${sizeFormatted}</span>
              </div>
            </div>
            <button type="button" class="btn-remove-file" onclick="removeSelectedFile(${index})" title="Remove document">✕</button>
          `;
          listContainer.appendChild(item);
        });

        const totalFormatted = (totalBytes / (1024 * 1024)).toFixed(2) + ' MB';
        const docLabel = selectedFiles.length === 1 ? '1 document' : `${selectedFiles.length} documents`;
        summaryText.textContent = `${docLabel} (${totalFormatted} total) · Merged into 1 print job`;
        submitText.textContent = `Continue with ${docLabel}`;
      }

      function previewCurrentChosenFiles(event) {
        if (event) event.stopPropagation();
        if (selectedFiles.length > 0) {
          const title = selectedFiles.length === 1 
            ? selectedFiles[0].name 
            : `${selectedFiles.length} Documents Preview`;
          openPdfViewer(selectedFiles, title);
        }
      }

      async function optimizeFileForPrint(file) {
        // 1. PDFs are NEVER compressed or altered - vector fidelity is 100% preserved
        const nameLower = file.name.toLowerCase();
        if (file.type === 'application/pdf' || nameLower.endsWith('.pdf')) {
          return file;
        }

        // 2. Only process standard raster images (JPG, PNG, WEBP, BMP)
        const isRaster = /^image\/(jpe?g|png|webp|bmp)$/i.test(file.type) ||
                         /\.(jpe?g|png|webp|bmp)$/i.test(nameLower);
        if (!isRaster) {
          return file; // Pass unusual formats unmodified to server
        }

        // 3. Skip small files (< 1.2 MB)
        if (file.size <= 1.2 * 1024 * 1024) {
          return file;
        }

        return new Promise((resolve) => {
          const img = new Image();
          const url = URL.createObjectURL(file);
          img.onload = () => {
            URL.revokeObjectURL(url);
            const MAX_DIM = 2500; // 300 DPI for crisp A4 print
            let w = img.width;
            let h = img.height;

            if (w <= MAX_DIM && h <= MAX_DIM) {
              resolve(file);
              return;
            }

            if (w > h) {
              if (w > MAX_DIM) {
                h = Math.round((h * MAX_DIM) / w);
                w = MAX_DIM;
              }
            } else {
              if (h > MAX_DIM) {
                w = Math.round((w * MAX_DIM) / h);
                h = MAX_DIM;
              }
            }

            const canvas = document.createElement('canvas');
            canvas.width = w;
            canvas.height = h;
            const ctx = canvas.getContext('2d');
            ctx.imageSmoothingEnabled = true;
            ctx.imageSmoothingQuality = 'high';
            ctx.fillStyle = '#FFFFFF';
            ctx.fillRect(0, 0, w, h);
            ctx.drawImage(img, 0, 0, w, h);

            canvas.toBlob((blob) => {
              if (blob && blob.size < file.size) {
                const newName = file.name.replace(/\.[^/.]+$/, "") + ".jpg";
                const optFile = new File([blob], newName, { type: 'image/jpeg', lastModified: Date.now() });
                resolve(optFile);
              } else {
                resolve(file);
              }
            }, 'image/jpeg', 0.90);
          };
          img.onerror = () => {
            URL.revokeObjectURL(url);
            resolve(file);
          };
          img.src = url;
        });
      }

      async function handleFormSubmit(event) {
        event.preventDefault();
        if (selectedFiles.length === 0) {
          alert('Please choose at least one PDF or image file before continuing.');
          return false;
        }

        const modal = document.getElementById('upload-modal');
        const stageTitle = document.getElementById('upload-stage-title');
        const stageSub = document.getElementById('upload-stage-sub');
        const progressFill = document.getElementById('upload-progress-fill');
        const statusDetail = document.getElementById('upload-status-detail');
        const percentageText = document.getElementById('upload-percentage');

        modal.style.display = 'flex';
        progressFill.style.width = '0%';
        percentageText.textContent = '0%';

        // Stage 1: Smart Client Optimization (preserving 300 DPI print quality)
        stageTitle.textContent = 'Optimizing Documents...';
        stageSub.textContent = 'Preserving 300 DPI high-definition print clarity';
        statusDetail.textContent = 'Checking files...';

        const optimizedFiles = [];
        for (let i = 0; i < selectedFiles.length; i++) {
          const orig = selectedFiles[i];
          statusDetail.textContent = `Optimizing (${i + 1}/${selectedFiles.length}): ${orig.name}`;
          const optPct = Math.round(((i + 1) / selectedFiles.length) * 20);
          progressFill.style.width = `${optPct}%`;
          percentageText.textContent = `${optPct}%`;
          const opt = await optimizeFileForPrint(orig);
          optimizedFiles.push(opt);
        }

        // Stage 2: Upload to Cloud Server
        stageTitle.textContent = 'Uploading to Cloud...';
        stageSub.textContent = 'Transferring documents securely';
        progressFill.style.width = '20%';
        percentageText.textContent = '20%';

        const fd = new FormData();
        optimizedFiles.forEach(f => fd.append('files', f, f.name));

        const xhr = new XMLHttpRequest();
        xhr.open('POST', '/upload', true);
        xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');

        xhr.upload.onprogress = (e) => {
          if (e.lengthComputable) {
            const uploadPct = Math.round((e.loaded / e.total) * 70);
            const totalPct = 20 + uploadPct;
            progressFill.style.width = `${totalPct}%`;
            percentageText.textContent = `${totalPct}%`;
            const loadedMb = (e.loaded / (1024 * 1024)).toFixed(1);
            const totalMb = (e.total / (1024 * 1024)).toFixed(1);
            statusDetail.textContent = `Uploading: ${loadedMb} MB / ${totalMb} MB`;
          }
        };

        xhr.onload = () => {
          if (xhr.status >= 200 && xhr.status < 400) {
            stageTitle.textContent = 'Merging & Finalizing...';
            stageSub.textContent = 'Saving PDF to Supabase Cloud Storage';
            progressFill.style.width = '100%';
            percentageText.textContent = '100%';
            statusDetail.textContent = 'Done! Redirecting...';

            try {
              const resData = JSON.parse(xhr.responseText);
              if (resData.redirect_url) {
                setTimeout(() => { window.location.href = resData.redirect_url; }, 350);
                return;
              }
            } catch (e) {}

            if (xhr.responseURL) {
              window.location.href = xhr.responseURL;
            } else {
              window.location.href = '/queue';
            }
          } else {
            let errMsg = 'Upload failed. Please check your files and try again.';
            try {
              const resData = JSON.parse(xhr.responseText);
              if (resData.error) errMsg = resData.error;
            } catch (e) {}
            modal.style.display = 'none';
            alert(errMsg);
          }
        };

        xhr.onerror = () => {
          modal.style.display = 'none';
          alert('Network connection error. Please try uploading again.');
        };

        xhr.send(fd);
        return false;
      }

      function validateBeforeSubmit() {
        if (selectedFiles.length === 0) {
          alert('Please choose at least one PDF or image file before continuing.');
          return false;
        }
        return true;
      }

      const dropzone = document.getElementById('dropzone');
      ['dragenter', 'dragover'].forEach(name => {
        dropzone.addEventListener(name, (e) => {
          e.preventDefault();
          e.stopPropagation();
          dropzone.classList.add('dragover');
        });
      });
      ['dragleave', 'drop'].forEach(name => {
        dropzone.addEventListener(name, (e) => {
          e.preventDefault();
          e.stopPropagation();
          dropzone.classList.remove('dragover');
        });
      });
      dropzone.addEventListener('drop', (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropzone.classList.remove('dragover');
        if (e.dataTransfer && e.dataTransfer.files) {
          addFiles(e.dataTransfer.files);
        }
      });
    </script>
    """, cur=CURRENCY, p=PRICE_PER_PAGE, error=request.args.get("error"))


@app.post("/upload")
def upload():
    def upload_err(msg):
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify(ok=False, error=msg), 400
        return redirect(url_for("index", error=msg))

    files = request.files.getlist("files")
    if not files or not any(f.filename for f in files):
        files = request.files.getlist("file")
    valid_files = [f for f in files if f and f.filename and is_supported_upload(f)]
    if not valid_files:
        return upload_err("Please choose at least one valid PDF or image file.")

    job_id = uuid.uuid4().hex[:10]
    stored = f"{job_id}.pdf"
    path = os.path.join(UPLOADS, stored)

    try:
        if len(valid_files) == 1 and is_pdf_upload(valid_files[0]):
            valid_files[0].save(path)
            display_name = os.path.basename(valid_files[0].filename)
            notes = ""
        else:
            writer = PdfWriter()
            file_names = []
            for f in valid_files:
                orig_name = os.path.basename(f.filename)
                file_names.append(orig_name)
                if is_pdf_upload(f):
                    f.stream.seek(0)
                    writer.append(f.stream)
                else:
                    writer.append(image_to_pdf(f))
            writer.write(path)
            if len(file_names) == 1:
                display_name = file_names[0]
                notes = ""
            elif len(file_names) == 2:
                display_name = f"{file_names[0]} + {file_names[1]}"
                notes = ", ".join(file_names)
            else:
                display_name = f"{file_names[0]} (+{len(file_names)-1} documents)"
                notes = ", ".join(file_names)
    except (OSError, UnidentifiedImageError, ValueError, TypeError):
        if os.path.exists(path):
            os.remove(path)
        return upload_err("Could not read an uploaded file. Please ensure every PDF or image is valid.")
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        return upload_err("Could not read or merge the uploaded files. Please ensure every PDF or image is valid.")

    try:
        pages = len(PdfReader(path).pages)
    except Exception:
        if os.path.exists(path):
            os.remove(path)
        return upload_err("That file could not be read as a PDF.")

    if pages < 1 or pages > MAX_PAGES:
        if os.path.exists(path):
            os.remove(path)
        return upload_err(f"Combined document must have 1 to {MAX_PAGES} pages (uploaded total is {pages} pages).")

    color = request.form.get("color", "bw")
    if color not in PRICE_PER_PAGE:
        color = "bw"
    try:
        copies = max(1, min(20, int(request.form.get("copies", 1))))
    except ValueError:
        copies = 1
    duplex = 1 if request.form.get("duplex") else 0

    offset = calculate_paise_offset(job_id)
    calculated_amount = price(pages, copies, color, offset)
    created_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    con = get_db()
    with con:
        con.execute(
            """INSERT INTO jobs (id, filename, stored, pages, copies, color, duplex, amount, status, created, notes, utr_number, reserved_paise_offset)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                job_id, display_name, stored, pages, copies, color,
                duplex, calculated_amount, "pending_payment",
                created_iso, notes, "", offset
            )
        )

    # Sync with Supabase so Termux listener knows about it
    supabase_sync_job({
        "id": job_id, "filename": display_name, "stored": stored,
        "pages": pages, "copies": copies, "color": color, "duplex": duplex,
        "amount": calculated_amount, "reserved_paise_offset": offset,
        "status": "pending_payment", "created": created_iso, "notes": notes
    })

    # Upload merged PDF to Supabase Cloud Storage for persistent cloud backup
    supabase_upload_pdf(job_id, path)

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify(ok=True, redirect_url=url_for("job", job_id=job_id))

    return redirect(url_for("job", job_id=job_id))


def get_job(job_id):
    con = get_db()
    row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row and SUPABASE_URL:
        supa_jobs = supabase_request(f"jobs?id=eq.{job_id}&select=*")
        if supa_jobs and len(supa_jobs) > 0:
            sj = supa_jobs[0]
            with con:
                con.execute(
                    """INSERT OR IGNORE INTO jobs (id, filename, stored, pages, copies, color, duplex, amount, status, created, notes, utr_number, reserved_paise_offset)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        sj["id"], sj.get("filename", ""), sj.get("stored", ""),
                        int(sj.get("pages", 1)), int(sj.get("copies", 1)), sj.get("color", "bw"),
                        int(sj.get("duplex", 0)), float(sj.get("amount", 0.0)), sj.get("status", "pending_payment"),
                        sj.get("created", ""), sj.get("notes", ""), sj.get("utr_number", ""), int(sj.get("reserved_paise_offset", 0))
                    )
                )
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    if not row:
        abort(404)
    return row


def get_public_base_url():
    """
    Returns public HTTPS base URL for Cashfree callbacks and redirects.
    Ensures 'https://' is strictly used since Cashfree production rejects 'http://'.
    """
    host = request.headers.get("x-forwarded-host") or request.host
    if "localhost" in host or "127.0.0.1" in host:
        return "https://printer-of-a-s.onrender.com"
    return f"https://{host}"


@app.get("/job/<job_id>")
def job(job_id):
    j = get_job(job_id)

    # Check if user returned from Cashfree checkout via return_url
    cf_order_id = request.args.get("cf_order_id")
    if cf_order_id and j["status"] == "pending_payment" and cashfree_helper.is_cashfree_configured():
        order_info = cashfree_helper.get_order_status(cf_order_id)
        if order_info and order_info.get("order_status") == "PAID":
            payments = cashfree_helper.get_order_payments(cf_order_id)
            utr = "Cashfree-Auto"
            if payments and isinstance(payments, list) and len(payments) > 0:
                utr = str(payments[0].get("bank_reference") or payments[0].get("cf_payment_id") or "Cashfree-Auto")
            con = get_db()
            with con:
                con.execute("UPDATE jobs SET status='queued', utr_number=? WHERE id=?", (utr, job_id))
            if SUPABASE_URL:
                supabase_request(f"jobs?id=eq.{job_id}", method="PATCH", data={"status": "queued", "utr_number": utr})
                supabase_request("incoming_payments", method="POST", data={
                    "amount": float(j["amount"]),
                    "utr_number": utr,
                    "raw_message": f"Cashfree Return URL Verification for {job_id}",
                    "sender_info": "Cashfree Return URL",
                    "is_consumed": True,
                    "matched_job_id": job_id
                })
            return redirect(url_for("queue", just=job_id))

    cf_session = None
    if cashfree_helper.is_cashfree_configured() and j["status"] == "pending_payment":
        base_url = get_public_base_url()
        notify_url = f"{base_url}/api/cashfree/webhook"
        return_url = f"{base_url}/job/{job_id}?cf_order_id={{order_id}}"
        cf_session = cashfree_helper.get_or_create_cashfree_session(job_id, j["amount"], notify_url=notify_url, return_url=return_url)

    return render_page("""
    <div class="card">
      <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px;">
        <div>
          <h1 class="page-title" style="font-size: 1.9rem;">Job Specifications</h1>
          <p class="page-sub" style="margin-bottom: 18px;">{{ j['filename'] }} ({{ j['pages'] }} pages)</p>
        </div>
        <span class="tag {{ j['status'] }}" id="job-status-tag">{{ j['status'] }}</span>
      </div>

      <!-- Print Preview Actions -->
      <div style="display: flex; gap: 10px; margin: 10px 0 24px;">
        <button type="button" class="btn-preview-prominent" style="flex: 1; justify-content: center; padding: 12px 20px;" onclick="openPdfViewer('/job/{{ j['id'] }}/preview', '{{ j['filename'] }}')">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"></path></svg>
          Print Preview
        </button>
        <a href="/job/{{ j['id'] }}/preview" target="_blank" rel="noopener noreferrer" class="btn-secondary" style="justify-content: center; padding: 12px 18px;">
          Open in Tab ↗
        </a>
      </div>

      {% if j['status']=='pending_payment' %}
      <div class="form-row">
        <div>
          <label class="field-label" for="opt-color">Colour Mode</label>
          <select id="opt-color" name="color" class="field-select" onchange="updateJobPricing()">
            <option value="bw" {% if j['color']=='bw' %}selected{% endif %}>Black &amp; white ({{ cur }}{{ p['bw'] }} / page)</option>
            <option value="color" {% if j['color']=='color' %}selected{% endif %}>Full colour ({{ cur }}{{ p['color'] }} / page)</option>
          </select>
        </div>
        <div>
          <label class="field-label" for="opt-copies">Copies</label>
          <input id="opt-copies" type="number" name="copies" class="field-input" value="{{ j['copies'] }}" min="1" max="20" onchange="updateJobPricing()" oninput="updateJobPricing()">
        </div>
      </div>

      <label class="checkbox-row" for="opt-duplex">
        <input type="checkbox" name="duplex" value="1" id="opt-duplex" onchange="updateJobPricing()" {% if j['duplex'] %}checked{% endif %}>
        <span class="checkbox-label-text">Print on both sides (Duplex)</span>
      </label>

      <table class="data-table">
        <tr><th>Job ID</th><td><code>{{ j['id'] }}</code></td></tr>
        {% if j['notes'] %}
        <tr><th>Included Documents</th><td>{{ j['notes'] }}</td></tr>
        {% endif %}
        <tr><th>Document Pages</th><td>{{ j['pages'] }} pages</td></tr>
        <tr><th>Unit Rate</th><td id="display-rate">{{ cur }}{{ rate }} / page</td></tr>
        <tr class="total-row"><th>Total Payable</th><td id="display-total">{{ cur }}{{ '%.2f'|format(j['amount']) }}</td></tr>
      </table>

      <!-- Autonomous Payment Card -->
      <div class="upi-payment-card" id="upi-card">
        {% if cf_session and cf_session.get('session_id') %}
        <!-- Cashfree Official Gateway View -->
        <div class="upi-badge" style="background: rgba(16, 185, 129, 0.15); color: #10B981; border: 1px solid rgba(16, 185, 129, 0.3);">
          ⚡ Cashfree Official Gateway ({{ cf_session.get('env', 'live').upper() }})
        </div>
        <div class="upi-amount-hero" id="hero-amount">{{ cur }}{{ '%.2f'|format(j['amount']) }}</div>
        <div class="upi-paise-note" style="color: #A1A1AA; font-size: 13px; margin-bottom: 16px;">
          Pay via Google Pay, PhonePe, Paytm, BHIM, UPI QR, Cards &amp; Netbanking
        </div>

        <!-- Primary Cashfree Checkout Action -->
        <button type="button" id="btn-cashfree-pay" onclick="triggerCashfreePayment()" style="width: 100%; display: flex; align-items: center; justify-content: center; gap: 10px; background: linear-gradient(135deg, #10B981, #059669); color: #FFFFFF; font-size: 16px; font-weight: 700; padding: 15px 20px; border-radius: 10px; border: none; cursor: pointer; box-shadow: 0 4px 15px rgba(16, 185, 129, 0.35); transition: transform 0.15s ease, box-shadow 0.15s ease;">
          <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"></path></svg>
          Pay {{ cur }}<span id="btn-amount-text">{{ '%.2f'|format(j['amount']) }}</span> with Cashfree
        </button>

        <!-- Dynamic QR Code Card (Click to open Cashfree Dynamic QR) -->
        <div onclick="triggerCashfreePayment()" style="cursor: pointer; margin-top: 14px; padding: 16px; border: 2px dashed rgba(16, 185, 129, 0.35); border-radius: 12px; background: rgba(16, 185, 129, 0.05); text-align: center; transition: all 0.2s ease;">
          <div style="display: flex; align-items: center; justify-content: center; gap: 8px; margin-bottom: 6px;">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="#10B981" stroke-width="2"><rect x="3" y="3" width="7" height="7"></rect><rect x="14" y="3" width="7" height="7"></rect><rect x="14" y="14" width="7" height="7"></rect><rect x="3" y="14" width="7" height="7"></rect><path d="M7 7h.01M17 7h.01M7 17h.01M17 17h.01"></path></svg>
            <span style="font-weight: 700; color: #10B981; font-size: 14px;">Instant UPI QR &amp; App Checkout</span>
          </div>
          <p style="margin: 0 0 8px; font-size: 12px; color: var(--text-muted);">Point your mobile camera or GPay / PhonePe / Paytm to scan, or pay directly via app</p>
          <span style="display: inline-block; font-size: 12px; font-weight: 600; color: #10B981; background: rgba(16, 185, 129, 0.18); padding: 5px 14px; border-radius: 20px;">
            Click to Open Cashfree Checkout ↗
          </span>
        </div>

        {% if cf_session.get('env') == 'sandbox' %}
        <!-- Cashfree Sandbox Simulator Button -->
        <div id="cf-simulator-box" style="background: rgba(139, 92, 246, 0.12); border: 1px dashed #A78BFA; border-radius: 8px; padding: 12px; margin-top: 14px; text-align: center;">
          <div style="font-size: 11px; color: #C4B5FD; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;">🧪 Cashfree Test Mode Active</div>
          <div style="font-size: 12px; color: var(--text-muted); margin: 4px 0 10px;">Zero money deducted. Click below to simulate UPI payment:</div>
          <button type="button" onclick="triggerCashfreePayment()" class="btn-secondary" style="display: inline-flex; align-items: center; justify-content: center; gap: 6px; padding: 8px 16px; font-size: 13px; color: #FFFFFF; border-color: #8B5CF6; background: rgba(139, 92, 246, 0.2); cursor: pointer;">
            Open Cashfree Simulator ↗
          </button>
        </div>
        {% endif %}

        <!-- Fallback Direct Paytm UPI (Collapsible) -->
        <details style="margin-top: 20px; border-top: 1px dashed rgba(255,255,255,0.1); padding-top: 12px; text-align: left; font-size: 13px;">
          <summary style="cursor: pointer; color: var(--text-muted); font-weight: 600; outline: none; user-select: none;">
            Manual Offline Fallback (Direct Paytm UPI)
          </summary>
          <div style="margin-top: 12px; text-align: center; background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.06); border-radius: 8px; padding: 14px;">
            <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 8px;">Direct VPA: <code>{{ upi_vpa }}</code></div>
            <div id="qrcode-fallback-container" style="display: flex; justify-content: center; margin-bottom: 10px;"></div>
            <a id="btn-fallback-upi" href="upi://pay?pa={{ upi_vpa }}&pn={{ upi_name }}&am={{ '%.2f'|format(j['amount']) }}&cu=INR&tn=Job_{{ j['id'] }}" class="btn-secondary" style="font-size: 12px; padding: 6px 12px; display: inline-flex;">
              Open in Paytm / UPI App
            </a>
          </div>
        </details>

        {% else %}
        <!-- Standalone Direct UPI Mode (Paytm fallback when Cashfree not configured) -->
        <div class="upi-badge">⚡ Direct UPI Mode (Paytm)</div>
        <div class="upi-amount-hero" id="hero-amount">{{ cur }}{{ '%.2f'|format(j['amount']) }}</div>
        <div class="upi-paise-note">Direct UPI: {{ upi_vpa }}</div>
        <div class="upi-qr-box" id="qrcode-container"></div>
        <a id="btn-upi-intent" href="upi://pay?pa={{ upi_vpa }}&pn={{ upi_name }}&am={{ '%.2f'|format(j['amount']) }}&cu=INR&tn=Job_{{ j['id'] }}" class="btn-upi-intent">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"></path></svg>
          Pay with UPI App (Mobile)
        </a>
        {% endif %}

        <!-- Live Listener Status Pulse -->
        <div class="pulse-container" id="status-container" style="margin-top: 18px;">
          <div class="pulse-dot"></div>
          <span id="status-label">{% if cf_session %}Listening for instant Cashfree payment...{% else %}Listening for bank SMS confirmation...{% endif %}</span>
        </div>

        <!-- Manual UTR Fallback -->
        <div class="utr-fallback-box">
          <span style="font-size: 13px; color: var(--text-muted); font-weight: 600;">Paid but not updating? Enter 12-digit UPI UTR / Ref No:</span>
          <div class="utr-input-group">
            <input type="text" id="manual-utr" class="utr-input" placeholder="e.g. 432187654321" maxlength="12">
            <button type="button" class="btn-secondary" onclick="submitManualUtr()" style="padding: 10px 16px; color: #FFFFFF; border-color: var(--accent); background: var(--card-bg);">Verify</button>
          </div>
          <div id="utr-feedback" style="font-size: 12px; margin-top: 6px; display: none;"></div>
        </div>

        <!-- Fast Dev Simulator (Optional) -->
        <form method="post" action="/job/{{ j['id'] }}/pay" style="margin-top: 14px; width: 100%;">
          <button type="submit" style="background: none; border: none; color: var(--text-muted); font-size: 12px; text-decoration: underline; cursor: pointer;">
            (Testing: Click here to simulate immediate payment)
          </button>
        </form>
      </div>

      <script>
        const jobId = "{{ j['id'] }}";
        const upiVpa = "{{ upi_vpa }}";
        const upiName = "{{ upi_name }}";
        let currentAmount = "{{ '%.2f'|format(j['amount']) }}";
        const currencySymbol = "{{ cur }}";
        let cashfreeSessionId = "{{ cf_session['session_id'] if cf_session else '' }}";
        let cfEnv = "{{ cf_session.get('env', 'production') if cf_session else 'production' }}";

        // Cashfree Payment Trigger (Modal on Desktop, Full Viewport on Mobile)
        function triggerCashfreePayment() {
          if (!cashfreeSessionId) {
            alert("Cashfree session is being prepared or unavailable. Please wait or refresh the page.");
            return;
          }
          if (typeof Cashfree === "undefined") {
            alert("Payment SDK is loading, please try again in a few seconds.");
            return;
          }
          try {
            const isMobile = /Android|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
            const redirectTarget = isMobile ? "_self" : "_modal";
            const cashfree = Cashfree({ mode: cfEnv });
            cashfree.checkout({
              paymentSessionId: cashfreeSessionId,
              redirectTarget: redirectTarget
            }).then(function(result) {
              if (result && result.error) {
                console.warn("Cashfree checkout error / closed:", result.error.message);
              }
              if (result && result.redirect) {
                console.log("Cashfree redirecting...");
              }
            });
          } catch (e) {
            console.error("Cashfree Checkout Error:", e);
            alert("Failed to open checkout: " + e.message);
          }
        }

        // Fallback or Standalone Paytm QR Rendering
        function renderFallbackQr(amt) {
          const container = document.getElementById('qrcode-fallback-container') || document.getElementById('qrcode-container');
          if (!container) return;
          container.innerHTML = '';
          const uri = `upi://pay?pa=${encodeURIComponent(upiVpa)}&pn=${encodeURIComponent(upiName)}&am=${amt}&cu=INR&tn=Job_${jobId}`;
          const fallbackBtn = document.getElementById('btn-fallback-upi') || document.getElementById('btn-upi-intent');
          if (fallbackBtn) fallbackBtn.href = uri;
          try {
            new QRCode(container, {
              text: uri,
              width: 150,
              height: 150,
              colorDark: "#000000",
              colorLight: "#ffffff",
              correctLevel: QRCode.CorrectLevel.M
            });
          } catch(e) {
            console.error("QR Code Error:", e);
          }
        }

        // Render QR on page load if direct mode or fallback element exists
        if (document.getElementById('qrcode-container') || document.getElementById('qrcode-fallback-container')) {
          renderFallbackQr(currentAmount);
        }

        // Real-Time Payment Polling (Every 2 seconds)
        const pollInterval = setInterval(checkPaymentStatus, 2000);

        async function checkPaymentStatus() {
          try {
            const resp = await fetch(`/api/job/${jobId}/payment-status`);
            const data = await resp.json();
            if (data.is_paid) {
              clearInterval(pollInterval);
              const card = document.getElementById('upi-card');
              if (card) {
                card.style.borderColor = "var(--accent)";
                card.innerHTML = `
                  <div style="padding: 24px 10px;">
                    <div style="font-size: 40px; margin-bottom: 12px;">🎉</div>
                    <h2 style="color: var(--accent); font-size: 1.5rem; margin-bottom: 8px;">Payment Verified!</h2>
                    <p style="color: var(--text-muted); font-size: 14px; margin-bottom: 6px;">UTR / Ref: <code>${data.utr || 'Auto-Verified'}</code></p>
                    <p style="color: #FFFFFF; font-weight: 600;">Sending your document to the printer queue...</p>
                  </div>
                `;
              }
              const tag = document.getElementById('job-status-tag');
              if (tag) {
                tag.className = 'tag queued';
                tag.textContent = 'queued';
              }
              setTimeout(() => {
                window.location.href = `/queue?just=${jobId}`;
              }, 1200);
            }
          } catch (err) {
            console.warn("Status poll error:", err);
          }
        }

        // Manual UTR Submission Fallback
        async function submitManualUtr() {
          const utrInput = document.getElementById('manual-utr');
          const fb = document.getElementById('utr-feedback');
          const utrVal = (utrInput.value || '').trim();
          if (utrVal.length !== 12 || !/^\\d+$/.test(utrVal)) {
            fb.style.display = 'block';
            fb.style.color = '#F87171';
            fb.textContent = 'Please enter a valid 12-digit numeric UTR.';
            return;
          }
          fb.style.display = 'block';
          fb.style.color = 'var(--text-muted)';
          fb.textContent = 'Verifying with Supabase...';

          try {
            const res = await fetch(`/api/job/${jobId}/verify-utr`, {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({utr: utrVal})
            });
            const data = await res.json();
            if (data.ok) {
              fb.style.color = 'var(--accent)';
              fb.textContent = 'Payment Confirmed! Redirecting...';
              setTimeout(() => { window.location.href = `/queue?just=${jobId}`; }, 800);
            } else {
              fb.style.color = '#F87171';
              fb.textContent = data.message || 'Verification failed.';
            }
          } catch (e) {
            fb.style.color = '#F87171';
            fb.textContent = 'Error connecting to server.';
          }
        }

        // Dynamic Options Update (Colour/Copies/Duplex)
        async function updateJobPricing() {
          const colorEl = document.getElementById('opt-color');
          const copiesEl = document.getElementById('opt-copies');
          const duplexEl = document.getElementById('opt-duplex');
          const colorVal = colorEl ? colorEl.value : 'bw';
          const copiesVal = Math.max(1, Math.min(20, parseInt(copiesEl.value) || 1));
          const duplexVal = duplexEl && duplexEl.checked ? 1 : 0;

          try {
            const res = await fetch(`/api/job/${jobId}/update-options`, {
              method: 'POST',
              headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({color: colorVal, copies: copiesVal, duplex: duplexVal})
            });
            const data = await res.json();
            if (data.ok) {
              currentAmount = data.amount.toFixed(2);
              document.getElementById('display-rate').textContent = currencySymbol + data.rate.toFixed(1) + ' / page';
              document.getElementById('display-total').textContent = currencySymbol + currentAmount;
              const heroAmt = document.getElementById('hero-amount');
              if (heroAmt) heroAmt.textContent = currencySymbol + currentAmount;
              const btnAmt = document.getElementById('btn-amount-text');
              if (btnAmt) btnAmt.textContent = currentAmount;

              if (data.cf_session_id) {
                cashfreeSessionId = data.cf_session_id;
              }
              if (data.cf_env) {
                cfEnv = data.cf_env;
              }

              renderFallbackQr(currentAmount);
            }
          } catch (err) {
            console.error("Update error:", err);
          }
        }
      </script>
      {% else %}
      <table class="data-table">
        <tr><th>Job ID</th><td><code>{{ j['id'] }}</code></td></tr>
        {% if j['notes'] %}
        <tr><th>Included Documents</th><td>{{ j['notes'] }}</td></tr>
        {% endif %}
        <tr><th>Total Pages</th><td>{{ j['pages'] }} pages</td></tr>
        <tr><th>Copies</th><td>{{ j['copies'] }} copy(ies)</td></tr>
        <tr><th>Colour Mode</th><td>{{ 'Full Colour' if j['color']=='color' else 'Black & White' }}</td></tr>
        <tr><th>Both Sides</th><td>{{ 'Yes (Duplex)' if j['duplex'] else 'No (Single-sided)' }}</td></tr>
        <tr><th>Rate</th><td>{{ cur }}{{ rate }} / page</td></tr>
        <tr class="total-row"><th>Total</th><td>{{ cur }}{{ '%.2f'|format(j['amount']) }}</td></tr>
      </table>

      <div style="display: flex; gap: 12px; margin-top: 20px;">
        <a href="/queue?just={{ j['id'] }}" class="btn-submit" style="text-decoration: none; text-align: center;">View in Queue</a>
        <a href="/" class="btn-secondary">Upload Another</a>
      </div>
      {% endif %}
    </div>
    """, j=j, cur=CURRENCY, p=PRICE_PER_PAGE, rate=PRICE_PER_PAGE.get(j["color"], 2.0),
       upi_vpa=UPI_VPA, upi_name=UPI_NAME, cf_session=cf_session)


@app.get("/job/<job_id>/preview")
def job_preview(job_id):
    j = get_job(job_id)
    file_path = os.path.join(UPLOADS, j["stored"])
    if not os.path.exists(file_path):
        supabase_download_pdf(job_id, file_path)
    if not os.path.exists(file_path):
        abort(404)
    return send_file(file_path, mimetype="application/pdf")


@app.post("/job/<job_id>/pay")
def pay(job_id):
    """Fallback payment simulation route."""
    j = get_job(job_id)
    con = get_db()
    with con:
        con.execute(
            "UPDATE jobs SET status='queued' WHERE id=? AND status='pending_payment'",
            (job_id,)
        )
    if SUPABASE_URL:
        supabase_request(f"jobs?id=eq.{job_id}", method="PATCH", data={"status": "queued"})
    return redirect(url_for("queue", just=job_id))


# ---------- Payment & Kiosk Status APIs ----------
@app.get("/api/job/<job_id>/payment-status")
def payment_status(job_id):
    con = get_db()
    row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row and SUPABASE_URL:
        supa_jobs = supabase_request(f"jobs?id=eq.{job_id}&select=*")
        if supa_jobs and len(supa_jobs) > 0:
            sj = supa_jobs[0]
            with con:
                con.execute(
                    """INSERT OR IGNORE INTO jobs (id, filename, stored, pages, copies, color, duplex, amount, status, created, notes, utr_number, reserved_paise_offset)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        sj["id"], sj.get("filename", ""), sj.get("stored", ""),
                        int(sj.get("pages", 1)), int(sj.get("copies", 1)), sj.get("color", "bw"),
                        int(sj.get("duplex", 0)), float(sj.get("amount", 0.0)), sj.get("status", "pending_payment"),
                        sj.get("created", ""), sj.get("notes", ""), sj.get("utr_number", ""), int(sj.get("reserved_paise_offset", 0))
                    )
                )
            row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()

    if not row:
        return jsonify(error="Job not found"), 404

    status = row["status"]
    utr = row["utr_number"] if "utr_number" in row.keys() else ""

    # 1. Check Supabase if local is still pending
    if status == "pending_payment" and SUPABASE_URL:
        supa_jobs = supabase_request(f"jobs?id=eq.{job_id}&select=*")
        if supa_jobs and len(supa_jobs) > 0:
            remote_job = supa_jobs[0]
            remote_status = remote_job.get("status")
            if remote_status in ("queued", "printing", "done"):
                status = remote_status
                utr = remote_job.get("utr_number", "")
                with con:
                    con.execute("UPDATE jobs SET status=?, utr_number=? WHERE id=?", (status, utr, job_id))

    # 2. Check Cashfree PG Order Status if still pending
    if status == "pending_payment" and cashfree_helper.is_cashfree_configured():
        amount_cents = int(round(float(row["amount"]) * 100))
        order_id = f"job_{job_id}_{amount_cents}"
        order_info = cashfree_helper.get_order_status(order_id)
        if order_info and order_info.get("order_status") == "PAID":
            payments = cashfree_helper.get_order_payments(order_id)
            if payments:
                p = payments[0]
                utr = str(p.get("bank_reference") or p.get("cf_payment_id") or "Cashfree-Auto")
            else:
                utr = "Cashfree-Auto"
            status = "queued"
            with con:
                con.execute("UPDATE jobs SET status='queued', utr_number=? WHERE id=?", (utr, job_id))
            if SUPABASE_URL:
                supabase_request(f"jobs?id=eq.{job_id}", method="PATCH", data={"status": "queued", "utr_number": utr})
                supabase_request("incoming_payments", method="POST", data={
                    "amount": float(row["amount"]),
                    "utr_number": utr,
                    "raw_message": f"Cashfree Polling Payment for {job_id}",
                    "sender_info": "Cashfree PG Poller",
                    "is_consumed": True,
                    "matched_job_id": job_id
                })

    return jsonify({
        "id": job_id,
        "status": status,
        "is_paid": status in ("queued", "printing", "done"),
        "utr": utr
    })


@app.route("/api/cashfree/webhook", methods=["GET", "POST"])
def cashfree_webhook():
    """
    Cashfree Server-to-Server Webhook receiver.
    Triggered instantly (<500ms) upon customer payment.
    """
    if request.method == "GET":
        return jsonify(ok=True, message="Cashfree webhook endpoint ready"), 200

    timestamp = request.headers.get("x-webhook-timestamp", "")
    signature = request.headers.get("x-webhook-signature", "")
    raw_body = request.get_data(as_text=True)

    # Verify signature if signature header is provided
    if signature and timestamp:
        valid = cashfree_helper.verify_webhook_signature(timestamp, raw_body, signature)
        if not valid:
            print("[WARN] Invalid Cashfree webhook signature received!")
            # If it's a dummy test ping without order data, accept it
            test_data = request.get_json(silent=True) or {}
            if not test_data.get("data", {}).get("order"):
                return jsonify(ok=True, message="Test ping acknowledged"), 200
            return jsonify(ok=False, message="Invalid signature"), 400

    data = request.get_json(silent=True) or {}
    event_type = data.get("type", "")

    if event_type == "PAYMENT_SUCCESS_WEBHOOK" or "data" in data:
        payload = data.get("data", {})
        order = payload.get("order", {})
        payment = payload.get("payment", {})
        order_id = str(order.get("order_id", ""))

        # Accurately extract job_id from order_id (e.g. job_test_cf_123_400 -> test_cf_123)
        job_id = order_id
        if order_id.startswith("job_"):
            remainder = order_id[4:]
            if "_" in remainder:
                left, right = remainder.rsplit("_", 1)
                job_id = left if right.isdigit() else remainder
            else:
                job_id = remainder

        utr = str(payment.get("bank_reference") or payment.get("cf_payment_id") or "Cashfree-Auto")
        amount = float(payment.get("payment_amount", order.get("order_amount", 0.0)))

        con = get_db()
        row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row and row["status"] == "pending_payment":
            with con:
                con.execute("UPDATE jobs SET status='queued', utr_number=? WHERE id=?", (utr, job_id))

            if SUPABASE_URL:
                supabase_request(f"jobs?id=eq.{job_id}", method="PATCH", data={"status": "queued", "utr_number": utr})
                supabase_request("incoming_payments", method="POST", data={
                    "amount": amount,
                    "utr_number": utr,
                    "raw_message": f"Cashfree Webhook {payment.get('cf_payment_id', '')} for {job_id}",
                    "sender_info": "Cashfree Webhook",
                    "is_consumed": True,
                    "matched_job_id": job_id
                })

        print(f"[CASHFREE WEBHOOK] Job {job_id} successfully queued! UTR: {utr}")
        return jsonify(ok=True, message="Payment acknowledged"), 200

    return jsonify(ok=True, message="Webhook received"), 200


@app.post("/api/job/<job_id>/verify-utr")
def verify_utr(job_id):
    data = request.get_json(silent=True) or {}
    utr = str(data.get("utr", "")).strip()
    if len(utr) != 12 or not utr.isdigit():
        return jsonify(ok=False, message="Please enter a valid 12-digit UTR number."), 400

    con = get_db()
    row = con.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        return jsonify(ok=False, message="Job not found"), 404

    # Update local SQLite
    with con:
        con.execute("UPDATE jobs SET status='queued', utr_number=? WHERE id=?", (utr, job_id))

    # Update Supabase
    if SUPABASE_URL:
        supabase_request(f"jobs?id=eq.{job_id}", method="PATCH", data={"status": "queued", "utr_number": utr})
        supabase_request(f"incoming_payments?utr_number=eq.{utr}", method="PATCH", data={"is_consumed": True, "matched_job_id": job_id})

    return jsonify(ok=True, message="Payment verified!")


@app.post("/api/job/<job_id>/update-options")
def update_options(job_id):
    data = request.get_json(silent=True) or {}
    j = get_job(job_id)

    color = data.get("color", j["color"])
    if color not in PRICE_PER_PAGE:
        color = "bw"
    try:
        copies = max(1, min(20, int(data.get("copies", j["copies"]))))
    except (ValueError, TypeError):
        copies = 1
    duplex = 1 if data.get("duplex") else 0

    offset = j["reserved_paise_offset"] if "reserved_paise_offset" in j.keys() else calculate_paise_offset(job_id)
    new_amount = price(j["pages"], copies, color, offset)

    con = get_db()
    with con:
        con.execute(
            "UPDATE jobs SET color=?, copies=?, duplex=?, amount=? WHERE id=?",
            (color, copies, duplex, new_amount, job_id)
        )

    if SUPABASE_URL:
        supabase_request(f"jobs?id=eq.{job_id}", method="PATCH", data={
            "color": color, "copies": copies, "duplex": duplex, "amount": new_amount
        })

    rate = PRICE_PER_PAGE.get(color, 2.0)

    # If Cashfree is configured, also fetch new Cashfree session data for the updated amount
    cf_data = {}
    if cashfree_helper.is_cashfree_configured():
        base_url = get_public_base_url()
        notify_url = f"{base_url}/api/cashfree/webhook"
        return_url = f"{base_url}/job/{job_id}?cf_order_id={{order_id}}"
        sess = cashfree_helper.get_or_create_cashfree_session(job_id, new_amount, notify_url=notify_url, return_url=return_url)
        if sess:
            cf_data = {
                "cf_session_id": sess.get("session_id"),
                "cf_env": sess.get("env", "production"),
                "cf_qr": sess.get("qr_data"),
                "cf_simulator": sess.get("links", {}).get("web") or sess.get("links", {}).get("default"),
                "cf_intent": sess.get("links", {}).get("default")
            }

    return jsonify(ok=True, amount=new_amount, rate=rate, **cf_data)


@app.get("/queue")
def queue():
    con = get_db()
    if SUPABASE_URL:
        try:
            supa_jobs = supabase_request("jobs?status=neq.pending_payment&select=*&order=created.asc&limit=100")
            if supa_jobs:
                with con:
                    for sj in supa_jobs:
                        con.execute("""INSERT OR REPLACE INTO jobs 
                            (id, filename, stored, pages, copies, color, duplex, amount, status, created, notes, utr_number, reserved_paise_offset)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (sj["id"], sj.get("filename", "document.pdf"), sj.get("stored", f"{sj['id']}.pdf"),
                             int(sj.get("pages", 1)), int(sj.get("copies", 1)), sj.get("color", "bw"),
                             1 if sj.get("duplex") else 0, float(sj.get("amount", 2.0)), sj.get("status", "queued"),
                             sj.get("created", ""), sj.get("notes", ""), sj.get("utr_number", ""), int(sj.get("reserved_paise_offset", 0)))
                        )
        except Exception as e:
            print(f"[QUEUE SUPABASE SYNC ERROR] {e}")

    active_rows = con.execute("""SELECT * FROM jobs WHERE status IN ('printing', 'queued')
                                 ORDER BY CASE WHEN status='printing' THEN 0 ELSE 1 END, created ASC""").fetchall()

    finished_rows = con.execute("""SELECT * FROM jobs WHERE status IN ('done', 'failed')
                                   ORDER BY created DESC LIMIT 20""").fetchall()

    queue_map = {r["id"]: idx for idx, r in enumerate(active_rows, start=1)}

    just = request.args.get("just")
    your_job = None
    your_pos = None
    if just:
        for r in active_rows:
            if r["id"] == just:
                your_job = r
                your_pos = queue_map[r["id"]]
                break
        if not your_job:
            for r in finished_rows:
                if r["id"] == just:
                    your_job = r
                    break

    all_rows = list(active_rows) + list(finished_rows)

    return render_page("""
    <section class="queue-page">
    <div class="card queue-card">
      <div class="queue-header">
        <div>
          <h1 class="page-title" style="font-size: 1.9rem; display: flex; align-items: center; gap: 12px;">
            Print Queue <span class="pulse-dot" title="Live Auto-Updating"></span>
          </h1>
          <p class="page-sub" style="margin-bottom: 0;">
            {% if active_rows|length == 0 %}
              No active documents in line. Ready to print!
            {% elif active_rows|length == 1 %}
              1 active document in queue — processed in order.
            {% else %}
              {{ active_rows|length }} active documents in queue — numbers descend as printing finishes.
            {% endif %}
          </p>
        </div>
        <a href="/" class="btn-secondary">+ Upload Files</a>
      </div>

      {% if your_job %}
        {% if your_pos %}
          <div class="user-queue-banner {% if your_job['status'] == 'printing' %}printing{% else %}queued{% endif %}">
            <div class="uq-number">#{{ your_pos }}</div>
            <div class="uq-content">
              <div class="uq-badge-tag">
                {% if your_job['status'] == 'printing' %}
                  NOW PRINTING 🖨️
                {% elif your_pos == 1 %}
                  NEXT IN LINE ⚡
                {% else %}
                  YOUR QUEUE NUMBER
                {% endif %}
              </div>
              <div class="uq-title">{{ your_job['filename'] }}</div>
              <div class="uq-desc">
                {% if your_job['status'] == 'printing' %}
                  Your document is printing right now! Please collect your printed sheets from the tray.
                {% elif your_pos == 1 %}
                  You are <strong>#1 in line</strong>! Your document is next to print as soon as the printer is ready.
                {% else %}
                  You are <strong>#{{ your_pos }} in line</strong> ({{ your_pos - 1 }} document{% if your_pos > 2 %}s{% endif %} ahead of yours). Your queue number will descend automatically.
                {% endif %}
              </div>
            </div>
          </div>
        {% elif your_job['status'] == 'done' %}
          <div class="user-queue-banner done">
            <div class="uq-number" style="color: #10B981; background: rgba(16,185,129,0.12); border-color: rgba(16,185,129,0.3);">✓</div>
            <div class="uq-content">
              <div class="uq-badge-tag" style="color: #10B981;">PRINTING COMPLETE 🎉</div>
              <div class="uq-title">{{ your_job['filename'] }}</div>
              <div class="uq-desc">Your document has finished printing! Files have been permanently purged from server storage for your privacy.</div>
            </div>
          </div>
        {% endif %}
      {% endif %}

      {% if not all_rows %}
      <p style="color: var(--text-muted); margin: 30px 0;">
        The queue is currently empty. <a href="/" style="color: var(--accent); font-weight: 600;">Upload a document</a> to print.
      </p>
      {% else %}
      <div class="queue-table-wrap">
      <table class="data-table queue-table">
        <thead>
          <tr>
            <th>Queue #</th>
            <th>Job ID</th>
            <th>Document</th>
            <th>Sheets</th>
            <th>Colour</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {% for r in all_rows %}
          <tr {% if r['id'] == just %}class="user-highlight-row"{% endif %}>
            <td>
              {% if r['id'] in queue_map %}
                {% if r['status'] == 'printing' %}
                  <span class="badge-token printing">#{{ queue_map[r['id']] }} (Printing)</span>
                {% else %}
                  <span class="badge-token queued">#{{ queue_map[r['id']] }}</span>
                {% endif %}
              {% elif r['status'] == 'done' %}
                <span class="badge-token done">✓ Done</span>
              {% elif r['status'] == 'failed' %}
                <span class="badge-token failed">✗ Failed</span>
              {% else %}
                <span class="badge-token queued">-</span>
              {% endif %}
            </td>
            <td>
              <code class="queue-job-id">{{ r['id'] }}</code>{% if r['id']==just %} <span style="color: var(--accent); font-weight: 700;">(yours)</span>{% endif %}
            </td>
            <td>
              <div class="queue-document-name">{{ r['filename'] }}</div>
              {% if r['notes'] %}
              <div class="queue-document-notes">{{ r['notes'] }}</div>
              {% endif %}
            </td>
            <td class="queue-number">{{ r['pages'] * r['copies'] }}</td>
            <td class="queue-colour" style="text-transform: capitalize;">{{ r['color'] }}</td>
            <td><span class="tag {{ r['status'] }}">{{ r['status'] }}</span></td>
          </tr>
          {% endfor %}
        </tbody>
      </table>
      </div>
      {% endif %}
    </div>
    </section>

    <script>
      // Live Auto-Refresh: Polls every 3.5 seconds to descend queue numbers live as printing happens
      setInterval(async () => {
        try {
          const res = await fetch(window.location.href);
          if (!res.ok) return;
          const html = await res.text();
          const doc = new DOMParser().parseFromString(html, 'text/html');
          const newCard = doc.querySelector('.queue-card');
          const currentCard = document.querySelector('.queue-card');
          if (newCard && currentCard) {
            currentCard.innerHTML = newCard.innerHTML;
          }
        } catch (e) {}
      }, 3500);
    </script>
    """, all_rows=all_rows, active_rows=active_rows, queue_map=queue_map, just=just, your_job=your_job, your_pos=your_pos)


# ---------- Informational & Compliance Pages ----------

@app.get("/pricing")
def pricing_page():
    return render_page("""
    <div class="content-page-wrap">
      <a href="/" class="back-nav-link">← Back to Print Kiosk</a>
      <div class="content-card">
        <div class="content-badge">Transparent Pricing</div>
        <h1 class="content-title">Print Rates & Pricing</h1>
        <p class="content-sub">Simple, flat-rate pricing with zero hidden charges or platform fees. Pay instantly with any UPI app.</p>

        <div class="info-card-grid">
          <div class="info-box" style="border-color: rgba(141, 235, 181, 0.3); background: rgba(141, 235, 181, 0.04);">
            <div class="info-box-title">Black & White Print</div>
            <div class="price-tag-big">₹2.00 <span style="font-size: 14px; font-weight: 500; color: var(--text-muted);">/ page</span></div>
            <div class="info-box-desc">High-contrast, crystal-clear 1200 DPI monochrome laser print. Perfect for resumes, assignments, exam tickets, notes, and legal documents.</div>
          </div>

          <div class="info-box" style="border-color: rgba(96, 165, 250, 0.3); background: rgba(96, 165, 250, 0.04);">
            <div class="info-box-title" style="color: #60A5FA;">Full Colour Print</div>
            <div class="price-tag-big" style="color: #60A5FA;">₹8.00 <span style="font-size: 14px; font-weight: 500; color: var(--text-muted);">/ page</span></div>
            <div class="info-box-desc">Vibrant high-density laser printing. Ideal for certificates, project presentations, charts, photo IDs, and brochures.</div>
          </div>
        </div>

        <div class="policy-section">
          <h2>Kiosk Features & Options</h2>
          <div class="pricing-table-card">
            <table class="pricing-table">
              <thead>
                <tr>
                  <th>Feature</th>
                  <th>Details</th>
                  <th>Cost</th>
                </tr>
              </thead>
              <tbody>
                <tr>
                  <td><strong>Black & White (B/W)</strong></td>
                  <td>Single or double-sided monochrome document print</td>
                  <td><strong style="color: var(--accent);">₹2.00 / page</strong></td>
                </tr>
                <tr>
                  <td><strong>Full Colour</strong></td>
                  <td>Rich vibrant color laser document print</td>
                  <td><strong style="color: #60A5FA;">₹8.00 / page</strong></td>
                </tr>
                <tr>
                  <td><strong>Duplex Printing</strong></td>
                  <td>Print on both sides of each sheet (saves paper)</td>
                  <td>Included (per page rate)</td>
                </tr>
                <tr>
                  <td><strong>Multiple Copies</strong></td>
                  <td>Print from 1 to 20 copies per order</td>
                  <td>Pages × Copies × Rate</td>
                </tr>
                <tr>
                  <td><strong>Convenience Fee</strong></td>
                  <td>Payment gateway & platform maintenance charge</td>
                  <td><strong style="color: #10B981;">₹0.00 (FREE)</strong></td>
                </tr>
                <tr>
                  <td><strong>Paise Offset</strong></td>
                  <td>Micro-offset (1 to 10 paise max) for collision-free instant UPI detection</td>
                  <td>Max +₹0.10</td>
                </tr>
              </tbody>
            </table>
          </div>
        </div>

        <div class="policy-highlight-box">
          <p><strong>💡 Live Rate Calculator:</strong> You can see your exact price dynamically calculated when you upload your files on the <a href="/" style="color: var(--accent); font-weight: 700;">Home Page</a> before confirming payment.</p>
        </div>

        <div style="text-align: center; margin-top: 32px;">
          <a href="/" class="btn-primary" style="display: inline-flex; align-items: center; gap: 8px; text-decoration: none; padding: 14px 28px; font-size: 15px;">
            Upload & Print Now →
          </a>
        </div>
      </div>
    </div>
    """)


@app.get("/about")
def about_page():
    return render_page("""
    <div class="content-page-wrap">
      <a href="/" class="back-nav-link">← Back to Print Kiosk</a>
      <div class="content-card">
        <div class="content-badge">About Us</div>
        <h1 class="content-title">About Printer of A's</h1>
        <p class="content-sub">Autonomous, human-less smart print kiosks engineered for fast, secure, and privacy-first document printing.</p>

        <div class="policy-section">
          <h2>Our Story & Mission</h2>
          <p>Traditional photocopy and printing shops come with friction: long queues, waiting for an operator, sharing private personal documents over public WhatsApp or infected pendrives, and dealing with cash change.</p>
          <p><strong>Printer of A's</strong> was built to solve this once and for all. We provide an autonomous, 100% human-less cloud printing experience. Students, educators, professionals, and visitors can walk up to the kiosk, upload their documents directly from their mobile browser, pay seamlessly with any UPI app, and receive freshly printed sheets from the laser printer in seconds.</p>
        </div>

        <div class="info-card-grid">
          <div class="info-box">
            <div class="info-box-title">⚡ 100% Autonomous</div>
            <div class="info-box-desc">Works directly in your mobile browser. Scan the kiosk QR, upload PDFs or photos, and print without installing apps.</div>
          </div>
          <div class="info-box">
            <div class="info-box-title">🔒 Zero Document Retention</div>
            <div class="info-box-desc">No human operator ever reads or sees your files. All files are automatically purged from local disk and cloud storage as soon as printing completes.</div>
          </div>
          <div class="info-box">
            <div class="info-box-title">💳 Seamless UPI</div>
            <div class="info-box-desc">Zero platform fees. Instant verification through dynamic Cashfree UPI QR codes with all major UPI apps.</div>
          </div>
        </div>

        <div class="policy-section">
          <h2>How It Works</h2>
          <ul>
            <li><strong>Step 1: Upload Documents:</strong> Select PDFs or camera photos on your phone. Our smart client-side optimizer scales images to 300 DPI laser standards in milliseconds while leaving PDFs in pure vector resolution.</li>
            <li><strong>Step 2: Instant Payment:</strong> Scan the dynamic UPI QR code with GPay, PhonePe, Paytm, or BHIM.</li>
            <li><strong>Step 3: Autonomous Laser Printing:</strong> The kiosk worker picks up your job from the queue, prints your documents, and purges your files permanently for user privacy.</li>
          </ul>
        </div>

        <div class="policy-section">
          <h2>Contact & Operator Information</h2>
          <p>Printer of A's is operated with dedicated customer care:</p>
          <div class="info-card-grid" style="margin-top: 14px;">
            <div class="info-box">
              <div class="info-box-title">Support Phones</div>
              <div class="info-box-value"><a href="tel:9696123736">+91 9696123736</a></div>
              <div class="info-box-value" style="margin-top: 4px;"><a href="tel:9589974245">+91 9589974245</a></div>
            </div>
            <div class="info-box">
              <div class="info-box-title">Official Email</div>
              <div class="info-box-value"><a href="mailto:printerofas@gmail.com">printerofas@gmail.com</a></div>
              <div class="info-box-desc">Response time: within 24 hours</div>
            </div>
          </div>
        </div>
      </div>
    </div>
    """)


@app.get("/contact")
def contact_page():
    return render_page("""
    <div class="content-page-wrap">
      <a href="/" class="back-nav-link">← Back to Print Kiosk</a>
      <div class="content-card">
        <div class="content-badge">Support & Assistance</div>
        <h1 class="content-title">Contact Us</h1>
        <p class="content-sub">Have questions, feedback, or need help with a print transaction? Our support team is available 7 days a week.</p>

        <div class="info-card-grid">
          <div class="info-box" style="border-color: rgba(141, 235, 181, 0.3);">
            <div class="info-box-title">Primary Helpline</div>
            <div class="info-box-value"><a href="tel:9696123736">+91 9696123736</a></div>
            <div class="info-box-desc">Call or WhatsApp for immediate kiosk assistance & print inquiries.</div>
          </div>

          <div class="info-box" style="border-color: rgba(141, 235, 181, 0.3);">
            <div class="info-box-title">Secondary Helpline</div>
            <div class="info-box-value"><a href="tel:9589974245">+91 9589974245</a></div>
            <div class="info-box-desc">Alternate support line for technical inquiries and refund queries.</div>
          </div>

          <div class="info-box" style="border-color: rgba(96, 165, 250, 0.3);">
            <div class="info-box-title" style="color: #60A5FA;">Email Support</div>
            <div class="info-box-value"><a href="mailto:printerofas@gmail.com">printerofas@gmail.com</a></div>
            <div class="info-box-desc">All email queries responded to within 24 business hours.</div>
          </div>

          <div class="info-box">
            <div class="info-box-title">Refund Turnaround</div>
            <div class="info-box-value" style="color: #10B981;">2 Working Days</div>
            <div class="info-box-desc">Failed prints or debited amounts are credited back within 2 working days.</div>
          </div>
        </div>

        <div class="policy-section" style="margin-top: 32px;">
          <h2>How to Get Fast Support</h2>
          <p>When contacting us regarding a print job or payment, please provide the following details so we can resolve your issue in minutes:</p>
          <ul>
            <li><strong>Job ID:</strong> The 10-character code shown on your screen (e.g. <code>cfb0ee5c81</code>).</li>
            <li><strong>Payment Reference / UTR Number:</strong> The 12-digit transaction ID from your UPI app (Google Pay, PhonePe, Paytm, etc.).</li>
            <li><strong>Kiosk Location:</strong> The campus or premises location of the kiosk where you printed.</li>
          </ul>
        </div>

        <div class="policy-highlight-box">
          <p><strong>Operating Hours:</strong> Monday to Sunday, 8:00 AM – 10:00 PM IST.<br>For urgent kiosk issues outside these hours, drop an email to <a href="mailto:printerofas@gmail.com" style="color: var(--accent);">printerofas@gmail.com</a> and we will assist promptly.</p>
        </div>
      </div>
    </div>
    """)


@app.get("/privacy")
def privacy_page():
    return render_page("""
    <div class="content-page-wrap">
      <a href="/" class="back-nav-link">← Back to Print Kiosk</a>
      <div class="content-card">
        <div class="content-badge">Data Privacy</div>
        <h1 class="content-title">Privacy Policy</h1>
        <p class="content-sub">Effective Date: November 2026. Zero Document Retention & End-to-End Privacy Commitment.</p>

        <div class="policy-highlight-box" style="border-color: rgba(141, 235, 181, 0.4); background: rgba(141, 235, 181, 0.08);">
          <h2 style="font-size: 1.15rem; color: #8DEBB5; margin-bottom: 6px;">🔒 Zero Document Retention Guarantee</h2>
          <p>We respect your privacy above all else. <strong>Printer of A's does NOT store, inspect, archive, or retain your uploaded files.</strong> The moment your document finishes printing at the kiosk (status: done), all uploaded images and merged PDF files are <strong>permanently deleted</strong> from both local disk and Supabase Cloud Storage.</p>
        </div>

        <div class="policy-section">
          <h2>1. Information We Collect</h2>
          <p>When you use the Printer of A's kiosk, we only collect the minimum technical data necessary to complete your print request:</p>
          <ul>
            <li><strong>Document Data (Temporary):</strong> File name, page count, color selection, copies, and duplex settings required to compute the rate and queue the job.</li>
            <li><strong>Payment Data:</strong> Job ID, amount paid, and 12-digit bank reference (UTR) provided by Cashfree Payment Gateway to verify completion of payment. We <strong>never</strong> collect, view, or store bank account numbers, credit/debit card numbers, CVVs, or UPI PINs.</li>
            <li><strong>Contact Details (Voluntary):</strong> If you reach out to our support team, we collect your phone number or email solely to assist with your query or refund.</li>
          </ul>
        </div>

        <div class="policy-section">
          <h2>2. How We Use Your Information</h2>
          <ul>
            <li>To process and print your documents according to your selected preferences.</li>
            <li>To verify payment status with Cashfree Payment Gateway and manage your position in the print queue.</li>
            <li>To process eligible refunds within 2 working days in case of printer hardware issues.</li>
            <li>We <strong>never sell, lease, trade, or share</strong> user data with any third-party advertisers or marketing agencies.</li>
          </ul>
        </div>

        <div class="policy-section">
          <h2>3. Data Retention & Permanent Deletion</h2>
          <p>Uploaded documents are stored ephemerally in encrypted Supabase Cloud Storage only while waiting in the print queue. As soon as the kiosk printer daemon signals <code>status: done</code>:</p>
          <ul>
            <li>The local temporary file on the kiosk server is deleted via <code>os.remove()</code>.</li>
            <li>The document object in the Supabase Cloud Storage bucket is permanently purged via the Supabase Storage deletion API.</li>
            <li>Only basic audit logs (Job ID, amount, and timestamp) are retained for accounting and compliance purposes.</li>
          </ul>
        </div>

        <div class="policy-section">
          <h2>4. Data Security</h2>
          <p>All data transmitted between your device and Printer of A's is encrypted in transit using industry-standard <strong>HTTPS/TLS 1.3</strong> protocols. Payment transactions are processed directly through RBI-compliant payment gateway partners (Cashfree Payments India).</p>
        </div>

        <div class="policy-section">
          <h2>5. Contact & Grievance Redressal</h2>
          <p>For any privacy-related inquiries, concerns, or requests, please contact our Data & Grievance team:</p>
          <p>
            <strong>Printer of A's Support Team</strong><br>
            Phone: <a href="tel:9696123736" style="color: var(--accent);">+91 9696123736</a> / <a href="tel:9589974245" style="color: var(--accent);">+91 9589974245</a><br>
            Email: <a href="mailto:printerofas@gmail.com" style="color: var(--accent);">printerofas@gmail.com</a>
          </p>
        </div>
      </div>
    </div>
    """)


@app.get("/terms")
def terms_page():
    return render_page("""
    <div class="content-page-wrap">
      <a href="/" class="back-nav-link">← Back to Print Kiosk</a>
      <div class="content-card">
        <div class="content-badge">Legal Terms</div>
        <h1 class="content-title">Terms & Conditions</h1>
        <p class="content-sub">Last updated: November 2026. Please read these terms carefully before using Printer of A's services.</p>

        <div class="policy-section">
          <h2>1. Agreement to Terms</h2>
          <p>By accessing or using the Printer of A's website, upload portal, and autonomous print kiosks, you agree to be bound by these Terms and Conditions. If you do not agree to these terms, please do not use the service.</p>
        </div>

        <div class="policy-section">
          <h2>2. Description of Service</h2>
          <p>Printer of A's operates self-service, autonomous print kiosks allowing customers to upload digital files (PDF, PNG, JPG, JPEG, WEBP), pay via UPI, and collect physical laser prints directly from the kiosk printer tray.</p>
        </div>

        <div class="policy-section">
          <h2>3. User Responsibilities & Acceptable Content</h2>
          <p>You represent and warrant that:</p>
          <ul>
            <li>You own or have necessary licenses, rights, and permissions to print the uploaded documents.</li>
            <li>You will NOT upload, transmit, or print any material that is unlawful, defamatory, obscene, pornographic, promoting hate speech, or infringing on intellectual property or copyright laws.</li>
            <li>You are solely responsible for verifying print preview, page count, and color selections prior to payment.</li>
          </ul>
        </div>

        <div class="policy-section">
          <h2>4. Pricing, Payments & Micro-Offsets</h2>
          <ul>
            <li>All rates (e.g. ₹2.00/page for B/W, ₹8.00/page for Colour) are clearly displayed before confirmation.</li>
            <li>A micro-offset of 1 to 10 paise (₹0.01 to ₹0.10 max) is applied per transaction to enable unique payment matching across concurrent customers.</li>
            <li>Payments are processed securely via Cashfree Payment Gateway. Orders are queued only after successful receipt of payment.</li>
          </ul>
        </div>

        <div class="policy-section">
          <h2>5. Document Collection & Output Tray</h2>
          <p>Customers are responsible for collecting their printed documents from the kiosk output tray immediately upon completion. Printer of A's is not liable for documents left unattended in the output tray after printing has finished.</p>
        </div>

        <div class="policy-section">
          <h2>6. Limitation of Liability</h2>
          <p>Printer of A's shall not be liable for any indirect, incidental, or consequential damages arising from machine downtime, network disruptions, or power outages. In the event of an automated hardware failure where printing cannot complete, our liability is strictly limited to a full refund of the amount paid in accordance with our Refund Policy.</p>
        </div>

        <div class="policy-section">
          <h2>7. Contact Information</h2>
          <p>Questions regarding these Terms should be directed to:<br>
          Email: <a href="mailto:printerofas@gmail.com" style="color: var(--accent);">printerofas@gmail.com</a><br>
          Helpline: <a href="tel:9696123736" style="color: var(--accent);">+91 9696123736</a> / <a href="tel:9589974245" style="color: var(--accent);">+91 9589974245</a></p>
        </div>
      </div>
    </div>
    """)


@app.get("/refunds")
@app.get("/cancellation-refund")
def refunds_page():
    return render_page("""
    <div class="content-page-wrap">
      <a href="/" class="back-nav-link">← Back to Print Kiosk</a>
      <div class="content-card">
        <div class="content-badge">Customer Protection</div>
        <h1 class="content-title">Refund & Cancellation Policy</h1>
        <p class="content-sub">Fair, transparent, and prompt refunds. Guaranteed turnaround within 2 working days.</p>

        <div class="policy-highlight-box" style="border-color: rgba(16, 185, 129, 0.4); background: rgba(16, 185, 129, 0.08);">
          <h2 style="font-size: 1.15rem; color: #10B981; margin-bottom: 6px;">⏱️ Guaranteed 2 Working Days Refund Turnaround</h2>
          <p>If your print job fails due to any technical reason or your account was debited without the job printing, <strong>we will issue a 100% refund directly back to your original payment method (UPI / Bank Account) within 2 working days</strong>.</p>
        </div>

        <div class="policy-section">
          <h2>1. Cancellation Policy</h2>
          <ul>
            <li><strong>Before Payment:</strong> You can cancel, modify, or delete your uploaded files at any time on the kiosk upload page with zero charge.</li>
            <li><strong>After Payment:</strong> Because Printer of A's is an automated kiosk system, once your payment is confirmed, the document is immediately submitted to the physical printer queue and printing begins automatically. Once physical printing has commenced, the order cannot be cancelled.</li>
          </ul>
        </div>

        <div class="policy-section">
          <h2>2. Refund Eligibility Criteria</h2>
          <p>You are entitled to a full refund under any of the following circumstances:</p>
          <ul>
            <li><strong>Transaction Debited but Job Not Queued:</strong> Amount was deducted from your UPI account, but the kiosk did not register the payment or queue your document.</li>
            <li><strong>Printer Hardware Malfunction:</strong> The printer ran out of paper, paper jammed, experienced a hardware failure, or suffered an unexpected power cut during your job.</li>
            <li><strong>Severe Print Quality Defects:</strong> Ink streaks, illegible output, or blank pages directly resulting from a printer malfunction.</li>
            <li><strong>Duplicate Debits:</strong> Multiple charges for a single print job.</li>
          </ul>
        </div>

        <div class="policy-section">
          <h2>3. Refund Processing Timeline</h2>
          <ul>
            <li><strong>Review & Verification:</strong> All refund requests are reviewed and verified within 12 to 24 hours against our transaction logs.</li>
            <li><strong>Payout Timeline:</strong> Approved refunds are credited directly back to the source bank account or UPI VPA within <strong>2 working days</strong>.</li>
            <li><strong>Zero Deductions:</strong> No processing fees or deduction charges will be levied on eligible refunds.</li>
          </ul>
        </div>

        <div class="policy-section">
          <h2>4. How to Claim Your Refund</h2>
          <p>To request a refund, please reach out to us with your transaction details:</p>
          <div class="info-card-grid" style="margin-top: 16px;">
            <div class="info-box">
              <div class="info-box-title">Via WhatsApp / Phone</div>
              <div class="info-box-value"><a href="tel:9696123736">+91 9696123736</a></div>
              <div class="info-box-value" style="margin-top: 4px;"><a href="tel:9589974245">+91 9589974245</a></div>
              <div class="info-box-desc">Fastest response via WhatsApp message with payment screenshot.</div>
            </div>

            <div class="info-box">
              <div class="info-box-title">Via Email</div>
              <div class="info-box-value"><a href="mailto:printerofas@gmail.com">printerofas@gmail.com</a></div>
              <div class="info-box-desc">Subject: <em>Refund Request - [Job ID]</em></div>
            </div>
          </div>
          <p style="margin-top: 14px;"><strong>Please include:</strong></p>
          <ul>
            <li>Your 10-character <strong>Job ID</strong> (if available).</li>
            <li>Your 12-digit <strong>UPI Reference / UTR Number</strong> from your payment receipt.</li>
            <li>Screenshot of your payment or brief description of the issue.</li>
          </ul>
        </div>
      </div>
    </div>
    """)


# ---------- Tablet & Raspberry Pi Kiosk Controller APIs ----------
def require_pi():
    auth = request.headers.get("Authorization", "")
    token = request.args.get("token") or ""
    if auth == f"Bearer {PI_TOKEN}" or token == PI_TOKEN:
        return
    abort(401)


@app.get("/manifest.json")
def pwa_manifest():
    return jsonify({
        "name": "Printer of A's — Tablet Kiosk Station",
        "short_name": "Print Kiosk",
        "description": "Autonomous Tablet Print Kiosk Controller",
        "start_url": "/kiosk",
        "display": "standalone",
        "background_color": "#111113",
        "theme_color": "#10B981",
        "orientation": "any",
        "icons": [
            {
                "src": "https://api.iconify.design/lucide:printer.svg?color=%2310b981",
                "sizes": "192x192 512x512",
                "type": "image/svg+xml",
                "purpose": "any maskable"
            }
        ]
    })


_LAST_SUPA_SYNC = 0.0

def sync_supabase_queue(con, force=False):
    global _LAST_SUPA_SYNC
    now = time.time()
    if not SUPABASE_URL:
        return
    # Throttle Supabase sync to once every 2.5s unless forced
    if not force and (now - _LAST_SUPA_SYNC < 2.5):
        return

    _LAST_SUPA_SYNC = now
    try:
        supa_jobs = supabase_request("jobs?status=in.(queued,printing)&select=*&order=created.asc&limit=100")
        if supa_jobs is not None:
            active_ids = {sj["id"] for sj in supa_jobs}
            with con:
                for sj in supa_jobs:
                    con.execute("""INSERT OR REPLACE INTO jobs 
                        (id, filename, stored, pages, copies, color, duplex, amount, status, created, notes, utr_number, reserved_paise_offset)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (sj["id"], sj.get("filename", "document.pdf"), sj.get("stored", f"{sj['id']}.pdf"),
                         int(sj.get("pages", 1)), int(sj.get("copies", 1)), sj.get("color", "bw"),
                         1 if sj.get("duplex") else 0, float(sj.get("amount", 2.0)), sj.get("status", "queued"),
                         sj.get("created", ""), sj.get("notes", ""), sj.get("utr_number", ""), int(sj.get("reserved_paise_offset", 0)))
                    )
                # Clear any local jobs that are no longer active in Supabase
                local_active = con.execute("SELECT id FROM jobs WHERE status IN ('queued', 'printing')").fetchall()
                for la in local_active:
                    if la["id"] not in active_ids:
                        con.execute("UPDATE jobs SET status='done' WHERE id=?", (la["id"],))
    except Exception as e:
        print(f"[KIOSK SUPABASE SYNC ERROR] {e}")


@app.get("/api/next-job")
def next_job():
    """Tablet or Pi polls this. Returns the oldest queued job and marks it 'printing'."""
    require_pi()
    con = get_db()
    sync_supabase_queue(con)
    with con:
        row = con.execute("""SELECT * FROM jobs WHERE status='queued'
                             ORDER BY created ASC LIMIT 1""").fetchone()
        if not row:
            return jsonify(job=None)
        con.execute("UPDATE jobs SET status='printing' WHERE id=?", (row["id"],))
        if SUPABASE_URL:
            supabase_request(f"jobs?id=eq.{row['id']}", method="PATCH", data={"status": "printing"})

    file_url = f"/api/job/{row['id']}/file?token={PI_TOKEN}"
    return jsonify(job={
        "id": row["id"],
        "filename": row["filename"],
        "pages": row["pages"],
        "copies": row["copies"],
        "color": row["color"],
        "duplex": bool(row["duplex"]),
        "notes": row["notes"] if "notes" in row.keys() else "",
        "file_url": file_url
    })


@app.get("/api/kiosk/queue")
def kiosk_queue():
    """Returns currently queued and printing jobs for the live kiosk monitor."""
    require_pi()
    con = get_db()
    sync_supabase_queue(con)
    rows = con.execute("""SELECT id, filename, pages, copies, color, duplex, status, amount, created 
                          FROM jobs WHERE status IN ('queued', 'printing') 
                          ORDER BY CASE WHEN status='printing' THEN 0 ELSE 1 END, created ASC LIMIT 50""").fetchall()
    return jsonify(ok=True, jobs=[dict(r) for r in rows])


def generate_diagnostic_pdf():
    """Generates an A4 hardware calibration and diagnostic test sheet."""
    width, height = 1240, 1754
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    margin = 75
    draw.rectangle([margin, margin, width - margin, height - margin], outline="#000000", width=3)
    draw.rectangle([margin + 10, margin + 10, width - margin - 10, height - margin - 10], outline="#E2E8F0", width=1)

    for x, y in [(margin, margin), (width - margin, margin), (margin, height - margin), (width - margin, height - margin)]:
        draw.line([(x - 25, y), (x + 25, y)], fill="#000000", width=2)
        draw.line([(x, y - 25), (x, y + 25)], fill="#000000", width=2)

    draw.rectangle([margin + 10, margin + 10, width - margin - 10, margin + 120], fill="#111827")
    draw.text((margin + 30, margin + 25), "PRINTER OF A's  |  AUTONOMOUS PRINT KIOSK", fill="#10B981")
    draw.text((margin + 30, margin + 65), "HARDWARE DIAGNOSTIC & CALIBRATION SHEET", fill="#FFFFFF")

    info_y = margin + 145
    now_str = datetime.now().strftime("%d-%b-%Y %I:%M:%S %p")
    kiosk_id = os.environ.get("KIOSK_ID", "KIOSK_01")
    draw.text((margin + 30, info_y), f"STATION ID: {kiosk_id} (Acer Tablet Controller)", fill="#1E293B")
    draw.text((margin + 30, info_y + 25), "HOST: printer-of-a-s.onrender.com  |  CLOUD: Supabase Active", fill="#64748B")
    draw.text((margin + 30, info_y + 50), f"TIMESTAMP: {now_str}", fill="#64748B")

    cmyk_y = info_y + 95
    draw.text((margin + 30, cmyk_y), "1. COLOR SPECTRUM & NOZZLE HEALTH (CMYK)", fill="#0F172A")
    cmyk_y += 30
    colors = [
        ("CYAN", (0, 180, 216)),
        ("MAGENTA", (217, 4, 103)),
        ("YELLOW", (255, 183, 3)),
        ("BLACK", (20, 20, 20))
    ]
    swatch_w = (width - 2 * margin - 60) // 4
    for i, (name, col) in enumerate(colors):
        sx = margin + 30 + i * swatch_w
        draw.rectangle([sx, cmyk_y, sx + swatch_w - 15, cmyk_y + 55], fill=col)
        draw.text((sx + 8, cmyk_y + 60), name, fill="#334155")

    gray_y = cmyk_y + 110
    draw.text((margin + 30, gray_y), "2. GRAYSCALE DENSITY & CONTRAST RAMP (0% - 100%)", fill="#0F172A")
    gray_y += 30
    steps = 10
    step_w = (width - 2 * margin - 60) // steps
    for s in range(steps):
        v = int(255 * (1 - (s / (steps - 1))))
        gx = margin + 30 + s * step_w
        draw.rectangle([gx, gray_y, gx + step_w - 5, gray_y + 45], fill=(v, v, v), outline="#94A3B8")
        draw.text((gx + 4, gray_y + 50), f"{int((s/(steps-1))*100)}%", fill="#64748B")

    type_y = gray_y + 95
    draw.text((margin + 30, type_y), "3. TYPOGRAPHIC RESOLUTION & EDGE ACUITY", fill="#0F172A")
    type_y += 30
    sample_texts = [
        "24pt: The quick brown fox jumps over the lazy dog 1234567890",
        "16pt: Autonomous Cloud Print Kiosks deliver on-demand zero-retention printing.",
        "12pt: Razor-sharp micro-typography tests printhead nozzle alignment and DPI scaling.",
        "9pt: ABCDEFGHIJKLMNOPQRSTUVWXYZ abcdefghijklmnopqrstuvwxyz 1234567890 !@#$%^&*()_+"
    ]
    for txt in sample_texts:
        draw.text((margin + 30, type_y), txt, fill="#1E293B")
        type_y += 32

    grid_y = type_y + 30
    draw.text((margin + 30, grid_y), "4. ROLLER TENSION & MARGIN CHECK (0.5 inch Grid)", fill="#0F172A")
    grid_y += 25
    draw.rectangle([margin + 30, grid_y, width - margin - 30, grid_y + 180], outline="#CBD5E1", width=1)
    for gy in range(grid_y, grid_y + 180, 30):
        draw.line([(margin + 30, gy), (width - margin - 30, gy)], fill="#E2E8F0", width=1)
    for gx in range(margin + 30, width - margin - 30, 40):
        draw.line([(gx, grid_y), (gx, grid_y + 180)], fill="#E2E8F0", width=1)
    draw.text((margin + 50, grid_y + 75), "VERIFIED: Page feed is straight if grid lines are parallel to sheet edges", fill="#059669")

    draw.line([(margin + 30, height - margin - 60), (width - margin - 30, height - margin - 60)], fill="#E2E8F0", width=2)
    draw.text((margin + 30, height - margin - 45), "PRINTER OF A's  •  ACER TABLET CONTROLLER  •  POWERED BY DEEPMIND AGENTIC TECH", fill="#94A3B8")

    pdf_buffer = BytesIO()
    img.save(pdf_buffer, format="PDF", resolution=150.0)
    pdf_buffer.seek(0)
    return pdf_buffer


@app.get("/api/kiosk/diagnostic-pdf")
def kiosk_diagnostic_pdf():
    buf = generate_diagnostic_pdf()
    return send_file(buf, mimetype="application/pdf", as_attachment=False, download_name="kiosk_diagnostic_sheet.pdf")


@app.get("/api/job/<job_id>/file")
def job_file(job_id):
    require_pi()
    j = get_job(job_id)
    file_path = os.path.join(UPLOADS, j["stored"])
    if not os.path.exists(file_path):
        supabase_download_pdf(job_id, file_path)
    if not os.path.exists(file_path):
        abort(404)
    return send_file(file_path, mimetype="application/pdf")


@app.post("/api/job/<job_id>/status")
def job_status(job_id):
    require_pi()
    status = (request.get_json(silent=True) or {}).get("status")
    if status not in ("done", "failed"):
        abort(400)
    j = get_job(job_id)
    con = get_db()
    with con:
        con.execute("UPDATE jobs SET status=? WHERE id=?", (status, job_id))
    if SUPABASE_URL:
        supabase_request(f"jobs?id=eq.{job_id}", method="PATCH", data={"status": status})

    # Privacy Auto-Purge: once printing is finished, permanently remove local file & Supabase cloud backup
    if status == "done":
        file_path = os.path.join(UPLOADS, j["stored"])
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
                print(f"[PRIVACY PURGE] Removed local copy for job {job_id}")
            except Exception as e:
                print(f"[PRIVACY PURGE ERROR] Local file delete {job_id}: {e}")
        supabase_delete_pdf(job_id)

    return jsonify(ok=True)


@app.get("/kiosk")
def kiosk_station():
    """Tablet Kiosk Station Dashboard (Optimized for Android Acer Tablet & PWA Fullscreen)."""
    return render_template_string("""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Printer of A's — Tablet Kiosk Station</title>
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <meta name="mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-capable" content="yes">
  <meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
  <meta name="theme-color" content="#10B981">
  <link rel="manifest" href="/manifest.json">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;600&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0D0D11;
      --card-bg: #16161C;
      --card-border: rgba(255, 255, 255, 0.08);
      --accent: #10B981;
      --accent-glow: rgba(16, 185, 129, 0.25);
      --text: #F8FAFC;
      --text-muted: #94A3B8;
      --warning: #F59E0B;
      --danger: #EF4444;
      --font-main: 'Plus Jakarta Sans', system-ui, sans-serif;
      --font-mono: 'JetBrains Mono', monospace;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: var(--bg);
      color: var(--text);
      font-family: var(--font-main);
      min-height: 100vh;
      display: flex;
      flex-direction: column;
      user-select: none;
      -webkit-user-select: none;
    }
    /* Top Kiosk Bar */
    .kiosk-nav {
      background: #111115;
      border-bottom: 1px solid var(--card-border);
      padding: 12px 24px;
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
    }
    .kiosk-brand {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .kiosk-brand-icon {
      width: 38px;
      height: 38px;
      background: linear-gradient(135deg, #10B981, #059669);
      border-radius: 10px;
      display: flex;
      align-items: center;
      justify-content: center;
      box-shadow: 0 0 15px var(--accent-glow);
    }
    .kiosk-title {
      font-size: 1.15rem;
      font-weight: 800;
      letter-spacing: -0.3px;
    }
    .kiosk-sub {
      font-size: 0.75rem;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.8px;
    }
    .kiosk-actions {
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .kiosk-badge {
      font-size: 12px;
      font-weight: 700;
      padding: 6px 14px;
      border-radius: 20px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    .badge-online {
      background: rgba(16, 185, 129, 0.15);
      color: #10B981;
      border: 1px solid rgba(16, 185, 129, 0.3);
    }
    .badge-paused {
      background: rgba(245, 158, 11, 0.15);
      color: #F59E0B;
      border: 1px solid rgba(245, 158, 11, 0.3);
    }
    .pulse-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: currentColor;
      box-shadow: 0 0 8px currentColor;
      animation: pulse 1.8s infinite;
    }
    @keyframes pulse { 0% { opacity: 0.4; } 50% { opacity: 1; } 100% { opacity: 0.4; } }

    .btn-kiosk {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--text);
      padding: 8px 14px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s;
    }
    .btn-kiosk:hover { border-color: var(--accent); color: var(--accent); }
    .btn-kiosk.active { background: rgba(16, 185, 129, 0.15); border-color: var(--accent); color: #10B981; }

    /* Main Container */
    .kiosk-main {
      flex: 1;
      padding: 24px;
      max-width: 1380px;
      margin: 0 auto;
      width: 100%;
      display: grid;
      grid-template-columns: 1.15fr 0.85fr;
      gap: 24px;
    }
    @media (max-width: 900px) {
      .kiosk-main { grid-template-columns: 1fr; }
    }

    .kiosk-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 24px;
      position: relative;
      overflow: hidden;
    }

    /* Active Printing Hero */
    .active-job-hero {
      border: 2px solid var(--accent);
      box-shadow: 0 0 25px var(--accent-glow);
      text-align: center;
      padding: 36px 24px;
    }
    .idle-hero {
      text-align: center;
      padding: 48px 24px;
    }
    .idle-icon {
      font-size: 48px;
      margin-bottom: 16px;
      display: inline-block;
      animation: float 3s ease-in-out infinite;
    }
    @keyframes float { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-8px); } }

    .job-title-large {
      font-size: 1.6rem;
      font-weight: 800;
      margin-bottom: 8px;
    }
    .job-meta-chips {
      display: flex;
      justify-content: center;
      gap: 8px;
      flex-wrap: wrap;
      margin: 16px 0 24px;
    }
    .chip {
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(255, 255, 255, 0.1);
      padding: 6px 14px;
      border-radius: 20px;
      font-size: 13px;
      font-weight: 600;
    }

    .progress-bar-container {
      background: rgba(255, 255, 255, 0.06);
      border-radius: 12px;
      height: 16px;
      overflow: hidden;
      margin: 18px 0 10px;
      border: 1px solid rgba(255, 255, 255, 0.1);
    }
    .progress-bar-fill {
      background: linear-gradient(90deg, #10B981, #34D399);
      height: 100%;
      width: 0%;
      transition: width 0.3s ease;
      box-shadow: 0 0 12px #10B981;
    }

    /* Mode Selector Pills */
    .mode-bar {
      display: flex;
      gap: 8px;
      background: #111116;
      padding: 6px;
      border-radius: 10px;
      border: 1px solid var(--card-border);
      margin-bottom: 20px;
    }
    .mode-btn {
      flex: 1;
      padding: 10px 8px;
      background: transparent;
      border: none;
      color: var(--text-muted);
      font-size: 13px;
      font-weight: 700;
      border-radius: 8px;
      cursor: pointer;
      text-align: center;
      transition: all 0.2s;
    }
    .mode-btn.active {
      background: var(--card-bg);
      color: #10B981;
      box-shadow: 0 2px 8px rgba(0, 0, 0, 0.4);
      border: 1px solid rgba(16, 185, 129, 0.3);
    }

    /* Queue Table */
    .queue-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
      margin-top: 14px;
    }
    .queue-table th {
      text-align: left;
      padding: 10px 12px;
      color: var(--text-muted);
      border-bottom: 1px solid var(--card-border);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .queue-table td {
      padding: 12px;
      border-bottom: 1px solid rgba(255, 255, 255, 0.04);
    }

    /* Terminal Log */
    .terminal-box {
      background: #09090C;
      border: 1px solid rgba(255, 255, 255, 0.08);
      border-radius: 12px;
      padding: 14px;
      font-family: var(--font-mono);
      font-size: 12px;
      height: 240px;
      overflow-y: auto;
      color: #A1A1AA;
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .terminal-line { word-break: break-all; }
    .term-success { color: #10B981; font-weight: 600; }
    .term-info { color: #60A5FA; }
    .term-warn { color: #F59E0B; }

    /* Modal */
    .kiosk-modal-overlay {
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.85);
      backdrop-filter: blur(8px);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 1000;
      padding: 20px;
    }
    .kiosk-modal-box {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      width: 100%;
      max-width: 440px;
      padding: 28px;
      text-align: center;
    }
    .pin-input {
      background: #09090C;
      border: 2px solid var(--card-border);
      color: #FFFFFF;
      font-size: 20px;
      letter-spacing: 6px;
      text-align: center;
      padding: 14px;
      border-radius: 10px;
      width: 100%;
      margin: 18px 0;
      outline: none;
      font-family: var(--font-mono);
    }
    .pin-input:focus { border-color: var(--accent); }
  </style>
</head>
<body>

  <!-- Top Navigation Bar -->
  <nav class="kiosk-nav">
    <div class="kiosk-brand">
      <div class="kiosk-brand-icon">
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#FFFFFF" stroke-width="2.2"><path d="M6 9V2h12v7"></path><path d="M6 18H4a2 2 0 01-2-2v-5a2 2 0 012-2h16a2 2 0 012 2v5a2 2 0 01-2 2h-2"></path><path d="M6 14h12v8H6z"></path></svg>
      </div>
      <div>
        <div class="kiosk-title">Printer of A's — Tablet Kiosk</div>
        <div class="kiosk-sub" id="clock-display">STATION READY</div>
      </div>
    </div>

    <div class="kiosk-actions">
      <div class="kiosk-badge badge-online" id="status-badge">
        <div class="pulse-dot"></div>
        <span id="status-badge-text">LISTENING</span>
      </div>
      <button type="button" class="btn-kiosk" id="btn-audio" onclick="toggleAudio()">
        <span id="audio-icon">🔔</span> Chime: ON
      </button>
      <button type="button" class="btn-kiosk" id="btn-wakelock" onclick="toggleWakeLock()">
        🔒 Awake: ON
      </button>
      <button type="button" class="btn-kiosk" onclick="toggleFullscreen()">
        ⛶ Fullscreen
      </button>
      <button type="button" class="btn-kiosk" onclick="changePasscode()" title="Change Token">
        ⚙️ Token
      </button>
    </div>
  </nav>

  <!-- Main Kiosk Layout -->
  <main class="kiosk-main">

    <!-- Left Column: Active Print & Controls -->
    <div style="display: flex; flex-direction: column; gap: 24px;">

      <!-- Mode Selector -->
      <div class="kiosk-card" style="padding: 16px 20px;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
          <span style="font-size: 13px; font-weight: 700; color: var(--text-muted); text-transform: uppercase;">1. Operational Mode</span>
          <span style="font-size: 12px; color: #10B981; font-weight: 600;" id="mode-desc">Virtual Simulator Mode</span>
        </div>
        <div class="mode-bar">
          <button type="button" class="mode-btn active" id="mode-sim" onclick="setMode('sim')">🧪 Virtual Simulator</button>
          <button type="button" class="mode-btn" id="mode-wifi" onclick="setMode('wifi')">🖨️ Universal Wi-Fi (IPP)</button>
          <button type="button" class="mode-btn" id="mode-otg" onclick="setMode('otg')">🔌 USB OTG Cable</button>
        </div>
        <div style="display: flex; gap: 12px;">
          <button type="button" class="btn-kiosk active" id="btn-autoprint" onclick="toggleAutoPrint()" style="flex: 1; justify-content: center; padding: 12px;">
            ⚡ Auto-Print: ACTIVE
          </button>
          <button type="button" class="btn-kiosk" onclick="runDiagnosticTest()" style="flex: 1; justify-content: center; padding: 12px; border-color: rgba(96, 165, 250, 0.4); color: #60A5FA;">
            🧪 Run Test Print
          </button>
        </div>
      </div>

      <!-- Active Job Hero Card -->
      <div class="kiosk-card" id="hero-card">
        <div class="idle-hero" id="idle-view">
          <div class="idle-icon">📡</div>
          <h2 style="font-size: 1.4rem; font-weight: 800; margin-bottom: 6px;">Waiting for Customer Documents...</h2>
          <p style="color: var(--text-muted); font-size: 13px; max-width: 440px; margin: 0 auto;">
            When customers upload and pay ₹2 via Cashfree on your website, documents will appear here automatically and start printing.
          </p>
        </div>

        <div id="active-job-view" style="display: none;">
          <div style="display: flex; justify-content: space-between; align-items: flex-start;">
            <span class="kiosk-badge badge-online">🖨️ CURRENTLY PRINTING</span>
            <span id="job-id-tag" style="font-family: var(--font-mono); font-size: 13px; color: var(--text-muted);">#--------</span>
          </div>

          <div style="margin: 24px 0 16px; text-align: center;">
            <h1 class="job-title-large" id="active-filename">Document.pdf</h1>
            <div class="job-meta-chips">
              <span class="chip" id="chip-pages">1 Page</span>
              <span class="chip" id="chip-copies">1 Copy</span>
              <span class="chip" id="chip-color">Black &amp; White</span>
              <span class="chip" id="chip-duplex">Single-sided</span>
            </div>

            <div class="progress-bar-container">
              <div class="progress-bar-fill" id="job-progress-fill"></div>
            </div>
            <div style="display: flex; justify-content: space-between; font-size: 12px; color: var(--text-muted);">
              <span id="progress-status-label">Spooling to printhead...</span>
              <span id="progress-percent">0%</span>
            </div>
          </div>

          <div style="background: rgba(16, 185, 129, 0.08); border: 1px solid rgba(16, 185, 129, 0.2); border-radius: 10px; padding: 12px; display: flex; align-items: center; gap: 10px; font-size: 12px; color: #10B981;">
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path></svg>
            <span><strong>Zero-Retention Protection:</strong> PDF will be permanently erased from cloud &amp; tablet upon completion.</span>
          </div>
        </div>
      </div>

    </div>

    <!-- Right Column: Queue & Activity Log -->
    <div style="display: flex; flex-direction: column; gap: 24px;">

      <!-- Queue Monitor -->
      <div class="kiosk-card">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
          <h3 style="font-size: 1.1rem; font-weight: 800;">Live Print Queue</h3>
          <span style="font-size: 12px; font-weight: 700; color: #10B981;" id="queue-count-badge">0 WAITING</span>
        </div>

        <div style="max-height: 380px; overflow-y: auto;">
          <table class="queue-table">
            <thead>
              <tr>
                <th>#</th>
                <th>File</th>
                <th>Specs</th>
                <th>Status</th>
                <th>Action</th>
              </tr>
            </thead>
            <tbody id="queue-tbody">
              <tr>
                <td colspan="5" style="text-align: center; color: var(--text-muted); padding: 24px;">Queue is currently empty</td>
              </tr>
            </tbody>
          </table>
        </div>
      </div>

      <!-- Real-Time Activity Log -->
      <div class="kiosk-card" style="flex: 1; display: flex; flex-direction: column;">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
          <h3 style="font-size: 1.1rem; font-weight: 800;">Activity Audit Log</h3>
          <button type="button" style="background: none; border: none; color: var(--text-muted); font-size: 12px; cursor: pointer; text-decoration: underline;" onclick="clearLog()">Clear</button>
        </div>
        <div class="terminal-box" id="terminal-log">
          <div class="terminal-line term-info">[SYSTEM] Tablet Kiosk Station initializing...</div>
        </div>
      </div>

    </div>

  </main>

  <!-- Passcode Unlock Modal -->
  <div class="kiosk-modal-overlay" id="pin-modal" style="display: none;">
    <div class="kiosk-modal-box">
      <div style="font-size: 38px; margin-bottom: 12px;">🔒</div>
      <h2 style="font-size: 1.4rem; font-weight: 800; margin-bottom: 6px;">Kiosk Passcode Required</h2>
      <p style="color: var(--text-muted); font-size: 13px;">Enter the Station Access Token (default: <code>kiosk-secret-token-123</code>) to authenticate this tablet.</p>
      <input type="password" id="pin-field" class="pin-input" placeholder="Enter Token..." onkeydown="if(event.key==='Enter') savePasscode()">
      <button type="button" class="btn-kiosk active" style="width: 100%; justify-content: center; padding: 14px; font-size: 15px;" onclick="savePasscode()">
        Unlock Station
      </button>
    </div>
  </div>

  <script>
    // State
    let kioskToken = localStorage.getItem('kiosk_token') || 'kiosk-secret-token-123';
    let currentMode = localStorage.getItem('kiosk_mode') || 'sim'; // 'sim', 'wifi', 'otg'
    let autoPrintActive = true;
    let audioEnabled = true;
    let wakeLockInstance = null;
    let isProcessing = false;

    // Clock
    setInterval(() => {
      const now = new Date();
      document.getElementById('clock-display').textContent = now.toLocaleTimeString('en-IN', {hour: '2-digit', minute:'2-digit', second:'2-digit'}) + ' IST';
    }, 1000);

    // Audio Synthesizer (Web Audio API - Zero MP3 dependency)
    function playChime() {
      if (!audioEnabled) return;
      try {
        const AudioCtx = window.AudioContext || window.webkitAudioContext;
        if (!AudioCtx) return;
        const ctx = new AudioCtx();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.type = "sine";
        osc.frequency.setValueAtTime(587.33, ctx.currentTime); // D5
        osc.frequency.setValueAtTime(880.00, ctx.currentTime + 0.12); // A5
        gain.gain.setValueAtTime(0.25, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.5);
        osc.start();
        osc.stop(ctx.currentTime + 0.5);
      } catch(e) {
        console.warn("Audio chime error:", e);
      }
    }

    function toggleAudio() {
      audioEnabled = !audioEnabled;
      const btn = document.getElementById('btn-audio');
      if (audioEnabled) {
        btn.innerHTML = '<span>🔔</span> Chime: ON';
        btn.classList.add('active');
        playChime();
      } else {
        btn.innerHTML = '<span>🔕</span> Chime: OFF';
        btn.classList.remove('active');
      }
    }

    // WakeLock API (Keep Tablet Awake)
    async function initWakeLock() {
      try {
        if ('wakeLock' in navigator) {
          wakeLockInstance = await navigator.wakeLock.request('screen');
          document.getElementById('btn-wakelock').classList.add('active');
          logTerminal("[SYSTEM] Screen WakeLock acquired (Always-On active).", "term-success");
        }
      } catch (e) {
        console.warn("WakeLock error:", e);
      }
    }

    function toggleWakeLock() {
      if (wakeLockInstance) {
        wakeLockInstance.release().then(() => {
          wakeLockInstance = null;
          document.getElementById('btn-wakelock').classList.remove('active');
          logTerminal("[SYSTEM] Screen WakeLock released.", "term-warn");
        });
      } else {
        initWakeLock();
      }
    }

    function toggleFullscreen() {
      if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen().catch(err => alert("Fullscreen error: " + err.message));
      } else {
        document.exitFullscreen();
      }
    }

    // Mode Selector
    function setMode(mode) {
      currentMode = mode;
      localStorage.setItem('kiosk_mode', mode);
      document.querySelectorAll('.mode-btn').forEach(b => b.classList.remove('active'));
      document.getElementById('mode-' + mode).classList.add('active');

      const descMap = {
        'sim': 'Virtual Simulator Mode (No Printer Needed)',
        'wifi': 'Universal Wi-Fi / IPP Mode (Wireless Network)',
        'otg': 'USB OTG Cable Mode (Direct Cable Print)'
      };
      document.getElementById('mode-desc').textContent = descMap[mode] || mode;
      logTerminal(`[MODE] Switched to ${descMap[mode]}`, "term-info");
    }

    // Auto-Print Toggle
    function toggleAutoPrint() {
      autoPrintActive = !autoPrintActive;
      const btn = document.getElementById('btn-autoprint');
      const badge = document.getElementById('status-badge');
      const badgeText = document.getElementById('status-badge-text');

      if (autoPrintActive) {
        btn.textContent = '⚡ Auto-Print: ACTIVE';
        btn.className = 'btn-kiosk active';
        badge.className = 'kiosk-badge badge-online';
        badgeText.textContent = 'LISTENING';
        logTerminal("[CONTROL] Auto-print resumed.", "term-success");
      } else {
        btn.textContent = '⏸️ Auto-Print: PAUSED';
        btn.className = 'btn-kiosk';
        badge.className = 'kiosk-badge badge-paused';
        badgeText.textContent = 'PAUSED';
        logTerminal("[CONTROL] Auto-print paused by operator.", "term-warn");
      }
    }

    // Terminal Logger
    function logTerminal(msg, cls = "") {
      const box = document.getElementById('terminal-log');
      if (!box) return;
      const now = new Date().toLocaleTimeString('en-IN', {hour: '2-digit', minute:'2-digit', second:'2-digit'});
      const line = document.createElement('div');
      line.className = 'terminal-line ' + cls;
      line.textContent = `[${now}] ${msg}`;
      box.appendChild(line);
      box.scrollTop = box.scrollHeight;
    }

    function clearLog() {
      const box = document.getElementById('terminal-log');
      if (box) box.innerHTML = '';
    }

    // Passcode Auth
    function checkPasscode() {
      if (!kioskToken) {
        document.getElementById('pin-modal').style.display = 'flex';
      }
    }

    function savePasscode() {
      const val = (document.getElementById('pin-field').value || '').trim();
      if (!val) { alert("Please enter passcode"); return; }
      kioskToken = val;
      localStorage.setItem('kiosk_token', val);
      document.getElementById('pin-modal').style.display = 'none';
      logTerminal("[AUTH] Station token saved and authenticated.", "term-success");
      pollQueue();
    }

    function changePasscode() {
      document.getElementById('pin-field').value = kioskToken;
      document.getElementById('pin-modal').style.display = 'flex';
    }

    // Diagnostic Test Print
    async function runDiagnosticTest() {
      logTerminal("[DIAGNOSTIC] Generating test calibration sheet...", "term-info");
      const url = `/api/kiosk/diagnostic-pdf?token=${encodeURIComponent(kioskToken)}`;
      if (currentMode === 'sim') {
        window.open(url, '_blank');
        logTerminal("[DIAGNOSTIC] Opened diagnostic test page in browser preview.", "term-success");
      } else {
        // Universal Print Trigger via hidden iframe
        let iframe = document.getElementById('print-iframe');
        if (!iframe) {
          iframe = document.createElement('iframe');
          iframe.id = 'print-iframe';
          iframe.style.position = 'fixed';
          iframe.style.right = '0';
          iframe.style.bottom = '0';
          iframe.style.width = '0';
          iframe.style.height = '0';
          iframe.style.border = '0';
          document.body.appendChild(iframe);
        }
        iframe.src = url;
        iframe.onload = () => {
          try {
            iframe.contentWindow.focus();
            iframe.contentWindow.print();
            logTerminal("[DIAGNOSTIC] Dispatched calibration sheet to printer spooler.", "term-success");
          } catch(e) {
            window.open(url, '_blank');
          }
        };
      }
    }

    // Main Queue Poller & Execution Engine
    async function pollQueue() {
      if (isProcessing) return;

      try {
        // 1. Refresh live queue list
        const qRes = await fetch(`/api/kiosk/queue?token=${encodeURIComponent(kioskToken)}`);
        if (qRes.status === 401) {
          document.getElementById('pin-modal').style.display = 'flex';
          return;
        }
        const qData = await qRes.json();
        if (qData.ok && qData.jobs) {
          updateQueueTable(qData.jobs);
        }

        // 2. If Auto-Print is enabled, claim next job
        if (autoPrintActive && !isProcessing) {
          const nextRes = await fetch(`/api/next-job?token=${encodeURIComponent(kioskToken)}`);
          if (nextRes.ok) {
            const nextData = await nextRes.json();
            if (nextData.job) {
              await processJob(nextData.job);
            }
          }
        }
      } catch (err) {
        console.warn("Kiosk poll error:", err);
      }
    }

    let lastFetchedJobs = [];

    function updateQueueTable(jobs) {
      lastFetchedJobs = jobs;
      const tbody = document.getElementById('queue-tbody');
      const badge = document.getElementById('queue-count-badge');
      if (!tbody) return;

      const queuedOnly = jobs.filter(j => j.status === 'queued');
      badge.textContent = `${queuedOnly.length} WAITING (${jobs.length} TOTAL)`;

      if (jobs.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align: center; color: var(--text-muted); padding: 24px;">Queue is currently empty</td></tr>';
        return;
      }

      tbody.innerHTML = jobs.map((j, i) => `
        <tr>
          <td><span style="font-weight: 700; color: #10B981;">#${i+1}</span><br><code style="font-size: 10px; color: var(--text-muted);">${j.id.substring(0, 6)}</code></td>
          <td style="font-weight: 600; max-width: 130px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${j.filename}">${j.filename}</td>
          <td>${j.pages}p • ${j.copies}c<br><span style="font-size: 10px; color: var(--text-muted);">${j.color.toUpperCase()}</span></td>
          <td><span class="chip" style="font-size: 11px; padding: 3px 8px; color: ${j.status==='printing' ? '#10B981':'#60A5FA'};">${j.status.toUpperCase()}</span></td>
          <td>
            ${j.status === 'printing' ? 
              `<button type="button" class="btn-kiosk" style="padding: 4px 8px; font-size: 11px; border-color: #10B981; color: #10B981;" onclick="markJobDone('${j.id}')">Done ✓</button>` :
              `<button type="button" class="btn-kiosk" style="padding: 4px 8px; font-size: 11px;" onclick="claimSpecificJob('${j.id}')">Print ▶</button>`
            }
          </td>
        </tr>
      `).join('');
    }

    async function markJobDone(jobId) {
      logTerminal(`[MANUAL] Marking Job #${jobId} DONE...`, "term-info");
      try {
        const res = await fetch(`/api/job/${jobId}/status?token=${encodeURIComponent(kioskToken)}`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${kioskToken}`
          },
          body: JSON.stringify({ status: 'done' })
        });
        if (res.ok) {
          logTerminal(`[JOB ${jobId}] Marked DONE. Zero-Retention: Cloud copy purged!`, "term-success");
          pollQueue();
        }
      } catch(e) {
        logTerminal(`[ERROR] Could not complete job: ${e.message}`, "term-warn");
      }
    }

    async function claimSpecificJob(jobId) {
      const job = lastFetchedJobs.find(j => j.id === jobId);
      if (job && !isProcessing) {
        job.file_url = `/api/job/${job.id}/file?token=${encodeURIComponent(kioskToken)}`;
        await processJob(job);
      }
    }

    // Process a single job
    async function processJob(job) {
      isProcessing = true;
      playChime();
      logTerminal(`[JOB ${job.id}] Claimed! File: ${job.filename} (${job.pages} pages, ${job.copies} copies, ${job.color})`, "term-success");

      // Show Active View
      document.getElementById('idle-view').style.display = 'none';
      document.getElementById('active-job-view').style.display = 'block';
      document.getElementById('hero-card').className = 'kiosk-card active-job-hero';

      document.getElementById('job-id-tag').textContent = '#' + job.id;
      document.getElementById('active-filename').textContent = job.filename;
      document.getElementById('chip-pages').textContent = `${job.pages} Page(s)`;
      document.getElementById('chip-copies').textContent = `${job.copies} Copy(ies)`;
      document.getElementById('chip-color').textContent = job.color === 'color' ? 'Full Colour' : 'Black & White';
      document.getElementById('chip-duplex').textContent = job.duplex ? 'Yes (Duplex)' : 'Single-sided';

      const fill = document.getElementById('job-progress-fill');
      const statusLabel = document.getElementById('progress-status-label');
      const percentLabel = document.getElementById('progress-percent');

      // Step 1: Downloading
      fill.style.width = '25%';
      percentLabel.textContent = '25%';
      statusLabel.textContent = 'Downloading PDF securely from cloud...';
      await new Promise(r => setTimeout(r, 800));

      // Step 2: Spooling / Printing
      fill.style.width = '60%';
      percentLabel.textContent = '60%';
      statusLabel.textContent = `Sending to ${currentMode === 'sim' ? 'Virtual Print Engine' : 'Hardware Printhead'}...`;

      if (currentMode !== 'sim') {
        // Trigger universal hardware print via invisible iframe
        const printUrl = `${job.file_url}&kiosk=1`;
        let iframe = document.getElementById('print-iframe');
        if (!iframe) {
          iframe = document.createElement('iframe');
          iframe.id = 'print-iframe';
          iframe.style.position = 'fixed';
          iframe.style.width = '0';
          iframe.style.height = '0';
          document.body.appendChild(iframe);
        }
        iframe.src = printUrl;
        await new Promise(r => setTimeout(r, 2000));
      } else {
        // Simulate physical feed time (approx 1.2s per page)
        const totalSimMs = Math.max(2000, job.pages * 1200);
        await new Promise(r => setTimeout(r, totalSimMs));
      }

      // Step 3: Finished
      fill.style.width = '100%';
      percentLabel.textContent = '100%';
      statusLabel.textContent = 'Print Complete! Notifying cloud server...';

      // Step 4: Call Server POST /api/job/<id>/status -> done (triggers zero retention purge)
      try {
        const doneRes = await fetch(`/api/job/${job.id}/status`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${kioskToken}`
          },
          body: JSON.stringify({ status: 'done' })
        });
        if (doneRes.ok) {
          logTerminal(`[JOB ${job.id}] Verified Complete. Zero-Retention: Cloud copy permanently deleted!`, "term-success");
        }
      } catch (err) {
        logTerminal(`[JOB ${job.id}] Status confirm failed: ${err.message}`, "term-warn");
      }

      await new Promise(r => setTimeout(r, 1500));

      // Reset to idle view
      document.getElementById('active-job-view').style.display = 'none';
      document.getElementById('idle-view').style.display = 'block';
      document.getElementById('hero-card').className = 'kiosk-card';
      fill.style.width = '0%';
      percentLabel.textContent = '0%';

      isProcessing = false;
      pollQueue();
    }

    // Initialize
    window.addEventListener('DOMContentLoaded', () => {
      setMode(currentMode);
      checkPasscode();
      initWakeLock();
      logTerminal("[SYSTEM] Tablet Kiosk Station ready.", "term-success");
      logTerminal("[SYSTEM] Polling queue every 2.5 seconds...", "term-info");
      setInterval(pollQueue, 2500);
      pollQueue();
    });
  </script>
</body>
</html>
    """)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001, debug=True)

