import os
import re
import gc
import json
import time
import base64
import html
import struct
import shutil
import zipfile
import urllib.parse
import subprocess
import threading
import datetime
import unicodedata
import http.server
import socketserver
import uuid
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
import urllib3
from PIL import Image
import pillow_heif

# ----------------- ĐẢM BẢO CÓ THƯ VIỆN CRYPTO GIẢI MÃ MEGA -----------------
try:
    from Crypto.Cipher import AES
    from Crypto.Util import Counter
except ImportError:
    try:
        import sys
        subprocess.run([sys.executable, "-m", "pip", "install", "pycryptodome"], check=True)
        from Crypto.Cipher import AES
        from Crypto.Util import Counter
    except Exception:
        pass

# ----------------- HÀM GHI NHẬT KÝ THỜI GIAN THỰC -----------------
def log(msg: str):
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

# ----------------- 1. MỞ SERVER HTTP DUY TRÌ RENDER -----------------
class RenderHealthHandler(http.server.BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "2")
        self.end_headers()

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass

def run_dummy_web_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        socketserver.TCPServer.allow_reuse_address = True
        with socketserver.TCPServer(("", port), RenderHealthHandler) as httpd:
            log(f"🌐 Đã mở cổng HTTP {port} để duy trì Render...")
            httpd.serve_forever()
    except Exception as e:
        log(f"Lưu ý server HTTP: {e}")

threading.Thread(target=run_dummy_web_server, daemon=True).start()

# ----------------- NẠP VÀ CẤP QUYỀN FFMPEG ĐA TẦNG -----------------
FFMPEG_EXEC = "ffmpeg"
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
    FFMPEG_EXEC = shutil.which("ffmpeg") or "ffmpeg"
except Exception:
    pass

try:
    import imageio_ffmpeg
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    if exe and os.path.exists(exe):
        FFMPEG_EXEC = exe
except Exception:
    pass

try:
    actual_path = shutil.which(FFMPEG_EXEC) if FFMPEG_EXEC == "ffmpeg" else FFMPEG_EXEC
    if actual_path and os.path.exists(actual_path):
        os.chmod(actual_path, 0o755)
except Exception:
    pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
pillow_heif.register_heif_opener()

# ----------------- 2. CẤU HÌNH BIẾN MÔI TRƯỜNG & BANNER ẢNH -----------------
APP_ID = os.environ.get("APP_ID", "").strip() or os.environ.get("LARK_APP_ID", "").strip()
APP_SECRET = os.environ.get("APP_SECRET", "").strip() or os.environ.get("LARK_APP_SECRET", "").strip()
TARGET_DOMAIN = getattr(lark, "LARK_DOMAIN", "https://open.larksuite.com")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "temp_files")
HISTORY_FILE = os.path.join(BASE_DIR, "history_proof.json")

BANNER_CARD1_KEY = "img_v3_0215r_61dad065-35d7-45ba-a33d-6ab073a717ah"      # Thẻ 1: Loading
BANNER_ERROR_KEY = "img_v3_0215r_6e344d17-b29f-4de6-a147-177aa11fa62h"      # Thẻ Báo Lỗi
BANNER_COMPLETED_KEY = "img_v3_0215r_124a0bca-2990-426a-8cf2-c72aeadb7fdh"  # Thẻ 2: Hoàn Tất

PROCESSED_MESSAGES = set()

global_session = requests.Session()
retries = Retry(total=3, backoff_factor=0.5, status_forcelist=[500, 502, 503, 504])
adapter = HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=retries)
global_session.mount("https://", adapter)
global_session.mount("http://", adapter)

client = lark.Client.builder() \
    .app_id(APP_ID) \
    .app_secret(APP_SECRET) \
    .domain(TARGET_DOMAIN) \
    .log_level(lark.LogLevel.INFO) \
    .build()

# ----------------- HÀM TẠO BANNER THU NHỎ KHOẢNG 1/2 VÀ CĂN CHÍNH GIỮA -----------------
def build_half_size_banner(img_key: str, alt_text: str = "Thông báo") -> list:
    if not img_key or img_key.startswith("DÁN_"):
        return []
    return [
        {
            "tag": "column_set",
            "flex_mode": "none",
            "background_style": "default",
            "columns": [
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 3,
                    "elements": [{"tag": "markdown", "content": " "}]
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 2,
                    "elements": [
                        {
                            "tag": "img",
                            "img_key": img_key,
                            "alt": {"tag": "plain_text", "content": alt_text},
                            "mode": "fit_horizontal"
                        }
                    ]
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 3,
                    "elements": [{"tag": "markdown", "content": " "}]
                }
            ]
        }
    ]

# ----------------- HÀM TẠO CALLOUT BOX CĂN CHÍNH GIỮA TUYỆT ĐỐI -----------------
def build_highlight_box(content_md: str, bg_style: str = "carmine") -> dict:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": bg_style,
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [{"tag": "markdown", "content": " "}]
            },
            {
                "tag": "column",
                "width": "auto",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": content_md
                    }
                ]
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [{"tag": "markdown", "content": " "}]
            }
        ]
    }

def build_centered_tag(tag_md: str) -> dict:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "default",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [{"tag": "markdown", "content": " "}]
            },
            {
                "tag": "column",
                "width": "auto",
                "elements": [
                    {
                        "tag": "markdown",
                        "content": tag_md
                    }
                ]
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "elements": [{"tag": "markdown", "content": " "}]
            }
        ]
    }

# 🌟 FOOTER: MƯA BÊN TRÁI - TAG SỐ LẦN ĐẾM (ĐỎ HỒNG CARMINE) Ở GÓC PHẢI
def build_footer_element(repeat_tag_str: str = "") -> dict:
    columns = [
        {
            "tag": "column",
            "width": "auto",
            "elements": [
                {
                    "tag": "markdown",
                    "content": "🌧️ <text_tag color='wathet'>**ʜồɪ ᴄʜɪềᴜ, ʜồɪ ᴄʜɪềᴜ...ᴛʀờɪ ᴍưᴀ...**</text_tag> 🌧️"
                }
            ]
        },
        {
            "tag": "column",
            "width": "weighted",
            "weight": 1,
            "elements": [{"tag": "markdown", "content": " "}]
        }
    ]
    if repeat_tag_str:
        columns.append({
            "tag": "column",
            "width": "auto",
            "elements": [
                {
                    "tag": "markdown",
                    "content": repeat_tag_str
                }
            ]
        })

    return {
        "tag": "column_set",
        "flex_mode": "none",
        "background_style": "default",
        "columns": columns
    }

# ----------------- 3. QUẢN LÝ LỊCH SỬ & ĐẾM TẦN SUẤT -----------------
def load_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_history(history: dict):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log(f"Lỗi lưu lịch sử: {e}")

def get_current_request_count(ticket_id: str) -> int:
    history = load_history()
    return history.get(ticket_id, {}).get("count", 0) + 1

def record_successful_request(ticket_id: str, count: int):
    history = load_history()
    history[ticket_id] = {
        "count": count,
        "last_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    save_history(history)

def format_size(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
    elif size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.2f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.2f} KB"
    return f"{size_bytes} B"

def sanitize_filename(filename: str) -> str:
    nfkd = unicodedata.normalize('NFKD', filename)
    ascii_name = re.sub(r'[^\w\s.-]', '', nfkd.encode('ASCII', 'ignore').decode('ASCII'))
    clean = re.sub(r'\s+', '_', ascii_name).strip()
    return clean or "proof_file"

def get_tenant_access_token() -> str:
    try:
        url = f"{TARGET_DOMAIN}/open-apis/auth/v3/tenant_access_token/internal"
        res = global_session.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=10)
        if res.status_code == 200:
            return res.json().get("tenant_access_token", "")
    except Exception as e:
        log(f"Lỗi lấy access token: {e}")
    return ""

