import os
import re
import gc
import json
import shutil
import urllib.parse
import subprocess
import threading
import datetime
import unicodedata
import zipfile
import http.server
import socketserver
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
import urllib3
from PIL import Image
import pillow_heif

try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except Exception:
    pass

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
pillow_heif.register_heif_opener()

# ----------------- 1. CẤU HÌNH BIẾN MÔI TRƯỜNG & SDK LARK -----------------
APP_ID = os.environ.get("APP_ID", "").strip() or os.environ.get("LARK_APP_ID", "").strip()
APP_SECRET = os.environ.get("APP_SECRET", "").strip() or os.environ.get("LARK_APP_SECRET", "").strip()
TARGET_DOMAIN = getattr(lark, "LARK_DOMAIN", "https://open.larksuite.com")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "temp_files")
ERROR_IMG_PATH = os.path.join(BASE_DIR, "gdrive_error.png")
HISTORY_FILE = os.path.join(BASE_DIR, "history_proof.json")

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

# ----------------- 2. SERVER HTTP GIỮ TRẠNG THÁI LIVE TRÊN RENDER -----------------
class RenderHealthHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot Lark Proof is running healthy 24/7!")

    def log_message(self, format, *args):
        pass

def run_dummy_web_server():
    port = int(os.environ.get("PORT", 10000))
    try:
        with socketserver.TCPServer(("", port), RenderHealthHandler) as httpd:
            print(f"🌐 Đã mở cổng HTTP {port} để duy trì dịch vụ...")
            httpd.serve_forever()
    except Exception as e:
        print(f"Lưu ý server HTTP: {e}")

# ----------------- 3. QUẢN LÝ LỊCH SỬ & ĐẾM SỐ LẦN XIN -----------------
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

def clean_file_display_name(filename: str) -> str:
    try:
        return filename.encode('latin1').decode('utf-8')
    except Exception:
        return filename

def sanitize_filename(filename: str) -> str:
    nfkd = unicodedata.normalize('NFKD', filename)
    ascii_name = re.sub(r'[^\w\s.-]', '', nfkd.encode('ASCII', 'ignore').decode('ASCII'))
    return re.sub(r'\s+', '_', ascii_name).strip()

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

def send_text_message(receive_id: str, text: str, receive_id_type: str = "open_id"):
    try:
        req = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(CreateMessageRequestBody.builder()
                          .receive_id(receive_id)
                          .msg_type("text")
                          .content(json.dumps({"text": text}))
                          .build()) \
            .build()
        client.im.v1.message.create(req)
    except Exception as e:
        print(f"Lỗi gửi tin nhắn: {e}")

# ----------------- 4. NÉN VIDEO & UPLOAD CHUẨN LARK API -----------------
def compress_video_if_large(video_path: str) -> str:
    """Nén video nếu vượt quá 24MB để đảm bảo Lark chấp nhận tải lên"""
    try:
        size_mb = os.path.getsize(video_path) / (1024 * 1024)
        if size_mb <= 24.0 and video_path.lower().endswith(".mp4"):
            return video_path

        name, _ = os.path.splitext(video_path)
        compressed_path = f"{name}_cmp.mp4"

        cmd = [
            "ffmpeg", "-y", "-nostdin",
            "-threads", "2",
            "-i", video_path,
            "-vf", "scale='min(640,iw)':-2,fps=20",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "32",
            "-c:a", "aac", "-b:a", "32k", "-ac", "1",
            compressed_path
        ]
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        gc.collect()

        if os.path.exists(compressed_path) and os.path.getsize(compressed_path) > 1000:
            print(f"⚡ Đã nén video thành công: {os.path.basename(compressed_path)} ({format_size(os.path.getsize(compressed_path))})")
            return compressed_path
    except Exception as e:
        print(f"Lưu ý nén video: {e}")
    return video_path

