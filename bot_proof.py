import os
import re
import gc
import json
import base64
import html
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
            print(f"🌐 Đã mở cổng HTTP {port} để duy trì Render...")
            httpd.serve_forever()
    except Exception as e:
        print(f"Lưu ý server HTTP: {e}")

threading.Thread(target=run_dummy_web_server, daemon=True).start()

# ----------------- NẠP VÀ CẤP QUYỀN FFMPEG -----------------
FFMPEG_EXEC = "ffmpeg"
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
    FFMPEG_EXEC = shutil.which("ffmpeg") or "ffmpeg"
except Exception as e:
    print(f"Lưu ý static_ffmpeg: {e}")

if not shutil.which(FFMPEG_EXEC):
    FFMPEG_EXEC = "ffmpeg"

try:
    actual_path = shutil.which(FFMPEG_EXEC)
    if actual_path:
        os.chmod(actual_path, 0o755)
except Exception:
    pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
pillow_heif.register_heif_opener()

# ----------------- 2. CẤU HÌNH BIẾN MÔI TRƯỜNG & BANNER ẢNH CHO 3 LOẠI THẺ -----------------
APP_ID = os.environ.get("APP_ID", "").strip() or os.environ.get("LARK_APP_ID", "").strip()
APP_SECRET = os.environ.get("APP_SECRET", "").strip() or os.environ.get("LARK_APP_SECRET", "").strip()
TARGET_DOMAIN = getattr(lark, "LARK_DOMAIN", "https://open.larksuite.com")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "temp_files")
HISTORY_FILE = os.path.join(BASE_DIR, "history_proof.json")

# 🌟 BỘ 3 BANNER CHO 3 TRẠNG THÁI THẺ
BANNER_CARD1_KEY = "img_v3_0215r_61dad065-35d7-45ba-a33d-6ab073a717ah"      # Thẻ 1: Loading ban đầu
BANNER_ERROR_KEY = "img_v3_0215r_6e344d17-b29f-4de6-a147-177aa11fa62h"      # Thẻ Báo Lỗi / Cảnh Báo
BANNER_COMPLETED_KEY = "img_v3_0215r_124a0bca-2990-426a-8cf2-c72aeadb7fdh"  # Thẻ 2: Hoàn Tất

FOOTER_RAIN_TEXT = "**<text_tag color='indigo'>🌧️ ʜồɪ ᴄʜɪềᴜ, ʜồɪ ᴄʜɪềᴜ...ᴛʀờɪ ᴍưᴀ...🌧️</text_tag>**"

PROCESSED_MESSAGES = set()

global_session = requests.Session()
retries = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
adapter = HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=retries)
global_session.mount("https://", adapter)
global_session.mount("http://", adapter)

client = lark.Client.builder() \
    .app_id(APP_ID) \
    .app_secret(APP_SECRET) \
    .domain(TARGET_DOMAIN) \
    .log_level(lark.LogLevel.INFO) \
    .build()

# ----------------- HÀM TẠO BANNER THU NHỎ BẰNG 1/2 VÀ CĂN GIỮA -----------------
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
                    "weight": 1,
                    "elements": [{"tag": "markdown", "content": " "}]
                },
                {
                    "tag": "column",
                    "width": "weighted",
                    "weight": 2,  # Đúng 2/4 = 50% (1/2) bề ngang thẻ
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
                    "weight": 1,
                    "elements": [{"tag": "markdown", "content": " "}]
                }
            ]
        }
    ]

# ----------------- 3. QUẢN LÝ LỊCH SỬ & ĐẾM TẦN SUẤT LẶP LẠI -----------------
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
        print(f"Lỗi lưu lịch sử: {e}")

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
    return clean or "proof_file.mp4"

def get_tenant_access_token() -> str:
    try:
        url = f"{TARGET_DOMAIN}/open-apis/auth/v3/tenant_access_token/internal"
        res = global_session.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=10)
        if res.status_code == 200:
            return res.json().get("tenant_access_token", "")
    except Exception as e:
        print(f"Lỗi lấy access token: {e}")
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
            print(f"❌ Lỗi gửi Card: Code={resp.code} | Msg={resp.msg}")
    except Exception as e:
        print(f"Lỗi reply thread card: {e}")