def reply_thread_card(message_id: str, card_content: dict):
    try:
        body = ReplyMessageRequestBody.builder() \
            .content(json.dumps(card_content)) \
            .msg_type("interactive") \
            .reply_in_thread(True) \
            .build()
        req = ReplyMessageRequest.builder() \
            .message_id(message_id) \
            .request_body(body) \
            .build()
        resp = client.im.v1.message.reply(req)
        if not resp.success():
            log(f"❌ Lỗi gửi Card: Code={resp.code} | Msg={resp.msg}")
    except Exception as e:
        log(f"Lỗi reply thread card: {e}")

# ----------------- HÀM NÉN VIDEO NHẸ TẢI CPU & TIẾT KIỆM RAM -----------------
def compress_video_to_safe_mp4(file_path: str, original_name: str = "") -> str:
    try:
        if not os.path.exists(file_path) or os.path.getsize(file_path) < 1000:
            return file_path

        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        if size_mb <= 32.0:
            return file_path

        dir_name = os.path.dirname(file_path)
        base_name = os.path.splitext(original_name or os.path.basename(file_path))[0]
        base_name = re.sub(r'^(?:conv_[a-f0-9]{6}_|opt_[a-f0-9]{6}_|tmp_[a-zA-Z0-9]+_)', '', base_name)
        clean_base = sanitize_filename(base_name)
        if clean_base.lower().endswith(".mp4"):
            clean_base = clean_base[:-4]

        temp_out = os.path.join(dir_name, f"tmp_{uuid.uuid4().hex[:6]}_{clean_base}.mp4")
        final_mp4 = os.path.join(dir_name, f"{clean_base}.mp4")

        log(f"⚙️ Bắt đầu nén nhẹ tải cho video: {clean_base} ({size_mb:.2f} MB)...")

        vf = "scale=640:360:force_original_aspect_ratio=decrease,scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=15"
        cmd = [
            FFMPEG_EXEC, "-y", "-nostdin",
            "-threads", "1",
            "-i", file_path,
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast",
            "-b:v", "350k", "-maxrate", "450k", "-bufsize", "700k",
            "-pix_fmt", "yuv420p",
            "-an",
            "-movflags", "+faststart",
            temp_out
        ]

        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=240)
        gc.collect()

        if proc.returncode == 0 and os.path.exists(temp_out) and os.path.getsize(temp_out) > 10000:
            out_mb = os.path.getsize(temp_out) / (1024 * 1024)
            log(f"✨ Nén video hoàn tất: {clean_base}.mp4 ({out_mb:.2f} MB)")
            try:
                if os.path.exists(final_mp4) and final_mp4 != temp_out:
                    os.remove(final_mp4)
                if os.path.exists(file_path) and file_path != temp_out and file_path != final_mp4:
                    os.remove(file_path)
            except Exception:
                pass
            os.rename(temp_out, final_mp4)
            return final_mp4
        else:
            if os.path.exists(temp_out):
                os.remove(temp_out)
    except Exception as e:
        log(f"Lỗi nén video: {e}")

    return file_path

# ----------------- TỰ ĐỘNG NHẬN DIỆN ĐUÔI TỆP THEO MAGIC BYTES -----------------
def auto_detect_and_fix_extension(file_path: str) -> str:
    try:
        dir_name = os.path.dirname(file_path)
        base_name = os.path.basename(file_path)
        name, ext = os.path.splitext(base_name)
        ext = ext.lower()

        if os.path.exists(file_path) and os.path.getsize(file_path) > 32:
            with open(file_path, "rb") as f:
                header = f.read(512)

            if header.startswith(b"%PDF"):
                if ext != ".pdf":
                    new_p = os.path.join(dir_name, f"{name}.pdf")
                    os.rename(file_path, new_p)
                    return new_p
                return file_path

            if header.startswith(b"\xff\xd8\xff") or header.startswith(b"\x89PNG") or header.startswith(b"GIF8") or (header.startswith(b"RIFF") and b"WEBP" in header[8:16]):
                new_ext = ".jpg" if header.startswith(b"\xff\xd8\xff") else (".png" if header.startswith(b"\x89PNG") else ".webp")
                if ext not in [".jpg", ".jpeg", ".png", ".webp"]:
                    new_p = os.path.join(dir_name, f"{name}{new_ext}")
                    os.rename(file_path, new_p)
                    return new_p
                return file_path

            is_video = (
                header.startswith(b"\x1a\x45\xdf\xa3") or
                header.startswith(b"FLV") or
                (header.startswith(b"RIFF") and b"AVI " in header[8:16]) or
                any(box in header[:128] for box in [b"ftyp", b"moov", b"mdat", b"wide", b"free", b"qt  "])
            )

            if is_video or (not ext and os.path.getsize(file_path) > 300 * 1024):
                if ext not in [".mp4", ".mov", ".avi", ".mkv", ".webm"]:
                    new_p = os.path.join(dir_name, f"{name}.mp4")
                    os.rename(file_path, new_p)
                    return new_p
                return file_path

    except Exception as e:
        log(f"Lỗi kiểm tra tệp: {e}")
    return file_path

# ----------------- CƠ CHẾ THẢ VÀ GỠ REACTION -----------------
def add_reaction_to_message(message_id: str, emoji_type: str) -> str:
    token = get_tenant_access_token()
    if not token or not message_id:
        return ""

    url = f"{TARGET_DOMAIN}/open-apis/im/v1/messages/{message_id}/reactions"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8"
    }
    data = {"reaction_type": {"emoji_type": emoji_type}}
    try:
        res = global_session.post(url, headers=headers, json=data, timeout=10)
        if res.status_code == 200:
            body = res.json()
            return body.get("data", {}).get("reaction_id", "")
    except Exception as e:
        log(f"Lỗi gọi API reaction: {e}")
    return ""

def remove_reaction_from_message(message_id: str, reaction_id: str):
    token = get_tenant_access_token()
    if not token or not message_id or not reaction_id:
        return

    url = f"{TARGET_DOMAIN}/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        global_session.delete(url, headers=headers, timeout=10)
    except Exception as e:
        log(f"Lỗi gỡ reaction: {e}")

# ----------------- TẢI LÊN FILE LARK -----------------
def upload_lark_file(file_path: str, file_type: str = "stream") -> str:
    if not os.path.exists(file_path):
        return ""
    
    if os.path.getsize(file_path) / (1024 * 1024) > 32.0:
        file_path = compress_video_to_safe_mp4(file_path)

    token = get_tenant_access_token()
    if not token:
        return ""

    url = f"{TARGET_DOMAIN}/open-apis/im/v1/files"
    raw_name = os.path.basename(file_path)
    safe_name = sanitize_filename(raw_name)

    headers = {"Authorization": f"Bearer {token}"}
    data = {"file_type": file_type, "file_name": safe_name}

    try:
        with open(file_path, "rb") as f:
            files = {"file": (safe_name, f)}
            res = global_session.post(url, headers=headers, data=data, files=files, timeout=240)
            if res.status_code == 200:
                body = res.json()
                if body.get("code") == 0:
                    return body["data"]["file_key"]
    except Exception as e:
        log(f"Lỗi upload: {e}")
    return ""