def upload_video_for_stream(file_path: str) -> str:
    """Upload video vào resource của Lark để phát trực tiếp khung video trong tin nhắn"""
    token = get_tenant_access_token()
    if not token:
        return ""
    
    url = f"{TARGET_DOMAIN}/open-apis/im/v1/messages/resources?type=video"
    raw_name = os.path.basename(file_path)
    safe_name = sanitize_filename(raw_name)

    headers = {"Authorization": f"Bearer {token}"}
    try:
        with open(file_path, "rb") as f:
            files = {"file": (safe_name, f, "video/mp4")}
            res = global_session.post(url, headers=headers, files=files, timeout=300)
            if res.status_code == 200:
                body = res.json()
                if body.get("code") == 0:
                    return body.get("data", {}).get("file_key", "")
                else:
                    print(f"Lark Resource API lỗi: {body}")
    except Exception as e:
        print(f"Lỗi upload video resource: {e}")
    return ""

def upload_file_direct(file_path: str, file_type: str = "stream") -> str:
    """Upload tệp đính kèm thông thường qua im/v1/files"""
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
            res = global_session.post(url, headers=headers, data=data, files=files, timeout=300)
            if res.status_code == 200:
                body = res.json()
                if body.get("code") == 0:
                    return body["data"]["file_key"]
    except Exception as e:
        print(f"Lỗi upload file: {e}")
    return ""

def upload_and_send_batch_proofs(message_id: str, final_files: list):
    """Bung từng tệp video và hình ảnh vào Thread"""
    for f in final_files:
        file_path = f["path"]
        file_ext = f["ext"]
        file_name = f["name"]

        try:
            # 1. Nếu là tệp ảnh
            if file_ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]:
                with open(file_path, "rb") as img_f:
                    create_req = CreateImageRequest.builder() \
                        .request_body(CreateImageRequestBody.builder().image_type("message").image(img_f).build()) \
                        .build()
                    create_resp = client.im.v1.image.create(create_req)
                    if create_resp and create_resp.success():
                        img_k = create_resp.data.image_key
                        body = ReplyMessageRequestBody.builder().content(json.dumps({"image_key": img_k})).msg_type("image").reply_in_thread(True).build()
                        client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(body).build())
                        print(f"✅ Đã gửi ảnh vào thread: {file_name}")

            # 2. Nếu là tệp video
            elif file_ext in [".mp4", ".mov", ".avi", ".mkv"]:
                send_path = compress_video_if_large(file_path)

                # Gửi khung video có thể xem ngay
                video_key = upload_video_for_stream(send_path)
                if video_key:
                    media_body = ReplyMessageRequestBody.builder().content(json.dumps({"file_key": video_key})).msg_type("media").reply_in_thread(True).build()
                    client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(media_body).build())
                    print(f"🎬 Đã bung khung phát video: {file_name}")

                # Gửi tệp đính kèm để tải về máy
                file_key = upload_file_direct(send_path, "stream")
                if file_key:
                    file_body = ReplyMessageRequestBody.builder().content(json.dumps({"file_key": file_key})).msg_type("file").reply_in_thread(True).build()
                    client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(file_body).build())
                    print(f"📥 Đã gửi tệp đính kèm: {file_name}")

            # 3. Các loại tệp khác
            else:
                file_key = upload_file_direct(file_path, "stream")
                if file_key:
                    file_body = ReplyMessageRequestBody.builder().content(json.dumps({"file_key": file_key})).msg_type("file").reply_in_thread(True).build()
                    client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(file_body).build())
                    print(f"📎 Đã gửi tệp: {file_name}")

        except Exception as e:
            print(f"Lỗi gửi media {file_name}: {e}")

        gc.collect()