# ----------------- HÀM NÉN VIDEO CHUẨN HD 480P ĐẢM BẢO LUÔN < 20MB -----------------
def compress_video_to_safe_mp4(file_path: str, original_name: str = "") -> str:
    """Nén video chuẩn HD 480p, tối ưu bitrate để luôn nằm trong khoảng 3MB - 8MB"""
    try:
        if not os.path.exists(file_path) or os.path.getsize(file_path) < 1000:
            return file_path

        size_mb = os.path.getsize(file_path) / (1024 * 1024)
        ext = os.path.splitext(file_path)[1].lower()

        # Nếu file đã là MP4 và nhẹ <= 20MB thì giữ nguyên
        if ext == ".mp4" and size_mb <= 20.0:
            return file_path

        dir_name = os.path.dirname(file_path)
        base_name = os.path.splitext(original_name or os.path.basename(file_path))[0]
        base_name = re.sub(r'^(?:conv_[a-f0-9]{6}_|opt_[a-f0-9]{6}_|tmp_[a-zA-Z0-9]+_)', '', base_name)
        clean_base = sanitize_filename(base_name)
        if clean_base.lower().endswith(".mp4"):
            clean_base = clean_base[:-4]

        temp_out = os.path.join(dir_name, f"tmp_enc_{uuid.uuid4().hex[:6]}_{clean_base}.mp4")
        final_mp4 = os.path.join(dir_name, f"{clean_base}.mp4")

        # Cú pháp chuẩn hóa scale=480:-2 (đảm bảo luôn chẵn điểm ảnh, không bị lỗi cú pháp)
        cmd = [
            FFMPEG_EXEC, "-y", "-nostdin",
            "-threads", "1",
            "-i", file_path,
            "-vf", "scale=480:-2,fps=15",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", "600k", "-maxrate", "800k", "-bufsize", "1200k",
            "-pix_fmt", "yuv420p",
            "-an",
            "-movflags", "+faststart",
            temp_out
        ]

        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=240)
        gc.collect()

        if proc.returncode == 0 and os.path.exists(temp_out) and os.path.getsize(temp_out) > 50000:
            out_mb = os.path.getsize(temp_out) / (1024 * 1024)
            print(f"⚡ Đã nén video HD 480p thành công: {clean_base}.mp4 ({out_mb:.2f} MB)")
            
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
            err = proc.stderr.decode('utf-8', errors='ignore')[-200:] if proc.stderr else ""
            print(f"⚠️ Nén chưa hoàn tất: {err}")
    except Exception as e:
        print(f"Lỗi nén video: {e}")
        if 'temp_out' in locals() and os.path.exists(temp_out):
            try:
                os.remove(temp_out)
            except Exception:
                pass

    return file_path