def upload_and_send_batch_proofs(message_id: str, final_files: list, urls: list = None) -> int:
    actual_sent_count = 0

    for f in final_files:
        file_path = f["path"]
        file_ext = f["ext"]
        file_name = f["name"]

        try:
            # 1. Hình ảnh
            if file_ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".jfif", ".svg", ".tiff"]:
                with open(file_path, "rb") as img_f:
                    create_req = CreateImageRequest.builder() \
                        .request_body(CreateImageRequestBody.builder().image_type("message").image(img_f).build()) \
                        .build()
                    create_resp = client.im.v1.image.create(create_req)
                    if create_resp and create_resp.success():
                        img_k = create_resp.data.image_key
                        body = ReplyMessageRequestBody.builder().content(json.dumps({"image_key": img_k})).msg_type("image").reply_in_thread(True).build()
                        resp = client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(body).build())
                        if resp and resp.success():
                            actual_sent_count += 1
                            log(f"✅ Đã gửi ảnh: {file_name}")

            # 2. Tệp PDF
            elif file_ext == ".pdf":
                file_key = upload_lark_file(file_path, "pdf") or upload_lark_file(file_path, "stream")
                if file_key:
                    file_body = ReplyMessageRequestBody.builder().content(json.dumps({"file_key": file_key})).msg_type("file").reply_in_thread(True).build()
                    resp = client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(file_body).build())
                    if resp and resp.success():
                        actual_sent_count += 1
                        log(f"📄 Đã bung tệp PDF vào thread: {file_name}")

            # 3. Tệp Video (bỏ qua nén nếu <= 32MB để bung siêu tốc)
            elif file_ext in [".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".webm", ".m4v", ".3gp"]:
                send_path = f["path"]
                if os.path.getsize(send_path) / (1024 * 1024) > 32.0:
                    send_path = compress_video_to_safe_mp4(file_path, original_name=file_name)
                
                if not os.path.exists(send_path) or os.path.getsize(send_path) < 10000:
                    continue

                file_key = upload_lark_file(send_path, "stream") or upload_lark_file(send_path, "mp4")
                if file_key:
                    file_body = ReplyMessageRequestBody.builder() \
                        .content(json.dumps({"file_key": file_key})) \
                        .msg_type("file") \
                        .reply_in_thread(True) \
                        .build()
                    resp = client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(file_body).build())
                    if resp and resp.success():
                        actual_sent_count += 1
                        log(f"📥 Đã bung video phát trực tiếp: {os.path.basename(send_path)}")

            # 4. Tệp khác
            else:
                file_key = upload_lark_file(file_path, "stream")
                if file_key:
                    file_body = ReplyMessageRequestBody.builder().content(json.dumps({"file_key": file_key})).msg_type("file").reply_in_thread(True).build()
                    resp = client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(file_body).build())
                    if resp and resp.success():
                        actual_sent_count += 1
                        log(f"📎 Đã bung tệp: {file_name}")
        except Exception as e:
            log(f"Lỗi gửi media: {e}")

        gc.collect()

    return actual_sent_count

# ----------------- HÀM TIỆN ÍCH MÃ HÓA CHO MEGA.NZ -----------------
def b64_url_decode(s: str) -> bytes:
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.b64decode(s)

def get_aes_ecb_decrypter(key: bytes):
    try:
        from Crypto.Cipher import AES
        return lambda data: AES.new(key, AES.MODE_ECB).decrypt(data)
    except Exception:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        c = Cipher(algorithms.AES(key), modes.ECB(), backend=default_backend()).decryptor()
        return lambda data: c.update(data) + c.finalize()

def get_aes_cbc_decrypter(key: bytes, iv: bytes):
    try:
        from Crypto.Cipher import AES
        return lambda data: AES.new(key, AES.MODE_CBC, iv=iv).decrypt(data)
    except Exception:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.backends import default_backend
        c = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend()).decryptor()
        return lambda data: c.update(data) + c.finalize()

def get_aes_ctr_decrypter(key: bytes, iv: bytes):
    try:
        from Crypto.Cipher import AES
        from Crypto.Util import Counter
        ctr = Counter.new(128, initial_value=int.from_bytes(iv, 'big'))
        cipher = AES.new(key, AES.MODE_CTR, counter=ctr)
        return lambda data: cipher.decrypt(data)
    except Exception:
        try:
            from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
            from cryptography.hazmat.backends import default_backend
            c = Cipher(algorithms.AES(key), modes.CTR(iv), backend=default_backend()).decryptor()
            return lambda data: c.update(data)
        except Exception as e:
            log(f"Lỗi khởi tạo AES CTR: {e}")
            return lambda data: data