# ----------------- 5. GIẢI MÃ LINK RÚT GỌN, FPT CLOUD & GOOGLE DRIVE -----------------
def resolve_proof_url(url: str) -> str:
    if any(ext in url.lower() for ext in [".mp4", ".mov", ".png", ".jpg", ".jfif", ".webm"]):
        return url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    cur_url = url

    if "acesse.one" in cur_url or "encurtador.dev" in cur_url:
        try:
            code = cur_url.rstrip("/").split("/")[-1]
            api_url = f"https://encurtador.dev/api/link/{code}"
            r_api = global_session.get(api_url, headers=headers, timeout=8, verify=False)
            if r_api.status_code == 200:
                data = r_api.json()
                dest = data.get("link", {}).get("destination") or data.get("destination") or data.get("url")
                if dest:
                    return dest
        except Exception:
            pass

    if "bom.so" in cur_url:
        try:
            r_bom = global_session.get(cur_url, headers=headers, allow_redirects=True, timeout=10, verify=False)
            if r_bom.url != cur_url:
                return r_bom.url
        except Exception:
            pass

    for _ in range(4):
        try:
            r = global_session.get(cur_url, headers=headers, allow_redirects=True, timeout=12, verify=False)
            if r.url != cur_url:
                cur_url = r.url
            if any(k in cur_url for k in ["drive.google.com", ".mp4", ".png", ".jpg"]):
                return cur_url

            meta_match = re.search(r'<meta[^>]*?content=["\']\d+;\s*url=([^"\'>\s]+)["\']', r.text, re.IGNORECASE)
            if meta_match:
                cur_url = urllib.parse.urljoin(cur_url, meta_match.group(1).replace("&amp;", "&"))
                continue

            dest_match = re.search(r'["\'](https?://(?:drive\.google\.com|[^"\']*?\.(?:mp4|mov|jpg|png))[^"\']*)["\']', r.text)
            if dest_match:
                cur_url = dest_match.group(1).replace("&amp;", "&")
                break
            break
        except Exception:
            break

    return cur_url

def download_single_gdrive_file(file_id: str, target_dir: str, preferred_name: str = "") -> bool:
    save_name = preferred_name or f"gdrive_{file_id}.mp4"
    save_path = os.path.join(target_dir, save_name)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    try:
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
        res = global_session.get(url, headers=headers, stream=True, verify=False, timeout=25)
        
        confirm_token = None
        for k, v in res.cookies.items():
            if k.startswith("download_warning"):
                confirm_token = v
                break
        if not confirm_token:
            m = re.search(r'confirm=([0-9A-Za-z_]+)', res.text)
            if m:
                confirm_token = m.group(1)

        if confirm_token:
            url = f"https://drive.google.com/uc?export=download&confirm={confirm_token}&id={file_id}"
            res = global_session.get(url, headers=headers, stream=True, verify=False, timeout=120)

        if res.status_code == 200 and "text/html" not in res.headers.get("Content-Type", ""):
            with open(save_path, "wb") as f:
                for chunk in res.iter_content(chunk_size=8 * 1024 * 1024):
                    if chunk:
                        f.write(chunk)
            if os.path.exists(save_path) and os.path.getsize(save_path) > 2000:
                return True
    except Exception as e:
        print(f"Lỗi tải file Drive: {e}")

    try:
        import gdown
        output = gdown.download(id=file_id, output=save_path, quiet=True)
        if output and os.path.exists(output) and os.path.getsize(output) > 2000:
            return True
    except Exception:
        pass

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
        res = global_session.get(clean_url, headers=headers, timeout=20, verify=False)
        if res.status_code == 200:
            html = res.text
            found_files = {}

            matches = re.findall(r'\["([a-zA-Z0-9_-]{28,45})",\["([^"]+)"', html)
            for fid, fname in matches:
                fname_clean = clean_file_display_name(fname)
                if any(ext in fname_clean.lower() for ext in [".jfif", ".mp4", ".mov", ".avi", ".mkv", ".jpg", ".png", ".jpeg", ".webp"]):
                    found_files[fid] = fname_clean

            if not found_files:
                matches_alt = re.findall(r'\["([a-zA-Z0-9_-]{28,45})","([^"]+)"', html)
                for fid, fname in matches_alt:
                    fname_clean = clean_file_display_name(fname)
                    if any(ext in fname_clean.lower() for ext in [".jfif", ".mp4", ".mov", ".jpg", ".png", ".jpeg"]):
                        found_files[fid] = fname_clean

            if found_files:
                success_count = 0
                for fid, fname in found_files.items():
                    if download_single_gdrive_file(fid, target_dir, fname):
                        success_count += 1
                return success_count > 0
    except Exception as e:
        print(f"Lỗi bóc tách Folder Drive: {e}")

    return False