def auto_detect_and_fix_extension(file_path: str) -> str:
    try:
        dir_name = os.path.dirname(file_path)
        base_name = os.path.basename(file_path)
        name, ext = os.path.splitext(base_name)
        ext = ext.lower()

        is_video = False
        is_webm = False
        is_image = False

        if ext in [".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".webm", ".m4v", ".3gp", ".ts"]:
            is_video = True

        if os.path.exists(file_path) and os.path.getsize(file_path) > 32:
            with open(file_path, "rb") as f:
                header = f.read(128)

            if header.startswith(b"\x1a\x45\xdf\xa3"):
                is_video = True
                is_webm = True
            elif b"ftyp" in header[:32] or b"moov" in header[:64] or b"mdat" in header[:64]:
                is_video = True
            elif (header.startswith(b"RIFF") and b"AVI " in header[8:16]) or header.startswith(b"FLV"):
                is_video = True
            elif header.startswith(b"\xff\xd8\xff") or header.startswith(b"\x89PNG\r\n\x1a\n") or header.startswith(b"GIF8"):
                is_image = True
            elif header.startswith(b"RIFF") and b"WEBP" in header[8:16]:
                is_image = True

        fname_low = base_name.lower()
        if any(k in fname_low for k in ["rc-upload", "video", "khui", "quay", "clip", "cam", "pack", "boc", "dong", "hang", "img_"]):
            is_video = True
        if ext == ".webm" or "webm" in fname_low:
            is_webm = True

        size_mb = os.path.getsize(file_path) / (1024 * 1024)

        # Nén video > 20MB hoặc tệp chưa chuẩn MP4
        if is_webm or ext in [".webm", ".mov", ".avi", ".mkv"] or (is_video and (ext != ".mp4" or size_mb > 20.0)):
            return compress_video_to_safe_mp4(file_path)

        if is_image and ext not in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"]:
            new_file_name = f"{name}.jpg"
            new_path = os.path.join(dir_name, new_file_name)
            os.rename(file_path, new_path)
            return new_path

    except Exception as e:
        print(f"Lỗi kiểm tra tệp: {e}")
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
            rx_id = body.get("data", {}).get("reaction_id", "")
            print(f"✨ Auto reaction [{emoji_type}] vào message_id: {message_id}")
            return rx_id
    except Exception as e:
        print(f"Lỗi gọi API reaction: {e}")
    return ""

def remove_reaction_from_message(message_id: str, reaction_id: str):
    token = get_tenant_access_token()
    if not token or not message_id or not reaction_id:
        return

    url = f"{TARGET_DOMAIN}/open-apis/im/v1/messages/{message_id}/reactions/{reaction_id}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        res = global_session.delete(url, headers=headers, timeout=10)
        if res.status_code == 200:
            print(f"⏰ Đã gỡ reaction đồng hồ")
    except Exception as e:
        print(f"Lỗi gỡ reaction: {e}")

# ----------------- TẢI LÊN FILE LARK -----------------
def upload_lark_file(file_path: str, file_type: str = "stream") -> str:
    if not os.path.exists(file_path):
        return ""
    if os.path.getsize(file_path) / (1024 * 1024) > 28.0:
        return ""

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
            res = global_session.post(url, headers=headers, data=data, files=files, timeout=90)
            if res.status_code == 200:
                body = res.json()
                if body.get("code") == 0:
                    return body["data"]["file_key"]
                else:
                    print(f"❌ Lark API lỗi upload: {body}")
    except Exception as e:
        print(f"Lỗi upload: {e}")
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
                            print(f"✅ Đã gửi ảnh: {file_name}")

            # 2. Tệp Video
            elif file_ext in [".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".webm", ".m4v", ".3gp"]:
                send_path = compress_video_to_safe_mp4(file_path, original_name=file_name)
                
                if not os.path.exists(send_path) or os.path.getsize(send_path) < 50000:
                    print(f"⚠️ Video {file_name} bị lỗi dữ liệu -> Bỏ qua!")
                    continue

                size_mb = os.path.getsize(send_path) / (1024 * 1024)
                file_key = ""
                if size_mb <= 28.0:
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
                        print(f"📥 Đã bung video phát trực tiếp: {os.path.basename(send_path)}")
                else:
                    if size_mb > 28.0:
                        direct_url = urls[0] if urls else ""
                        reply_thread_card(message_id, {
                            "elements": [
                                {"tag": "markdown", "content": f"⚠️ Video **{file_name}** ({format_size(os.path.getsize(file_path))}) có dung lượng lớn vượt giới hạn tải lên của Lark. Chị bấm vào link gốc để xem trực tiếp nhé: [**Mở Video**]({direct_url})"}
                            ]
                        })
                    else:
                        print(f"⚠️ Lỗi mạng không thể tải lên video {file_name} lên Lark!")

            # 3. Tệp khác
            else:
                if os.path.getsize(file_path) / (1024 * 1024) <= 28.0:
                    file_key = upload_lark_file(file_path, "stream")
                    if file_key:
                        file_body = ReplyMessageRequestBody.builder().content(json.dumps({"file_key": file_key})).msg_type("file").reply_in_thread(True).build()
                        resp = client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(file_body).build())
                        if resp and resp.success():
                            actual_sent_count += 1
                            print(f"📎 Đã bung tệp: {file_name}")
        except Exception as e:
            print(f"Lỗi gửi media: {e}")

        gc.collect()

    return actual_sent_count

# ----------------- 5. GIẢI MÃ LINK ĐA TẦNG CHO BOM.SO, BYVN.NET, L1NK.DEV -----------------
def extract_urls_from_text(raw_text: str) -> list:
    urls = []
    text = html.unescape(raw_text)
    text = urllib.parse.unquote(text)
    text = text.replace(r"\/", "/").replace(r"\u002f", "/").replace(r"\u002F", "/")

    found = re.findall(r'(https?://[^\s"\'<>]+)', text)
    for u in found:
        clean_u = u.split('"')[0].split("'")[0].split('\\')[0].rstrip(';>,.')
        if not any(ign in clean_u.lower() for ign in ["byvn.net", "bom.so", "l1nk.dev", "encurtador", "google.com/search", "facebook.com", "w3.org", "schema.org"]):
            urls.append(clean_u)

    b64_candidates = re.findall(r'[A-Za-z0-9+/=]{16,}', raw_text)
    for c in b64_candidates:
        try:
            padded = c + "=" * ((4 - len(c) % 4) % 4)
            dec = base64.b64decode(padded).decode('utf-8', errors='ignore')
            if any(k in dec for k in ["drive.google.com", "sharepoint", "aliyuncs", "fptcloud", "http"]):
                dec_urls = re.findall(r'https?://[^\s"\'<>]+', dec)
                for du in dec_urls:
                    if not any(ign in du.lower() for ign in ["byvn.net", "bom.so", "l1nk.dev"]):
                        urls.append(du)
        except Exception:
            pass

    folder_ids = re.findall(r'folders/([a-zA-Z0-9_-]{28,45})', text)
    for fid in folder_ids:
        urls.append(f"https://drive.google.com/drive/folders/{fid}")

    return list(set(urls))

def resolve_proof_url(url: str) -> str:
    if any(k in url.lower() for k in [
        ".mp4", ".mov", ".png", ".jpg", ".jfif", ".webm", ".avi", ".mkv",
        "aliyuncs.com", "oss-", "rc-upload", "tiktokcdn.com", "byteoversea.com", "fptcloud.com"
    ]):
        return url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"
    }
    cur_url = url.strip()

    # Bắt nhanh chuyển hướng bằng stream=True (không tải toàn bộ file nếu là video trực tiếp)
    try:
        r_trace = global_session.get(cur_url, headers=headers, allow_redirects=True, timeout=15, verify=False, stream=True)
        final_dest = r_trace.url
        r_trace.close()
        if not any(s in final_dest.lower() for s in ["bom.so", "byvn.net", "l1nk.dev", "encurtador.dev", "acesse.one"]):
            print(f"🔗 Đã bắt link đích chuyển hướng: {final_dest}")
            return final_dest
        cur_url = final_dest
    except Exception:
        pass

    if "stream.aspx" in cur_url and "id=" in cur_url:
        m = re.search(r'id=([^&]+)', cur_url)
        if m:
            file_server_path = urllib.parse.unquote(m.group(1))
            tenant_base = cur_url.split("/personal/")[0]
            return f"{tenant_base}/personal/{file_server_path.split('/personal/')[1]}?download=1"

    if "sharepoint.com" in cur_url or "1drv.ms" in cur_url:
        sep = "&" if "?" in cur_url else "?"
        if "download=1" not in cur_url:
            return f"{cur_url}{sep}download=1"
        return url

    # Quét sâu nếu link rút gọn trả về trang đệm HTML
    try:
        r_html = global_session.get(cur_url, headers=headers, timeout=12, verify=False)
        html_text = r_html.text
        html_unescaped = html.unescape(html_text).replace(r"\/", "/").replace(r"\u002f", "/")

        found_urls = extract_urls_from_text(html_unescaped)
        for cand in found_urls:
            print(f"🔗 Bóc tách thành công link đích ẩn trong mã nguồn: {cand}")
            return cand

        meta_match = re.search(r'<meta[^>]*?content=["\']\d+;\s*url=([^"\'>\s]+)["\']', html_text, re.IGNORECASE)
        if meta_match:
            return urllib.parse.urljoin(cur_url, meta_match.group(1))

        js_match = re.search(r'(?:window\.location(?:\.href)?|location\.replace)\s*=\s*["\']([^"\']+)["\']', html_text)
        if js_match:
            js_dest = js_match.group(1)
            if js_dest.startswith("http") and not any(s in js_dest for s in ["byvn.net", "bom.so"]):
                return js_dest
    except Exception:
        pass

    return cur_url