# ----------------- 4. GIẢI MÃ VÀ TẢI TỆP MEGA.NZ -----------------
def download_mega(url: str, target_dir: str) -> bool:
    clean_u = url.strip()
    log(f"☁️ Đang xử lý liên kết MEGA: {clean_u}")
    
    # 1. Nhận diện Thư mục MEGA
    m_folder = re.search(r'mega\.nz/(?:folder/|#F!)([a-zA-Z0-9_-]+)[#!]([a-zA-Z0-9_-]+)', clean_u)
    if m_folder:
        folder_id = m_folder.group(1)
        folder_key = m_folder.group(2)
        try:
            master_key = b64_url_decode(folder_key)
            if len(master_key) < 16:
                return False
            master_key = master_key[:16]

            api_url = f"https://g.api.mega.co.nz/cs?id=0&n={folder_id}"
            res = global_session.post(api_url, json=[{"a": "f", "c": 1, "r": 1, "ca": 1}], timeout=25)
            res_data = res.json()
            if not isinstance(res_data, list) or len(res_data) == 0:
                return False

            nodes = res_data[0].get("f", [])
            downloaded_count = 0

            for node in nodes:
                if node.get("t") == 0 and "k" in node:
                    k_str = node["k"]
                    enc_key_b64 = k_str.split(":")[-1]
                    enc_key = b64_url_decode(enc_key_b64)

                    if len(enc_key) >= 32:
                        dec_ecb = get_aes_ecb_decrypter(master_key)
                        raw_key = dec_ecb(enc_key[:32])

                        k_ints = struct.unpack(">8I", raw_key)
                        real_key = struct.pack(">4I", k_ints[0] ^ k_ints[4], k_ints[1] ^ k_ints[5], k_ints[2] ^ k_ints[6], k_ints[3] ^ k_ints[7])
                        iv = struct.pack(">4I", k_ints[4], k_ints[5], 0, 0)

                        file_name = f"mega_{node['h']}.mp4"
                        if node.get("a"):
                            try:
                                enc_a = b64_url_decode(node["a"])
                                dec_cbc = get_aes_cbc_decrypter(real_key, b"\x00" * 16)
                                dec_a = dec_cbc(enc_a)
                                if b"MEGA{" in dec_a:
                                    start_p = dec_a.find(b"MEGA{") + 4
                                    raw_j = dec_a[start_p:].split(b"\x00")[0].decode("utf-8", errors="ignore")
                                    r_idx = raw_j.rfind("}")
                                    if r_idx != -1:
                                        raw_j = raw_j[:r_idx+1]
                                    attr_d = json.loads(raw_j)
                                    file_name = attr_d.get("n", file_name)
                            except Exception as e:
                                log(f"Lỗi giải mã tên tệp MEGA: {e}")

                        file_name = sanitize_filename(file_name)
                        if not any(file_name.lower().endswith(x) for x in [".mp4", ".mov", ".avi", ".mkv", ".webm", ".jpg", ".png", ".pdf"]):
                            file_name += ".mp4"

                        dl_req = [{"a": "g", "g": 1, "n": node["h"]}]
                        dl_res = global_session.post(f"https://g.api.mega.co.nz/cs?id=1&n={folder_id}", json=dl_req, timeout=25)
                        dl_data = dl_res.json()
                        if isinstance(dl_data, list) and len(dl_data) > 0 and isinstance(dl_data[0], dict) and "g" in dl_data[0]:
                            dl_url = dl_data[0]["g"]
                            log(f"📥 Đang tải và giải mã video MEGA: {file_name} ({format_size(node.get('s', 0))})...")
                            stream_res = global_session.get(dl_url, stream=True, timeout=(30, 480))
                            save_path = os.path.join(target_dir, file_name)
                            dec_ctr = get_aes_ctr_decrypter(real_key, iv)

                            with open(save_path, "wb") as f_out:
                                for chunk in stream_res.iter_content(chunk_size=1024 * 1024):
                                    if chunk:
                                        f_out.write(dec_ctr(chunk))

                            if os.path.exists(save_path) and os.path.getsize(save_path) > 1000:
                                log(f"✅ Đã tải và giải mã thành công: {file_name} ({format_size(os.path.getsize(save_path))})")
                                downloaded_count += 1

            return downloaded_count > 0
        except Exception as e:
            log(f"Lỗi xử lý thư mục MEGA: {e}")

    # 2. Nhận diện Tệp MEGA đơn lẻ
    m_file = re.search(r'mega\.nz/(?:file/|#!)([a-zA-Z0-9_-]+)[#!]([a-zA-Z0-9_-]+)', clean_u)
    if m_file:
        file_id = m_file.group(1)
        file_key = m_file.group(2)
        try:
            enc_key = b64_url_decode(file_key)
            if len(enc_key) >= 32:
                k_ints = struct.unpack(">8I", enc_key[:32])
                real_key = struct.pack(">4I", k_ints[0] ^ k_ints[4], k_ints[1] ^ k_ints[5], k_ints[2] ^ k_ints[6], k_ints[3] ^ k_ints[7])
                iv = struct.pack(">4I", k_ints[4], k_ints[5], 0, 0)

                dl_res = global_session.post("https://g.api.mega.co.nz/cs?id=0", json=[{"a": "g", "g": 1, "p": file_id}], timeout=25)
                dl_data = dl_res.json()
                if isinstance(dl_data, list) and len(dl_data) > 0 and isinstance(dl_data[0], dict) and "g" in dl_data[0]:
                    dl_url = dl_data[0]["g"]
                    file_name = f"mega_{file_id}.mp4"
                    if dl_data[0].get("at"):
                        try:
                            enc_a = b64_url_decode(dl_data[0]["at"])
                            dec_cbc = get_aes_cbc_decrypter(real_key, b"\x00" * 16)
                            dec_a = dec_cbc(enc_a)
                            if b"MEGA{" in dec_a:
                                start_p = dec_a.find(b"MEGA{") + 4
                                raw_j = dec_a[start_p:].split(b"\x00")[0].decode("utf-8", errors="ignore")
                                r_idx = raw_j.rfind("}")
                                if r_idx != -1:
                                    raw_j = raw_j[:r_idx+1]
                                attr_d = json.loads(raw_j)
                                file_name = attr_d.get("n", file_name)
                        except Exception:
                            pass

                    file_name = sanitize_filename(file_name)
                    if not any(file_name.lower().endswith(x) for x in [".mp4", ".mov", ".avi", ".mkv", ".webm", ".jpg", ".png", ".pdf"]):
                        file_name += ".mp4"

                    log(f"📥 Đang tải và giải mã video đơn lẻ MEGA: {file_name}...")
                    stream_res = global_session.get(dl_url, stream=True, timeout=(30, 480))
                    save_path = os.path.join(target_dir, file_name)
                    dec_ctr = get_aes_ctr_decrypter(real_key, iv)

                    with open(save_path, "wb") as f_out:
                        for chunk in stream_res.iter_content(chunk_size=1024 * 1024):
                            if chunk:
                                f_out.write(dec_ctr(chunk))

                    if os.path.exists(save_path) and os.path.getsize(save_path) > 1000:
                        log(f"✅ Đã tải và giải mã thành công: {file_name} ({format_size(os.path.getsize(save_path))})")
                        return True
        except Exception as e:
            log(f"Lỗi xử lý file đơn lẻ MEGA: {e}")

    return False

# ----------------- 5. GIẢI MÃ VÀ TẢI TỆP ONEDRIVE (MICROSOFT BADGER API) -----------------
def get_badger_token() -> str:
    """Lấy Badger OAuth Token ẩn từ Microsoft để giải mã các link OneDrive SPO mới"""
    try:
        url = "https://api-badgerp.svc.ms/v1.0/token"
        headers = {
            "AppId": "1141147648",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        }
        res = global_session.post(url, headers=headers, json={"appId": "5cbed6ac-a083-4e14-b191-b4ba07653de2"}, timeout=10)
        if res.status_code == 200:
            token = res.json().get("token", "")
            if token:
                log(f"🔑 Lấy thành công Badger Token OneDrive: {token[:25]}...")
                return token
    except Exception as e:
        log(f"Lỗi lấy Badger Token: {e}")
    return ""

def _save_stream_to_file(res, target_dir: str, default_name: str = "proof_file") -> bool:
    try:
        content_disposition = res.headers.get("Content-Disposition", "")
        extracted_name = ""
        if "filename=" in content_disposition:
            fn_match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)["\']?', content_disposition)
            if fn_match:
                try:
                    extracted_name = urllib.parse.unquote(fn_match.group(1))
                except Exception:
                    extracted_name = fn_match.group(1)

        raw_name = extracted_name or default_name
        ct = res.headers.get("Content-Type", "").lower()
        if not any(raw_name.lower().endswith(x) for x in [".mp4", ".mov", ".avi", ".mkv", ".webm", ".jpg", ".jpeg", ".png", ".pdf"]):
            if "video" in ct or "mp4" in ct or "octet-stream" in ct:
                raw_name += ".mp4"
            elif "pdf" in ct:
                raw_name += ".pdf"
            elif "image" in ct:
                raw_name += ".jpg"

        clean_name = sanitize_filename(raw_name)
        save_path = os.path.join(target_dir, clean_name)

        if os.path.exists(save_path):
            try:
                os.remove(save_path)
            except Exception:
                pass

        with open(save_path, "wb") as f:
            for chunk in res.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)

        if os.path.exists(save_path) and os.path.getsize(save_path) > 500:
            log(f"📥 Đã tải thành công: {clean_name} ({format_size(os.path.getsize(save_path))})")
            return True
        if os.path.exists(save_path):
            os.remove(save_path)
    except Exception as e:
        log(f"Lỗi lưu file: {e}")
    return False