def download_proof(url: str, target_dir: str) -> bool:
    final_url = resolve_proof_url(url)
    
    # 1. Google Drive
    if "drive.google.com" in final_url:
        if "/folders/" in final_url:
            return download_gdrive_folder(final_url, target_dir)
        else:
            match = re.search(r'(?:/file/d/|id=)([a-zA-Z0-9_-]+)', final_url)
            if match:
                return download_single_gdrive_file(match.group(1), target_dir)

    # 2. Link trực tiếp S3 FPT Cloud
    try:
        parsed_url = urllib.parse.urlparse(final_url)
        path_name = os.path.basename(parsed_url.path)
        
        if path_name and ("." in path_name):
            save_name = path_name
        else:
            save_name = f"video_{len(os.listdir(target_dir)) + 1}.mp4"

        save_path = os.path.join(target_dir, save_name)
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        res = global_session.get(final_url, headers=headers, stream=True, timeout=120, verify=False)
        if res.status_code == 200:
            with open(save_path, "wb") as f:
                for chunk in res.iter_content(chunk_size=8 * 1024 * 1024):
                    if chunk:
                        f.write(chunk)
            if os.path.exists(save_path) and os.path.getsize(save_path) > 1000:
                return True
    except Exception as e:
        print(f"Lỗi tải file trực tiếp: {e}")

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

# ----------------- 7. XỬ LÝ CHÍNH & PHẢN HỒI THẺ -----------------
def process_request(message_id: str, chat_id: str, text: str, sender_id: str):
    urls = re.findall(r'https?://[^\s<>"]+', text)
    order_match = re.search(r"\b(\d{15,21})\b", text)
    ticket_id = order_match.group(1) if order_match else "PROOF_DATA"

    if not urls:
        return

    req_count = get_current_request_count(ticket_id)
    task_temp_dir = os.path.join(TEMP_DIR, message_id)
    shutil.rmtree(task_temp_dir, ignore_errors=True)
    os.makedirs(task_temp_dir, exist_ok=True)

    for u in urls:
        download_proof(u, task_temp_dir)

    final_files = []
    for root, _, fs in os.walk(task_temp_dir):
        for f in fs:
            raw_path = os.path.join(root, f)
            if os.path.getsize(raw_path) > 500:
                final_files.append({
                    "name": clean_file_display_name(f),
                    "path": raw_path,
                    "size": os.path.getsize(raw_path),
                    "ext": os.path.splitext(f)[1].lower()
                })

    if not final_files:
        reply_thread_card(message_id, {
            "elements": [{"tag": "markdown", "content": "<text_tag color='carmine'>🚨 Không thể tải video từ link trên, vui lòng kiểm tra lại quyền truy cập!</text_tag>"}]
        })
        shutil.rmtree(task_temp_dir, ignore_errors=True)
        return

    record_successful_request(ticket_id, req_count)
    total_size = sum(x["size"] for x in final_files)
    file_count = len(final_files)

    # ---------------- THẺ 1: BÁO CÁO BAN ĐẦU CHUẨN ĐẸP ----------------
    file_lines = []
    for item in final_files:
        file_lines.append(f"<font color='carmine'>╰┄‌• </font> {item['name']}: <text_tag color='carmine'>[{format_size(item['size'])}]</text_tag>")
    files_str = "\n".join(file_lines)

    header_block = (
        f"*<font color='turquoise'>          ≽^•⩊•^≼  </font>*\n"
        f"*<font color='turquoise'> ✧; Ｗｅｌｃｏｍｅ ;✧</font>*\n\n"
        f"🎫 <text_tag color='turquoise'>{ticket_id}</text_tag>\n"
        f"      ╰┄▸ 💾 <text_tag color='carmine'>{format_size(total_size)}</text_tag>\n"
        f"                ╰┄▸ 🗂️ <text_tag color='indigo'>{file_count}/{file_count}</text_tag>\n\n"
        f"• 🎬 : {file_count} file\n"
        f"{files_str}\n\n"
        f"⌛ *<text_tag color='yellow'>Ｌｏａｄｉｎｇ．．．</text_tag>*\n"
        f"*<text_tag color='yellow'>███████▒▒▒ 8O %</text_tag>*"
    )

    report_card_payload = {
        "elements": [
            {
                "tag": "markdown",
                "content": header_block
            }
        ]
    }
    reply_thread_card(message_id, report_card_payload)

    # ---------------- GỬI BUNG TỆP VÀO THREAD ----------------
    upload_and_send_batch_proofs(message_id, final_files)

    # ---------------- THẺ 2: KẾT QUẢ VỚI LEVEL 3 HEADING & CĂN GIỮA TUYỆT ĐỐI ----------------
    rabbit_side_md = "<font color='turquoise'>-ˋ (\\ (\\    .\n.(„• ֊ •„)\n─‌∪─‌∪࿎࿎</font>"
    title_side_md = "        <text_tag color='turquoise'>ᴄᴏᴍᴘʟᴇᴛᴇᴅ</text_tag>\n<text_tag color='turquoise'>-ˋˏ    𝐃𝐎𝐖𝐍𝐋𝐎𝐀𝐃 𝐏𝐑𝐎𝐎𝐅 ˎˊ-</text_tag>"
    
    sender_mention = f"<at id=\"{sender_id}\"></at>" if sender_id else "chị"
    heading_md = f"### ♡ {sender_mention} ơi...\n\n╰┄▸ 🎫 <text_tag color='carmine'>{ticket_id}</text_tag>"
    thankyou_md = "<font color='turquoise'>┊ t h a n k y o u ┊\n┈┈┈┈┈┈┈┈․° ••• °․┈┈┈┈┈┈┈┈</font>"

    finish_card_payload = {
        "elements": [
            {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "default",
                "columns": [
                    {"tag": "column", "width": "auto", "elements": [{"tag": "markdown", "content": rabbit_side_md}]},
                    {"tag": "column", "width": "weighted", "weight": 1, "elements": [{"tag": "markdown", "content": title_side_md}]}
                ]
            },
            {
                "tag": "markdown",
                "content": heading_md
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": thankyou_md
                },
                "text_align": "center"
            }
        ]
    }
    reply_thread_card(message_id, finish_card_payload)

    shutil.rmtree(task_temp_dir, ignore_errors=True)
    gc.collect()

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
            threading.Thread(target=process_request, args=(msg.message_id, chat_id, text, sender_id), daemon=True).start()
    except Exception as e:
        print(f"Lỗi message: {e}")