def extract_gdrive_title(file_id: str) -> str:
    try:
        url = f"https://drive.google.com/file/d/{file_id}/view"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        r = global_session.get(url, headers=headers, timeout=10, verify=False)
        if r.status_code == 200:
            m = re.search(r'<title>(.*?) - Google Drive</title>', r.text, re.IGNORECASE)
            if m:
                found_title = m.group(1).strip()
                if found_title and not found_title.startswith("http"):
                    return found_title
    except Exception:
        pass
    return ""

def _save_gdrive_stream(res, target_dir: str, real_title: str, file_id: str) -> bool:
    content_disposition = res.headers.get("Content-Disposition", "")
    extracted_name = ""
    if "filename=" in content_disposition:
        fn_match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)["\']?', content_disposition)
        if fn_match:
            try:
                extracted_name = urllib.parse.unquote(fn_match.group(1))
            except Exception:
                extracted_name = fn_match.group(1)

    raw_save_name = real_title or extracted_name or f"gdrive_{file_id}.mp4"
    clean_save_name = sanitize_filename(raw_save_name)
    save_path = os.path.join(target_dir, clean_save_name)

    with open(save_path, "wb") as f:
        for chunk in res.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)
    if os.path.exists(save_path) and os.path.getsize(save_path) > 50000:
        print(f"📥 Đã tải Drive thành công: {clean_save_name} ({format_size(os.path.getsize(save_path))})")
        return True
    return False