def download_onedrive(url: str, target_dir: str) -> bool:
    clean_u = url.strip()
    log(f"☁️ Đang xử lý liên kết OneDrive: {clean_u}")

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"
    }

    dest_url = clean_u
    dest_html = ""
    try:
        r = global_session.get(clean_u, headers=headers, allow_redirects=True, timeout=15, verify=False)
        dest_url = r.url
        dest_html = r.text
    except Exception as e:
        log(f"Lưu ý chuyển hướng OneDrive: {e}")

    # BƯỚC 1: XỬ LÝ QUA MICROSOFT BADGER AUTHENTICATION API
    badger_token = get_badger_token()
    if badger_token:
        badger_headers = {
            "Authorization": f"Badger {badger_token}",
            "Prefer": "autoredeem",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        candidates_share_keys = []

        b64_full = base64.urlsafe_b64encode(clean_u.encode('utf-8')).decode('utf-8').rstrip('=')
        candidates_share_keys.append(f"u!{b64_full}")

        clean_no_q = clean_u.split('?')[0]
        b64_no_q = base64.urlsafe_b64encode(clean_no_q.encode('utf-8')).decode('utf-8').rstrip('=')
        candidates_share_keys.append(f"u!{b64_no_q}")

        m_path_token = re.search(r'/c/[a-zA-Z0-9_-]+/([a-zA-Z0-9_-]+)', clean_no_q)
        if m_path_token:
            candidates_share_keys.append(f"u!{m_path_token.group(1)}")
            candidates_share_keys.append(m_path_token.group(1))

        redeem_m = re.search(r'[?&]redeem=([^&]+)', dest_url)
        if redeem_m:
            candidates_share_keys.append(f"u!{redeem_m.group(1)}")
            candidates_share_keys.append(redeem_m.group(1))

        photos_m = re.search(r'[?&]photosData=([^&]+)', dest_url)
        if photos_m:
            try:
                dec_p = urllib.parse.unquote(photos_m.group(1))
                m_item = re.search(r'/share/([^?&]+)', dec_p)
                if m_item:
                    candidates_share_keys.append(f"u!{m_item.group(1)}")
                    candidates_share_keys.append(m_item.group(1))
            except Exception:
                pass

        for sk in candidates_share_keys:
            api_endpoints = [
                f"https://my.microsoftpersonalcontent.com/_api/v2.0/shares/{sk}/driveitem",
                f"https://api.onedrive.com/v1.0/shares/{sk}/driveitem",
                f"https://my.microsoftpersonalcontent.com/_api/v2.0/shares/{sk}/root"
            ]

            for ep in api_endpoints:
                try:
                    res_api = global_session.get(ep, headers=badger_headers, timeout=12)
                    if res_api.status_code == 200:
                        data = res_api.json()
                        dl_url = data.get("@content.downloadUrl") or data.get("@microsoft.graph.downloadUrl") or data.get("downloadUrl")
                        fname = data.get("name") or "onedrive_video.mp4"
                        if dl_url:
                            log(f"🎯 Bắt thành công luồng tải qua Badger API: {fname}")
                            res_dl = global_session.get(dl_url, headers=badger_headers, stream=True, timeout=(30, 480))
                            if res_dl.status_code in [200, 206] and "text/html" not in res_dl.headers.get("Content-Type", "").lower():
                                return _save_stream_to_file(res_dl, target_dir, fname)
                except Exception:
                    continue

        for sk in candidates_share_keys[:3]:
            for content_ep in [
                f"https://my.microsoftpersonalcontent.com/_api/v2.0/shares/{sk}/driveitem/content",
                f"https://api.onedrive.com/v1.0/shares/{sk}/root/content"
            ]:
                try:
                    res_c = global_session.get(content_ep, headers=badger_headers, stream=True, allow_redirects=True, timeout=(30, 480))
                    if res_c.status_code in [200, 206] and "text/html" not in res_c.headers.get("Content-Type", "").lower():
                        log("🎯 Tải trực tiếp thành công qua Badger Content Endpoint!")
                        return _save_stream_to_file(res_c, target_dir, "onedrive_video.mp4")
                except Exception:
                    continue

    # BƯỚC 2: CÁC PHƯƠNG ÁN DỰ PHÒNG TRUYỀN THỐNG
    try:
        sep = "&" if "?" in clean_u else "?"
        res_d1 = global_session.get(f"{clean_u}{sep}download=1", headers=headers, stream=True, allow_redirects=True, timeout=(20, 300), verify=False)
        if res_d1.status_code in [200, 206] and "text/html" not in res_d1.headers.get("Content-Type", "").lower():
            return _save_stream_to_file(res_d1, target_dir, "onedrive_video.mp4")
    except Exception:
        pass

    try:
        cid_m = re.search(r'cid=([a-fA-F0-9]+)', dest_url) or re.search(r'/c/([a-fA-F0-9]+)', clean_u)
        id_m = re.search(r'[?&]id=([^&]+)', dest_url) or re.search(r'[?&]resid=([^&]+)', dest_url)
        cid = cid_m.group(1) if cid_m else ""
        resid = urllib.parse.unquote(id_m.group(1)) if id_m else ""

        if cid and resid:
            dl_cand = f"https://onedrive.live.com/download?cid={cid}&resid={resid}"
            res_cand = global_session.get(dl_cand, headers=headers, stream=True, allow_redirects=True, timeout=(20, 300), verify=False)
            if res_cand.status_code in [200, 206] and "text/html" not in res_cand.headers.get("Content-Type", "").lower():
                return _save_stream_to_file(res_cand, target_dir, "onedrive_video.mp4")
    except Exception:
        pass

    return False

# ----------------- 🌟 DANH SÁCH CÁC TÊN MIỀN RÚT GỌN -----------------
SHORT_DOMAINS = [
    "byvn.net", "by.com.vn", "bom.so", "bit.ly", "l1nk.dev",
    "tinyurl.com", "t.ly", "shorturl.at", "cutt.ly", "is.gd", "rb.gy", "s.id"
]

def is_short_url(u: str) -> bool:
    u_low = u.lower()
    return any(d in u_low for d in SHORT_DOMAINS)

# 🌟 BẢNG ÁNH XẠ TRỰC TIẾP CHO CÁC LINK GẶP TRỤC TRẶC MẠNG
KNOWN_URL_MAPPINGS = {
    "byvn.net/wyts": "https://aidc-xspace-xform.oss-ap-southeast-1.aliyuncs.com/common/rc-upload-1789710295067-25",
    "by.com.vn/wyts": "https://aidc-xspace-xform.oss-ap-southeast-1.aliyuncs.com/common/rc-upload-1789710295067-25",
}

# ----------------- 🌟 GIẢI MÃ ĐA TẦNG CHO BYVN.NET, BOM.SO, BIT.LY -----------------
def resolve_short_url(url: str) -> str:
    cur_url = url.strip()

    # Kiểm tra bảng ánh xạ trực tiếp
    for k, v in KNOWN_URL_MAPPINGS.items():
        if k in cur_url.lower():
            log(f"🎯 Khớp link gốc từ bảng ánh xạ: {v[:80]}...")
            return v

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"
    }

    for hop in range(8):
        if not is_short_url(cur_url):
            return cur_url

        # Tầng 1: HEAD request (0 byte tải dữ liệu)
        try:
            r_head = global_session.head(cur_url, headers=headers, allow_redirects=True, timeout=8, verify=False)
            if r_head.url and not is_short_url(r_head.url):
                log(f"🔗 Bắt link đích qua HEAD redirect: {r_head.url[:80]}...")
                return r_head.url
        except Exception:
            pass

        # Tầng 2: GET stream=True theo dõi chuyển hướng mà không tải body video
        try:
            r_stream = global_session.get(cur_url, headers=headers, allow_redirects=True, stream=True, timeout=12, verify=False)
            dest = r_stream.url
            r_stream.close()
            if dest and not is_short_url(dest):
                log(f"🔗 Bắt link đích qua GET stream redirect: {dest[:80]}...")
                return dest
            cur_url = dest
        except Exception as e:
            log(f"Lưu ý stream redirect: {e}")

        # Tầng 3: Kiểm tra tiêu đề Location từng nấc
        try:
            r_no_redir = global_session.get(cur_url, headers=headers, allow_redirects=False, timeout=8, verify=False)
            if r_no_redir.status_code in [301, 302, 303, 307, 308] and "Location" in r_no_redir.headers:
                loc = r_no_redir.headers["Location"].strip()
                if not loc.startswith("http"):
                    loc = urllib.parse.urljoin(cur_url, loc)
                log(f"🔗 Bắt Location header ({r_no_redir.status_code}): {loc[:80]}...")
                cur_url = loc
                continue
        except Exception as e:
            log(f"Lưu ý kiểm tra Location: {e}")

        # Tầng 4: Quét sâu mã nguồn HTML trang trung gian
        try:
            r_html = global_session.get(cur_url, headers=headers, timeout=10, verify=False)
            html_text = r_html.text
            unescaped = html.unescape(html_text).replace(r'\/', '/').replace(r'\u002f', '/').replace(r'\u002F', '/')

            # 1. Meta refresh
            meta_m = re.search(r'<meta[^>]*?content=["\']\d+;\s*url=([^"\'>\s]+)["\']', html_text, re.IGNORECASE)
            if meta_m:
                cand = meta_m.group(1).strip()
                if not cand.startswith("http"):
                    cand = urllib.parse.urljoin(cur_url, cand)
                log(f"🔗 Tìm thấy link trong meta refresh: {cand[:80]}...")
                cur_url = cand
                continue

            # 2. JavaScript redirect
            js_m = re.search(r'(?:window\.location(?:\.href)?|location\.replace|location\.href|window\.location\.assign)\s*(?:=|\()\s*["\']([^"\']+)["\']', html_text)
            if js_m:
                cand = js_m.group(1).strip()
                if cand.startswith("http"):
                    log(f"🔗 Tìm thấy link trong JS redirect: {cand[:80]}...")
                    cur_url = cand
                    continue

            # 3. Quét mọi URL trong HTML và tìm dịch vụ file
            found_urls = re.findall(r'https?://[^\s"\'<>\\]+', unescaped)
            found_target = ""
            for cand in found_urls:
                cand_clean = cand.rstrip(';,."\')]>')
                cand_low = cand_clean.lower()
                if any(ign in cand_low for ign in [
                    "byvn.net", "by.com.vn", "bom.so", "bit.ly", "l1nk.dev",
                    "cloudflare", "facebook.com", "google.com", "googleapis.com", "gstatic.com",
                    "w3.org", "schema.org", "jsdelivr", "cdnjs", "nel.cloudflare", "twitter.com", "zalo.me"
                ]):
                    continue
                if any(k in cand_low for k in ["aliyuncs.com", "rc-upload", "fptcloud.com", "drive.google.com", "1drv.ms", "onedrive.live.com", "mega.nz", ".mp4", ".pdf", ".mov"]):
                    found_target = cand_clean
                    break
                if not found_target:
                    found_target = cand_clean

            if found_target:
                log(f"🎯 Bóc tách thành công link ẩn trong HTML: {found_target[:80]}...")
                cur_url = found_target
                continue

        except Exception as e:
            log(f"Lỗi phân tích HTML trang rút gọn: {e}")

        break

    return cur_url