def handle_menu_click(data: dict) -> None:
    try:
        event = data.get("event", {})
        event_key = event.get("event_key", "")
        operator = event.get("operator", {})
        open_id = operator.get("operator_id", {}).get("open_id", "") or operator.get("open_id", "")

        if event_key == "trigger_proof_template":
            template = (
                "📋 TEMPLATES CHECK PROOF\n\n"
                "Hãy copy đoạn bên dưới, dán vào ô chat rồi thêm Ticket_ID & link proof nhé:\n\n"
                "Take proof & hold after confirmation\n"
                "7686040088400168978\n"
                "https://bom.so/sf8t5L"
            )
            if open_id:
                send_text_message(open_id, template, receive_id_type="open_id")
    except Exception as e:
        print(f"Lỗi menu click: {e}")

def silent_ignored_handler(data) -> None:
    pass

# ----------------- 8. KHỞI CHẠY WEBSOCKET LARK CLIENT -----------------
def start_bot():
    print("🚀 BOT LARK PROOF (PHIÊN BẢN CHUẨN MEDIA RESOURCES)...")
    threading.Thread(target=run_dummy_web_server, daemon=True).start()

    builder = lark.EventDispatcherHandler.builder("", "")
    builder.register_p2_im_message_receive_v1(handle_message)
    builder.register_p2_application_bot_menu_v6(lambda d: handle_menu_click(json.loads(lark.JSON.marshal(d))))
    
    event_handler = builder.build()

    if hasattr(event_handler, "_handlers"):
        event_handler._handlers["im.message.updated_v1"] = silent_ignored_handler

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