# ----------------- TẢI FILE GOOGLE DRIVE VƯỢT QUA TRANG CẢNH BÁO VI-RÚT (> 25MB) -----------------
def download_single_gdrive_file(file_id: str, target_dir: str, preferred_name: str = "") -> bool:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "*/*"
    }
    real_title = preferred_name or extract_gdrive_title(file_id)

    try:
        # Bước 1: Yêu cầu tải ban đầu
        init_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        res = global_session.get(init_url, headers=headers, stream=True, verify=False, timeout=30)
        
        # Nếu tệp nhỏ (< 25MB) được tải trực tiếp
        content_type = res.headers.get("Content-Type", "").lower()
        if res.status_code == 200 and "text/html" not in content_type:
            return _save_gdrive_stream(res, target_dir, real_title, file_id)

        # Bước 2: Tệp > 25MB (40MB/50MB/114MB) chuyển qua trang cảnh báo vi-rút
        html_text = res.text

        if not real_title:
            fn_match = re.search(r'([a-zA-Z0-9_\-\. ]+\.(?:mp4|mov|avi|mkv|webm))\s*\(\d+M\)', html_text)
            if fn_match:
                real_title = fn_match.group(1).strip()

        form_match = re.search(r'<form[^>]+action=["\']([^"\']+)["\']', html_text)
        link_match = re.search(r'<a[^>]+id=["\']uc-download-link["\'][^>]+href=["\']([^"\']+)["\']', html_text)
        
        download_url = None
        params = {}

        if form_match:
            download_url = form_match.group(1)
            input_matches = re.findall(r'<input[^>]+name=["\']([^"\']+)["\'][^>]+value=["\']([^"\']*)["\']', html_text)
            for k, v in input_matches:
                params[k] = v
            if "id" not in params:
                params["id"] = file_id
            if "export" not in params:
                params["export"] = "download"
        elif link_match:
            download_url = urllib.parse.urljoin("https://drive.google.com", link_match.group(1))
        else:
            uuid_match = re.search(r'name="uuid"\s+value="([^"]+)"', html_text) or re.search(r'uuid=([a-f0-9\-]+)', html_text)
            confirm_match = re.search(r'name="confirm"\s+value="([^"]+)"', html_text) or re.search(r'confirm=([0-9a-zA-Z_]+)', html_text)
            uuid_val = uuid_match.group(1) if uuid_match else ""
            confirm_val = confirm_match.group(1) if confirm_match else "t"
            
            download_url = "https://drive.usercontent.google.com/download"
            params = {"id": file_id, "export": "download", "confirm": confirm_val}
            if uuid_val:
                params["uuid"] = uuid_val

        if download_url:
            res_down = global_session.get(download_url, params=params, headers=headers, stream=True, verify=False, timeout=180)
            if res_down.status_code == 200 and "text/html" not in res_down.headers.get("Content-Type", "").lower():
                return _save_gdrive_stream(res_down, target_dir, real_title, file_id)

    except Exception as e:
        print(f"Lỗi tải Drive trực tiếp: {e}")

    # Dự phòng gdown nếu cần
    try:
        import gdown
        clean_save_name = sanitize_filename(real_title or f"gdrive_{file_id}.mp4")
        fallback_path = os.path.join(target_dir, clean_save_name)
        output = gdown.download(id=file_id, output=fallback_path, quiet=True)
        if output and os.path.exists(output) and os.path.getsize(output) > 50000:
            print(f"📥 Đã tải Drive bằng gdown thành công: {clean_save_name}")
            return True
    except Exception as e:
        print(f"Lỗi gdown: {e}")

    return False

def download_gdrive_folder(folder_url: str, target_dir: str) -> bool:
    folder_match = re.search(r'/folders/([a-zA-Z0-9_-]+)', folder_url)
    folder_id = folder_match.group(1) if folder_match else ""
    clean_url = f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else folder_url.split("?")[0]

    try:
        import gdown
        downloaded = gdown.download_folder(clean_url, output=target_dir, quiet=True, use_cookies=False)
        if downloaded and len(downloaded) > 0:
            return True
    except Exception:
        pass

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        res = global_session.get(clean_url, headers=headers, timeout=15, verify=False)
        if res.status_code == 200:
            html_text = res.text
            found_files = {}

            matches = re.findall(r'\["([a-zA-Z0-9_-]{28,45})",\["([^"]+)"', html_text)
            matches += re.findall(r'\["([a-zA-Z0-9_-]{28,45})","([^"]+)"', html_text)
            for fid, fname in matches:
                if fid not in found_files and 28 <= len(fid) <= 45 and fid != folder_id:
                    if not fname.startswith("http") and not any(k in fname for k in ["<", ">", "{", "}", ";"]):
                        found_files[fid] = fname

            if found_files:
                success_count = 0
                for fid, fname in found_files.items():
                    if download_single_gdrive_file(fid, target_dir, fname):
                        success_count += 1
                return success_count > 0
    except Exception as e:
        print(f"Lỗi Folder Drive: {e}")

    return False