# ----------------- TẢI FILE GOOGLE DRIVE VƯỢT QUA TRANG CẢNH BÁO VI-RÚT -----------------
def extract_gdrive_title(file_id: str) -> str:
    try:
        url = f"https://drive.google.com/file/d/{file_id}/view"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = global_session.get(url, headers=headers, timeout=10, verify=False)
        if r.status_code == 200:
            m = re.search(r'<title>(.*?) - Google Drive</title>', r.text, re.IGNORECASE)
            if m and not m.group(1).startswith("http"):
                return m.group(1).strip()
    except Exception:
        pass
    return ""

def download_single_gdrive_file(file_id: str, target_dir: str, preferred_name: str = "") -> bool:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "*/*"
    }
    real_title = preferred_name or extract_gdrive_title(file_id)

    try:
        direct_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download&confirm=t"
        res = global_session.get(direct_url, headers=headers, stream=True, verify=False, timeout=(30, 480))
        if res.status_code == 200 and "text/html" not in res.headers.get("Content-Type", "").lower():
            return _save_stream_to_file(res, target_dir, real_title or f"gdrive_{file_id}")
    except Exception:
        pass

    try:
        init_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        res = global_session.get(init_url, headers=headers, stream=True, verify=False, timeout=50)
        if res.status_code == 200 and "text/html" not in res.headers.get("Content-Type", "").lower():
            return _save_stream_to_file(res, target_dir, real_title or f"gdrive_{file_id}")

        html_text = res.text
        link_match = re.search(r'<a\s+[^>]*?id=["\']uc-download-link["\'][^>]*?href=["\']([^"\']+)["\']', html_text, re.IGNORECASE) or \
                     re.search(r'href=["\']((?:https://drive\.usercontent\.google\.com)?/download\?[^"\']+)["\']', html_text, re.IGNORECASE)

        if link_match:
            confirmed_url = link_match.group(1)
            if confirmed_url.startswith("/"):
                confirmed_url = "https://drive.usercontent.google.com" + confirmed_url
            res_down = global_session.get(html.unescape(confirmed_url), headers={"Referer": res.url}, stream=True, verify=False, timeout=(30, 480))
            if res_down.status_code == 200 and "text/html" not in res_down.headers.get("Content-Type", "").lower():
                return _save_stream_to_file(res_down, target_dir, real_title or f"gdrive_{file_id}")
    except Exception as e:
        pass

    try:
        import gdown
        fallback_path = os.path.join(target_dir, sanitize_filename(real_title or f"gdrive_{file_id}.mp4"))
        output = gdown.download(id=file_id, output=fallback_path, quiet=False, fuzzy=True)
        if output and os.path.exists(output) and os.path.getsize(output) > 500:
            return True
    except Exception:
        pass

    return False

def download_gdrive_folder(folder_url: str, target_dir: str) -> bool:
    folder_match = re.search(r'/folders/([a-zA-Z0-9_-]+)', folder_url)
    folder_id = folder_match.group(1) if folder_match else ""
    if not folder_id:
        m = re.search(r'([a-zA-Z0-9_-]{25,50})', folder_url)
        folder_id = m.group(1) if m else ""

    if not folder_id:
        return False

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    found_files = {}

    try:
        embed_url = f"https://drive.google.com/embeddedfolderview?id={folder_id}#list"
        r_embed = global_session.get(embed_url, headers=headers, timeout=20, verify=False)
        if r_embed.status_code == 200:
            for m in re.finditer(r'/file/d/([a-zA-Z0-9_-]{25,50})[^\'"]*[\'"][^>]*>([^<]+)<', r_embed.text):
                fid = m.group(1)
                if fid != folder_id and len(fid) >= 25:
                    found_files[fid] = html.unescape(m.group(2)).strip()

            for fid in re.findall(r'/file/d/([a-zA-Z0-9_-]{25,50})', r_embed.text):
                if fid != folder_id and fid not in found_files:
                    found_files[fid] = f"gdrive_file_{fid}"
    except Exception:
        pass

    if found_files:
        success_count = sum(1 for fid, fname in found_files.items() if download_single_gdrive_file(fid, target_dir, fname))
        return success_count > 0

    return False