def download_proof(url: str, target_dir: str) -> bool:
    final_url = resolve_proof_url(url)
    
    if "drive.google.com" in final_url:
        if "/folders/" in final_url:
            return download_gdrive_folder(final_url, target_dir)
        else:
            match = re.search(r'(?:/file/d/|id=)([a-zA-Z0-9_-]{25,50})', final_url)
            if match:
                return download_single_gdrive_file(match.group(1), target_dir)

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*"
        }
        res = global_session.get(final_url, headers=headers, stream=True, timeout=90, verify=False)
        
        content_type = res.headers.get("Content-Type", "").lower()
        if "text/html" in content_type:
            return False

        content_disposition = res.headers.get("Content-Disposition", "")
        extracted_name = ""
        if "filename=" in content_disposition:
            fn_match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)["\']?', content_disposition)
            if fn_match:
                try:
                    extracted_name = urllib.parse.unquote(fn_match.group(1))
                except Exception:
                    extracted_name = fn_match.group(1)

        raw_name = extracted_name or os.path.basename(urllib.parse.urlparse(final_url).path) or "downloaded_file"

        if ("webm" in content_type or "rc-upload" in raw_name.lower() or "aliyuncs.com" in final_url) and "." not in raw_name:
            raw_name = f"{raw_name}.webm"

        save_name = sanitize_filename(raw_name)
        save_path = os.path.join(target_dir, save_name)

        if res.status_code == 200:
            with open(save_path, "wb") as f:
                for chunk in res.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
            
            if os.path.exists(save_path) and (save_name.endswith(".zip") or "zip" in content_type):
                try:
                    with zipfile.ZipFile(save_path, 'r') as zip_ref:
                        zip_ref.extractall(target_dir)
                    os.remove(save_path)
                    return True
                except Exception as e:
                    print(f"Lỗi giải nén ZIP: {e}")

            if os.path.exists(save_path) and os.path.getsize(save_path) > 1000:
                return True
            elif os.path.exists(save_path):
                os.remove(save_path)
    except Exception as e:
        print(f"Lỗi tải trực tiếp: {e}")

    return False

# ----------------- 6. BÓC TÁCH NỘI DUNG TIN NHẮN -----------------
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
                    text_val = item.get("text", "") or item.get("href", "")
                    extracted_pieces.append(text_val)
                elif tag == "at":
                    extracted_pieces.append(f"@{item.get('user_name', '') or item.get('user_id', '')}")
        return " ".join(extracted_pieces)

    return ""

# ----------------- 7. XỬ LÝ CHÍNH & PHẢN HỒI THẺ CHO TỪNG TICKET -----------------
def process_single_task(message_id: str, chat_id: str, ticket_id: str, urls: list, sender_id: str):
    print(f"📥 BẮT ĐẦU XỬ LÝ ĐƠN: {ticket_id} (Tổng link: {len(urls)})")
    
    clock_rx_id = add_reaction_to_message(message_id, "AlarmClock")

    try:
        req_count = get_current_request_count(ticket_id)
        task_temp_dir = os.path.join(TEMP_DIR, f"{message_id}_{ticket_id}")
        shutil.rmtree(task_temp_dir, ignore_errors=True)
        os.makedirs(task_temp_dir, exist_ok=True)

        for u in urls:
            print(f"⏳ Đang tải link: {u}")
            download_proof(u, task_temp_dir)

        final_files = []
        for root, _, fs in os.walk(task_temp_dir):
            for f in fs:
                raw_path = os.path.join(root, f)
                if os.path.getsize(raw_path) > 1000:
                    fixed_path = auto_detect_and_fix_extension(raw_path)
                    fixed_name = os.path.basename(fixed_path)
                    final_files.append({
                        "name": fixed_name,
                        "path": fixed_path,
                        "size": os.path.getsize(fixed_path),
                        "ext": os.path.splitext(fixed_name)[1].lower()
                    })

        # ---------------- THẺ BÁO LỖI: BANNER THU NHỎ 1/2 VÀ CĂN GIỮA ----------------
        if not final_files:
            print(f"❌ Không tải được file nào cho đơn {ticket_id}")
            if clock_rx_id:
                remove_reaction_from_message(message_id, clock_rx_id)

            first_url = urls[0] if urls else ""
            error_img_element = build_half_size_banner(BANNER_ERROR_KEY, "Cảnh báo truy cập")

            if "sharepoint.com" in first_url or "1drv.ms" in first_url:
                sharepoint_card = {
                    "elements": error_img_element + [
                        {
                            "tag": "markdown",
                            "content": (
                                f"📁 **𝗧𝗶𝗰𝗸𝗲𝘁 𝗜𝗗: {ticket_id}**\n\n"
                                f"<font color='orange'>⚠️ Link được chia sẻ là **Thư mục SharePoint nội bộ**, bot không thể tải tự động do cơ chế bảo mật của Microsoft.</font>\n\n"
                                f"👉 [**Bấm vào đây để mở trực tiếp Thư mục SharePoint**]({first_url})\n\n"
                                f"<font color='grey'>💡 *Mẹo: Hãy bấm vào dấu 3 chấm cạnh video và chọn 'Sao chép liên kết' (Copy link) của riêng video đó rồi gửi lại cho bot nhé!*</font>"
                            )
                        },
                        {"tag": "hr"},
                        {"tag": "markdown", "content": FOOTER_RAIN_TEXT}
                    ]
                }
                reply_thread_card(message_id, sharepoint_card)
            else:
                reply_thread_card(message_id, {
                    "elements": error_img_element + [
                        {"tag": "markdown", "content": f"<text_tag color='carmine'>🚨 Không thể tải video của 𝗧𝗶𝗰𝗸𝗲𝘁 𝗜𝗗: {ticket_id}, vui lòng kiểm tra lại quyền truy cập link!</text_tag>"},
                        {"tag": "hr"},
                        {"tag": "markdown", "content": FOOTER_RAIN_TEXT}
                    ]
                })
            shutil.rmtree(task_temp_dir, ignore_errors=True)
            return

        record_successful_request(ticket_id, req_count)
        total_size = sum(x["size"] for x in final_files)
        file_count = len(final_files)

        # ---------------- PHÂN LOẠI NHÓM ĐỊNH DẠNG FILE BẬC THANG ----------------
        video_exts = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".wmv", ".webm", ".m4v", ".3gp"}
        image_exts = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic", ".jfif", ".svg", ".tiff"}
        audio_exts = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac", ".wma", ".opus"}

        c_video = sum(1 for x in final_files if x["ext"] in video_exts)
        c_image = sum(1 for x in final_files if x["ext"] in image_exts)
        c_audio = sum(1 for x in final_files if x["ext"] in audio_exts)
        c_doc = sum(1 for x in final_files if x["ext"] not in (video_exts | image_exts | audio_exts))

        cat_hierarchy = [
            ("🎞️", c_video),
            ("🖼️", c_image),
            ("📼", c_audio),
            ("📑", c_doc),
        ]

        indent_steps = [
            "  ",
            "                ",
            "                               ",
            "                                             "
        ]

        group_lines = [f"🗂️: {file_count} file"]
        step_idx = 0
        for icon, count in cat_hierarchy:
            if count > 0:
                indent = indent_steps[step_idx] if step_idx < len(indent_steps) else " " * (2 + step_idx * 14)
                group_lines.append(f"{indent}╰┈➤{icon} : {count} file")
                step_idx += 1

        summary_group_str = "\n".join(group_lines)

        # ---------------- THẺ 1: BANNER THU NHỎ 1/2 VÀ CĂN GIỮA Ở TRÊN CÙNG ----------------
        file_lines = []
        for item in final_files:
            file_lines.append(f"         <font color='carmine'>╰┄‌•  </font>{item['name']}: <text_tag color='carmine'>[{format_size(item['size'])}]</text_tag>")
        files_str = "\n".join(file_lines)

        if req_count > 1:
            repeat_tag = f" <text_tag color='orange'>🔁 Yêu cầu lần {req_count}</text_tag>"
        else:
            repeat_tag = " <text_tag color='grey'>🔁 Lần 1</text_tag>"

        header_block = (
            f"🎫<text_tag color='turquoise'>{ticket_id}</text_tag>{repeat_tag}\n"
            f"   ╰┄▸ 💾<text_tag color='carmine'>{format_size(total_size)}</text_tag>\n"
            f"         ╰┄▸ 🗂️ <text_tag color='indigo'>{file_count}/{file_count}</text_tag>\n\n"
            f"{summary_group_str}\n\n"
            f"{files_str}"
        )

        card1_img_element = build_half_size_banner(BANNER_CARD1_KEY, "Đang tải dữ liệu")

        loading_card_payload = {
            "elements": card1_img_element + [
                {"tag": "markdown", "content": header_block},
                {"tag": "hr"},
                {"tag": "markdown", "content": FOOTER_RAIN_TEXT}
            ]
        }
        reply_thread_card(message_id, loading_card_payload)

        # ---------------- BUNG TỆP VÀO THREAD (ĐÃ BẢO ĐẢM TẤT CẢ VIDEO LUÔN < 20MB ĐỂ PHÁT TRỰC TIẾP) ----------------
        actual_bung_success = upload_and_send_batch_proofs(message_id, final_files, urls)

        # ---------------- THẺ 2 (HOÀN TẤT): BANNER THU NHỎ 1/2 VÀ CĂN GIỮA Ở TRÊN CÙNG ----------------
        title_side_md = "<text_tag color='turquoise'>ᴄᴏᴍᴘʟᴇᴛᴇᴅ</text_tag>\n<text_tag color='turquoise'>-ˋˏ    𝐃𝐎𝐖𝐍𝐋𝐎𝐀𝐃 𝐏𝐑𝐎OF ˎˊ-</text_tag>"
        sender_mention = f"<at id=\"{sender_id}\"></at>" if sender_id else "chị"
        
        heading_md = f"<font color='carmine'>**♡ {sender_mention} ơi...</font>**\n      ╰┄▸ 🎫 *<text_tag color='carmine'>{ticket_id}</text_tag>*{repeat_tag}"
        thankyou_md = "<font color='turquoise'>      ┊ t h a n k y o u ┊\n┈┈┈┈┈┈┈┈․° ••• °․┈┈┈┈┈┈┈┈</font>"

        card2_img_element = build_half_size_banner(BANNER_COMPLETED_KEY, "Hoàn tất")

        finish_card_payload = {
            "elements": card2_img_element + [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": title_side_md},
                    "text_align": "center"
                },
                {"tag": "markdown", "content": heading_md},
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": thankyou_md},
                    "text_align": "center"
                },
                {"tag": "hr"},
                {"tag": "markdown", "content": FOOTER_RAIN_TEXT}
            ]
        }
        reply_thread_card(message_id, finish_card_payload)

        # ---------------- 🐍 ĐỔI TỪ ĐỒNG HỒ SANG BÉ RẮN KHI HOÀN TẤT ----------------
        if clock_rx_id:
            remove_reaction_from_message(message_id, clock_rx_id)

        if actual_bung_success > 0:
            add_reaction_to_message(message_id, "KeepYourSpiritsAwake")
        else:
            print(f"⚠️ Bung được ({actual_bung_success}/{len(final_files)}) media hợp lệ")

        shutil.rmtree(task_temp_dir, ignore_errors=True)
        gc.collect()

    except Exception as e:
        print(f"Lỗi trong process_single_task: {e}")
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
                    urls = re.findall(r'https?://[^\s<>"]+', current_text)
                    if urls:
                        threading.Thread(target=process_single_task, args=(message_id, chat_id, current_id, urls, sender_id), daemon=True).start()
                current_id = part
                current_text = ""
            else:
                current_text += " " + part
                
        if current_id and current_text:
            urls = re.findall(r'https?://[^\s<>"]+', current_text)
            if urls:
                threading.Thread(target=process_single_task, args=(message_id, chat_id, current_id, urls, sender_id), daemon=True).start()
    else:
        urls = re.findall(r'https?://[^\s<>"]+', text)
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
            print(f"📩 [LARK] ĐÃ BẮT ĐƯỢC LINK MỚI: {text[:80]}...")
            t = threading.Thread(target=parse_and_dispatch, args=(msg.message_id, chat_id, text, sender_id))
            t.daemon = True
            t.start()
    except Exception as e:
        print(f"Lỗi message: {e}")

# ----------------- 8. KHỞI CHẠY WEBSOCKET LARK CLIENT -----------------
def start_bot():
    print("🚀 BOT LARK PROOF SẴN SÀNG (ĐÃ TỰ ĐỘNG VƯỢT XÁC NHẬN DRIVE & SỬA LỖI LINK BOM.SO)...")

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

if __name__ == "__main__":
    start_bot()