# ----------------- ĐIỀU PHỐI TẢI XUỐNG CHO MỌI ĐỊNH DẠNG LINK -----------------
def download_proof(url: str, target_dir: str) -> bool:
    clean_u = url.strip()

    # 1. MEGA.NZ
    if "mega.nz" in clean_u.lower():
        return download_mega(clean_u, target_dir)

    # 2. OneDrive (1drv.ms / onedrive.live.com)
    if "1drv.ms" in clean_u.lower() or "onedrive.live.com" in clean_u.lower():
        return download_onedrive(clean_u, target_dir)

    # 🌟 Giải mã toàn diện mọi liên kết rút gọn
    if is_short_url(clean_u):
        final_url = resolve_short_url(clean_u)
    else:
        final_url = clean_u

    log(f"📥 Đang tải liên kết: {final_url}")

    if "mega.nz" in final_url.lower():
        return download_mega(final_url, target_dir)

    if "1drv.ms" in final_url.lower() or "onedrive.live.com" in final_url.lower():
        return download_onedrive(final_url, target_dir)

    # 3. Google Drive
    if any(k in final_url for k in ["drive.google.com", "drive.usercontent.google.com"]):
        if "/folders/" in final_url or "embeddedfolderview" in final_url:
            return download_gdrive_folder(final_url, target_dir)
        else:
            m = re.search(r'(?:/file/d/|/d/|id=|download\?id=)([a-zA-Z0-9_-]{25,50})', final_url)
            if m:
                return download_single_gdrive_file(m.group(1), target_dir)

    # 4. Tải trực tiếp (Alibaba Cloud OSS, FPT Cloud Tiki WMS, CDN)
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
            "Accept": "*/*"
        }
        res = global_session.get(final_url, headers=headers, stream=True, timeout=(25, 400), verify=False)
        if res.status_code in [200, 206]:
            actual_final_url = res.url or final_url
            ct = res.headers.get("Content-Type", "").lower()
            if "text/html" in ct and not any(k in actual_final_url.lower() for k in ["fptcloud", "aliyuncs", "rc-upload"]):
                return False

            raw_n = os.path.basename(urllib.parse.urlparse(actual_final_url).path) or "proof_media"
            if any(k in actual_final_url.lower() for k in ["fptcloud.com", "aliyuncs.com", "rc-upload"]) and not any(raw_n.lower().endswith(x) for x in [".mp4", ".mov", ".avi", ".mkv", ".webm", ".jpg", ".png", ".pdf"]):
                raw_n += ".mp4"
            return _save_stream_to_file(res, target_dir, raw_n)
    except Exception as e:
        log(f"Lỗi tải trực tiếp: {e}")

    return False

# ----------------- 6. BÓC TÁCH NỘI DUNG TIN NHẮN -----------------
def extract_urls_from_text(raw_text: str) -> list:
    text = html.unescape(raw_text)
    text = text.replace(r"\/", "/").replace(r"\u002f", "/").replace(r"\u002F", "/")
    
    found = re.findall(r'https?://[^\s"\'<>]+', text)
    cleaned = [u.rstrip(';>,.()[]\'"') for u in found if u.startswith("http") and len(u) > 10]
    return list(dict.fromkeys(cleaned))

def extract_message_text(message: dict) -> str:
    msg_type = message.get("message_type", "")
    content_raw = message.get("content", "{}")

    try:
        content_json = json.loads(content_raw)
    except Exception:
        return ""

    if msg_type == "text":
        return content_json.get("text", "")

    if msg_type == "post":
        extracted_pieces = []
        paragraphs = content_json.get("content", [])
        if not paragraphs and "post" in content_json:
            first_locale = next(iter(content_json["post"].values()), {})
            paragraphs = first_locale.get("content", [])

        for p in paragraphs:
            for item in p:
                tag = item.get("tag")
                if tag in ["text", "a"]:
                    extracted_pieces.append(item.get("text", "") or item.get("href", ""))
                elif tag == "at":
                    extracted_pieces.append(f"@{item.get('user_name', '') or item.get('user_id', '')}")
        return " ".join(extracted_pieces)

    return ""

# ----------------- 7. XỬ LÝ CHÍNH & PHẢN HỒI THẺ CHO TỪNG TICKET -----------------
def process_single_task(message_id: str, chat_id: str, ticket_id: str, urls: list, sender_id: str):
    log(f"📥 BẮT ĐẦU XỬ LÝ ĐƠN: {ticket_id} (Tổng link: {len(urls)})")
    clock_rx_id = add_reaction_to_message(message_id, "AlarmClock")

    try:
        req_count = get_current_request_count(ticket_id)
        task_temp_dir = os.path.join(TEMP_DIR, f"{message_id}_{ticket_id}")
        shutil.rmtree(task_temp_dir, ignore_errors=True)
        os.makedirs(task_temp_dir, exist_ok=True)

        for u in urls:
            download_proof(u, task_temp_dir)

        downloaded_paths = []
        for root, _, fs in os.walk(task_temp_dir):
            for f in fs:
                p = os.path.join(root, f)
                if os.path.getsize(p) > 500 and not os.path.basename(p).startswith("tmp_"):
                    downloaded_paths.append(p)

        final_files = []
        for raw_path in downloaded_paths:
            if os.path.exists(raw_path):
                fixed_path = auto_detect_and_fix_extension(raw_path)
                fixed_name = os.path.basename(fixed_path)
                final_files.append({
                    "name": fixed_name,
                    "path": fixed_path,
                    "size": os.path.getsize(fixed_path),
                    "ext": os.path.splitext(fixed_name)[1].lower()
                })

        # THẺ BÁO LỖI NẾU KHÔNG CÓ TỆP NÀO ĐƯỢC TẢI
        if not final_files:
            log(f"❌ Không tải được file nào cho đơn {ticket_id}")
            if clock_rx_id:
                remove_reaction_from_message(message_id, clock_rx_id)

            first_url = urls[0] if urls else ""
            error_img = build_half_size_banner(BANNER_ERROR_KEY, "Cảnh báo truy cập")
            if "sharepoint.com" in first_url:
                reply_thread_card(message_id, {
                    "elements": error_img + [
                        {"tag": "markdown", "content": f"📁 **𝗧𝗶𝗰𝗸𝗲𝘁 𝗜𝗗: {ticket_id}**\n\n<font color='orange'>⚠️ Link là **Thư mục SharePoint nội bộ**, bot không thể tải tự động do cơ chế bảo mật của Microsoft.</font>\n\n👉 [**Mở Thư mục SharePoint**]({first_url})"},
                        {"tag": "hr"},
                        build_footer_element()
                    ]
                })
            else:
                reply_thread_card(message_id, {
                    "elements": error_img + [
                        {"tag": "markdown", "content": f"<text_tag color='carmine'>🚨 Không thể tải video của 𝗧𝗶𝗰𝗸𝗲𝘁 𝗜𝗗: {ticket_id}, vui lòng kiểm tra lại quyền truy cập link!</text_tag>"},
                        {"tag": "hr"},
                        build_footer_element()
                    ]
                })
            shutil.rmtree(task_temp_dir, ignore_errors=True)
            return

        record_successful_request(ticket_id, req_count)
        total_size = sum(x["size"] for x in final_files)
        file_count = len(final_files)

        # PHÂN LOẠI FILE BẬC THANG
        video_exts = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".webm", ".m4v", ".3gp"}
        image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".jfif", ".svg", ".tiff"}
        audio_exts = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".wma", ".opus"}

        c_video = sum(1 for x in final_files if x["ext"] in video_exts)
        c_image = sum(1 for x in final_files if x["ext"] in image_exts)
        c_audio = sum(1 for x in final_files if x["ext"] in audio_exts)
        c_doc = sum(1 for x in final_files if x["ext"] not in (video_exts | image_exts | audio_exts))

        cat_hierarchy = [("🎞️", c_video), ("🖼️", c_image), ("📼", c_audio), ("📑", c_doc)]
        indent_steps = ["  ", "                ", "                               ", "                                             "]
        group_lines = [f"🗂️: {file_count} file"]
        step_idx = 0
        for icon, count in cat_hierarchy:
            if count > 0:
                indent = indent_steps[step_idx] if step_idx < len(indent_steps) else " " * (2 + step_idx * 14)
                group_lines.append(f"{indent}╰┈➤{icon} : {count} file")
                step_idx += 1

        summary_group_str = "\n".join(group_lines)
        repeat_tag = f"**<text_tag color='carmine'>📋 Lần {req_count}</text_tag>**" if req_count > 1 else "**<text_tag color='carmine'>📋 Lần 1</text_tag>**"

        # ---------------- THẺ 1: XUẤT HIỆN TỨC THÌ (ĐÃ BỎ DÒNG ĐẾM TỆP 1/1) ----------------
        file_lines = [f"         <font color='carmine'>╰┄‌•  </font>{item['name']}: <text_tag color='carmine'>[{format_size(item['size'])}]</text_tag>" for item in final_files]
        files_str = "\n".join(file_lines)

        header_block = (
            f"🎫<text_tag color='turquoise'>{ticket_id}</text_tag>\n"
            f"   ╰┄▸ 💾<text_tag color='carmine'>{format_size(total_size)}</text_tag>\n\n"
            f"{summary_group_str}\n\n"
            f"{files_str}"
        )

        card1_img_element = build_half_size_banner(BANNER_CARD1_KEY, "⌛Lᴏᴀᴅɪɴɢ...")
        card1_top_highlight = build_centered_tag("**<text_tag color='carmine'>°•*⁀➷ 𝐃𝐎𝐍'𝐓 𝐆𝐎 𝐀𝐍𝐘𝐖𝐇𝐄𝐑𝐄, 𝐁𝐄𝐂𝐀𝐔𝐒𝐄 𝐖𝐄 𝐖𝐎𝐍'𝐓 &gt;&lt; ➹*•°</text_tag>**")
        card1_bottom_highlight = build_centered_tag("**<text_tag color='red'>⌛Lᴏᴀᴅɪɴɢ...</text_tag>**")

        loading_card_payload = {
            "elements": card1_img_element + [
                card1_top_highlight,
                {"tag": "markdown", "content": header_block},
                card1_bottom_highlight,
                {"tag": "hr"},
                build_footer_element(repeat_tag)
            ]
        }
        reply_thread_card(message_id, loading_card_payload)

        # ---------------- BUNG TỆP VÀO THREAD ----------------
        actual_bung_success = upload_and_send_batch_proofs(message_id, final_files, urls)

        # ---------------- THẺ 2: HOÀN TẤT ----------------
        title_side_md = "**<text_tag color='turquoise'>・❥・Cᴏᴍᴘʟᴇᴛᴇᴅ</text_tag>**\n<text_tag color='turquoise'>-ˋˏ    𝐃𝐎𝐖𝐍𝐋𝐎𝐀𝐃 𝐏𝐑𝐎OF ˎˊ-</text_tag>"
        sender_mention = f"<at id=\"{sender_id}\"></at>" if sender_id else "chị"
        heading_md = f"<font color='carmine'>**♡ {sender_mention} ơi...</font>**\n      ╰┄▸ 🎫 *<text_tag color='carmine'>{ticket_id}</text_tag>*"
        thankyou_md = "<font color='turquoise'>   ┊ t h a n k y o u ┊\n┈┈┈┈┈┈┈┈․° ••• °․┈┈┈┈┈┈┈┈</font>"

        card2_img_element = build_half_size_banner(BANNER_COMPLETED_KEY, "・❥・Cᴏᴍᴘʟᴇᴛᴇᴅ")
        card2_top_highlight = build_centered_tag("**<text_tag color='turquoise'>·.¸¸.·♩♪♫ Gʀᴇᴀᴛ ᴛᴏ ʜᴀᴠᴇ ᴇᴠᴇʀʏᴏɴᴇ ♫♪♩·.¸¸.·</text_tag>**")
        card2_bottom_highlight = build_centered_tag("**<text_tag color='turquoise'>・❥Cᴏᴍᴘʟᴇᴛᴇᴅ</text_tag>**")

        finish_card_payload = {
            "elements": card2_img_element + [
                card2_top_highlight,
                build_centered_tag(title_side_md),
                {"tag": "markdown", "content": heading_md},
                build_centered_tag(thankyou_md),
                card2_bottom_highlight,
                {"tag": "hr"},
                build_footer_element(repeat_tag)
            ]
        }
        reply_thread_card(message_id, finish_card_payload)

        if clock_rx_id:
            remove_reaction_from_message(message_id, clock_rx_id)

        shutil.rmtree(task_temp_dir, ignore_errors=True)
        gc.collect()

    except Exception as e:
        log(f"Lỗi trong process_single_task: {e}")
        if clock_rx_id:
            remove_reaction_from_message(message_id, clock_rx_id)

def parse_and_dispatch(message_id: str, chat_id: str, text: str, sender_id: str):
    tokens = re.split(r'(\b\d{15,21}\b)', text)
    
    if len(tokens) >= 3:
        current_id = None
        current_text = ""
        for part in tokens:
            if re.match(r'^\d{15,21}$', part):
                if current_id and current_text:
                    urls = extract_urls_from_text(current_text)
                    if urls:
                        threading.Thread(target=process_single_task, args=(message_id, chat_id, current_id, urls, sender_id), daemon=True).start()
                current_id = part
                current_text = ""
            else:
                current_text += " " + part
                
        if current_id and current_text:
            urls = extract_urls_from_text(current_text)
            if urls:
                threading.Thread(target=process_single_task, args=(message_id, chat_id, current_id, urls, sender_id), daemon=True).start()
    else:
        urls = extract_urls_from_text(text)
        order_match = re.search(r"\b(\d{15,21})\b", text)
        ticket_id = order_match.group(1) if order_match else "PROOF_DATA"
        if urls:
            threading.Thread(target=process_single_task, args=(message_id, chat_id, ticket_id, urls, sender_id), daemon=True).start()

def handle_message(data: lark.im.v1.P2MessageReceiveV1) -> None:
    try:
        msg = data.event.message
        if msg.message_id in PROCESSED_MESSAGES:
            return
        PROCESSED_MESSAGES.add(msg.message_id)

        chat_id = msg.chat_id or ""
        sender_id = data.event.sender.sender_id.open_id if (data.event.sender and data.event.sender.sender_id) else ""

        msg_dict = {"message_type": msg.message_type, "content": msg.content}
        text = extract_message_text(msg_dict)

        if "http://" in text or "https://" in text:
            log(f"📩 [LARK] BẮT ĐƯỢC TIN NHẮN CHỨA LINK: {text[:60]}...")
            t = threading.Thread(target=parse_and_dispatch, args=(msg.message_id, chat_id, text, sender_id))
            t.daemon = True
            t.start()
    except Exception as e:
        log(f"Lỗi message: {e}")

# ----------------- 8. KHỞI CHẠY WEBSOCKET AUTO-RECONNECT -----------------
def start_bot():
    log("🚀 BOT LARK PROOF KHỞI ĐỘNG...")
    while True:
        try:
            builder = lark.EventDispatcherHandler.builder("", "")
            builder.register_p2_im_message_receive_v1(handle_message)
            event_handler = builder.build()

            ws_client = lark.ws.Client(
                app_id=APP_ID,
                app_secret=APP_SECRET,
                event_handler=event_handler,
                domain=TARGET_DOMAIN,
                log_level=lark.LogLevel.INFO
            )
            ws_client.start()
        except Exception as e:
            log(f"⚠️ Mất kết nối WebSocket: {e}. Kết nối lại sau 5s...")
            time.sleep(5)

if __name__ == "__main__":
    start_bot()
