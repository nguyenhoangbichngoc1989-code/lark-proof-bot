import static_ffmpeg
static_ffmpeg.add_paths()

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
import base64
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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
pillow_heif.register_heif_opener()

# Tự động nhận diện linh hoạt tên biến môi trường trên Render
APP_ID = os.environ.get("APP_ID", "") or os.environ.get("LARK_APP_ID", "")
APP_SECRET = os.environ.get("APP_SECRET", "") or os.environ.get("LARK_APP_SECRET", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "temp_files")
ERROR_IMG_PATH = os.path.join(BASE_DIR, "gdrive_error.png")
HISTORY_FILE = os.path.join(BASE_DIR, "history_proof.json")
FFMPEG_BIN = "ffmpeg"

PROCESSED_MESSAGES = set()
THREAD_MENTIONS_CACHE = {}

# Connection Pooling siêu tốc
global_session = requests.Session()
retries = Retry(total=2, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
adapter = HTTPAdapter(pool_connections=20, pool_maxsize=50, max_retries=retries)
global_session.mount("https://", adapter)
global_session.mount("http://", adapter)

client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).domain(lark.LARK_DOMAIN).build()

# ----------------- SERVER HTTP GIỮ TRẠNG THÁI LIVE TRÊN RENDER -----------------
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
            print(f"🌐 Đã mở cổng HTTP {port} để giữ dịch vụ Render hoạt động...")
            httpd.serve_forever()
    except Exception as e:
        print(f"Lưu ý server HTTP: {e}")

# ----------------- QUẢN LÝ LỊCH SỬ & ĐẾM SỐ LẦN XIN -----------------
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
        fixed = filename.encode('latin1').decode('utf-8')
        return fixed
    except Exception:
        return filename

def sanitize_filename(filename: str) -> str:
    nfkd = unicodedata.normalize('NFKD', filename)
    ascii_name = re.sub(r'[^\w\s.-]', '', nfkd.encode('ASCII', 'ignore').decode('ASCII'))
    return re.sub(r'\s+', '_', ascii_name).strip()

def get_tenant_access_token() -> str:
    try:
        url = "https://open.larksuite.com/open-apis/auth/v3/tenant_access_token/internal"
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
        token = get_tenant_access_token()
        if not token:
            return
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text})
        }
        global_session.post(
            f"https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type={receive_id_type}",
            headers=headers,
            json=payload,
            timeout=10
        )
    except Exception as e:
        print(f"Lỗi gửi tin nhắn text: {e}")

# ----------------- NÉN VIDEO SIÊU TỐC & KHÔNG TIMEOUT -----------------
def compress_video_if_large(video_path: str) -> str:
    size_mb = os.path.getsize(video_path) / (1024 * 1024)
    if size_mb <= 25.0:
        return video_path

    name, ext = os.path.splitext(video_path)
    compressed_path = f"{name}_compressed.mp4"

    cmd = (
        f'"{FFMPEG_BIN}" -y -threads 2 -i "{video_path}" '
        f'-vf "scale=\'min(480,iw)\':-2,fps=20" '
        f'-c:v libx264 -preset ultrafast -crf 32 '
        f'-c:a aac -b:a 32k -ac 1 '
        f'"{compressed_path}"'
    )
    try:
        subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=300)
        gc.collect()
        if os.path.exists(compressed_path) and os.path.getsize(compressed_path) > 1000:
            return compressed_path
    except Exception as e:
        print(f"Lỗi nén video: {e}")
    return video_path

# ----------------- TẢI VIDEO TỪ YOUTUBE / SHORTS -----------------
def download_youtube_video(url: str, target_dir: str) -> bool:
    try:
        import yt_dlp
        output_template = os.path.join(target_dir, "youtube_video.mp4")
        ydl_opts = {
            'format': 'best[ext=mp4]/best',
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        for f in os.listdir(target_dir):
            if f.startswith("youtube_video") and os.path.getsize(os.path.join(target_dir, f)) > 1000:
                return True
    except Exception as e:
        print(f"Lỗi tải YouTube video: {e}")
    return False

# ----------------- CHUYỂN ĐỔI FILE (.JFIF -> .JPEG, PDF -> ẢNH) -----------------
def convert_single_file(file_path: str) -> list[str]:
    name, ext = os.path.splitext(file_path)
    ext_lower = ext.lower()
    converted_files = []

    if ext_lower == ".zip":
        try:
            os.remove(file_path)
        except Exception:
            pass
        return []

    if ext_lower == ".jfif":
        out_path = f"{name}.jpg"
        try:
            with Image.open(file_path) as img:
                img.convert("RGB").save(out_path, "JPEG", quality=85)
            os.remove(file_path)
            return [out_path]
        except Exception:
            return [file_path]

    if ext_lower in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]:
        try:
            with Image.open(file_path) as img:
                img.verify()
        except Exception:
            try:
                os.remove(file_path)
            except Exception:
                pass
            return []

    if not ext_lower or ext_lower in [".dat", ".bin"]:
        try:
            with Image.open(file_path) as img:
                fmt = img.format.lower()
                fixed_ext = ".jpg" if fmt in ["jpeg", "jfif"] else f".{fmt}"
                new_path = f"{name}{fixed_ext}"
                shutil.move(file_path, new_path)
                file_path = new_path
                name, ext = os.path.splitext(file_path)
                ext_lower = ext.lower()
        except Exception:
            pass

    if ext_lower == ".pdf":
        try:
            import pypdfium2 as pdfium
            pdf = pdfium.PdfDocument(file_path)
            for i, page in enumerate(pdf):
                image = page.render(scale=1.5).to_pil()
                page_path = f"{name}_trang_{i+1}.png"
                image.save(page_path, "PNG")
                converted_files.append(page_path)
            converted_files.append(file_path)
            return converted_files
        except Exception:
            return [file_path]

    elif ext_lower == ".heic":
        out_path = f"{name}.jpg"
        try:
            with Image.open(file_path) as img:
                img.convert("RGB").save(out_path, "JPEG", quality=85)
            os.remove(file_path)
            return [out_path]
        except Exception:
            return [file_path]

    elif ext_lower in [".webm", ".mkv"]:
        out_path = f"{name}.mp4"
        try:
            cmd = f'"{FFMPEG_BIN}" -y -threads 2 -i "{file_path}" -c:v libx264 -preset ultrafast -c:a aac "{out_path}"'
            subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                os.remove(file_path)
                return [out_path]
        except Exception:
            pass
        return [file_path]

    return [file_path]

# ----------------- UPLOAD FILE VÀ GỬI VÀO THREAD -----------------
def upload_file_direct(file_path: str, file_type: str) -> str:
    token = get_tenant_access_token()
    if not token:
        return ""

    url = "https://open.larksuite.com/open-apis/im/v1/files"
    raw_name = os.path.basename(file_path)
    safe_name = sanitize_filename(raw_name)

    headers = {"Authorization": f"Bearer {token}"}
    data = {
        "file_type": file_type,
        "file_name": safe_name
    }

    try:
        with open(file_path, "rb") as f:
            files = {"file": (safe_name, f)}
            res = global_session.post(url, headers=headers, data=data, files=files, timeout=300)
            if res.status_code == 200:
                body = res.json()
                if body.get("code") == 0:
                    return body["data"]["file_key"]
    except Exception as e:
        print(f"Lỗi upload: {e}")
    return ""

def upload_and_send_batch_proofs(message_id: str, final_files: list):
    try:
        image_keys = []
        for f in final_files:
            file_path = f["path"]
            file_ext = f["ext"]

            if file_ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]:
                with open(file_path, "rb") as img_f:
                    create_req = CreateImageRequest.builder() \
                        .request_body(CreateImageRequestBody.builder().image_type("message").image(img_f).build()) \
                        .build()
                    create_resp = client.im.v1.image.create(create_req)
                    if create_resp and create_resp.success():
                        image_keys.append(create_resp.data.image_key)

        for img_k in image_keys:
            body = ReplyMessageRequestBody.builder().content(json.dumps({"image_key": img_k})).msg_type("image").reply_in_thread(True).build()
            client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(body).build())

        for f in final_files:
            file_path = f["path"]
            file_ext = f["ext"]

            if file_ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]:
                continue

            if file_ext in [".mp4", ".mov"]:
                upload_path = compress_video_if_large(file_path)

                media_key = upload_file_direct(upload_path, "mp4")
                if media_key:
                    media_body = ReplyMessageRequestBody.builder().content(json.dumps({"file_key": media_key})).msg_type("media").reply_in_thread(True).build()
                    client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(media_body).build())

                stream_key = upload_file_direct(upload_path, "stream")
                if stream_key:
                    file_body = ReplyMessageRequestBody.builder().content(json.dumps({"file_key": stream_key})).msg_type("file").reply_in_thread(True).build()
                    client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(file_body).build())

                gc.collect()
            else:
                file_key = upload_file_direct(file_path, "stream")
                if file_key:
                    file_body = ReplyMessageRequestBody.builder().content(json.dumps({"file_key": file_key})).msg_type("file").reply_in_thread(True).build()
                    client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(file_body).build())

        gc.collect()
    except Exception as e:
        print(f"Lỗi xử lý gửi gộp media: {e}")

# ----------------- TẢI GOOGLE DRIVE TỐC ĐỘ CAO & GIẢI MÃ LINK RÚT GỌN -----------------
def check_gdrive_error(url: str) -> bool:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        res = global_session.get(url, headers=headers, timeout=6, verify=False)
        text = res.text
        return ("không thể mở tệp tại thời điểm này" in text or "unable to open the file at this time" in text.lower())
    except Exception:
        return False

def resolve_proof_url(url: str) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
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
            
            if any(k in cur_url for k in ["drive.google.com", "sharepoint.com", "1drv.ms", "onedrive.live.com", ".mp4", ".png", ".jpg"]):
                return cur_url

            html = r.text
            meta_match = re.search(r'<meta[^>]*?content=["\']\d+;\s*url=([^"\'>\s]+)["\']', html, re.IGNORECASE)
            if meta_match:
                cur_url = urllib.parse.urljoin(cur_url, meta_match.group(1).replace("&amp;", "&"))
                continue

            dest_match = re.search(r'["\'](https?://(?:drive\.google\.com|[^"\']*?\.(?:mp4|mov|jpg|png))[^"\']*)["\']', html)
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

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "*/*"
    }
    try:
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
        res = global_session.get(url, headers=headers, stream=True, verify=False, timeout=25)
        
        confirm_token = None
        for k, v in res.cookies.items():
            if k.startswith("download_warning"):
                confirm_token = v
                break
        if not confirm_token:
            match = re.search(r'confirm=([0-9A-Za-z_]+)', res.text)
            if match:
                confirm_token = match.group(1)
        if not confirm_token:
            match_uuid = re.search(r'name="uuid"\s+value="([^"]+)"', res.text)
            if match_uuid:
                confirm_token = match_uuid.group(1)

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
        print(f"Session download: {e}")

    try:
        import gdown
        output = gdown.download(id=file_id, output=save_path, quiet=True)
        if output and os.path.exists(output) and os.path.getsize(output) > 2000:
            return True
    except Exception:
        pass

    return False

def download_gdrive_folder_files(folder_url: str, target_dir: str) -> bool:
    folder_match = re.search(r'/folders/([a-zA-Z0-9_-]+)', folder_url)
    folder_id = folder_match.group(1) if folder_match else ""
    clean_folder_url = f"https://drive.google.com/drive/folders/{folder_id}" if folder_id else folder_url.split("?")[0]

    try:
        import gdown
        downloaded = gdown.download_folder(clean_folder_url, output=target_dir, quiet=True, use_cookies=False)
        if downloaded and len(downloaded) > 0:
            return True
    except Exception:
        pass

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        res = global_session.get(clean_folder_url, headers=headers, timeout=20, verify=False)
        if res.status_code == 200:
            html = res.text
            found_files = {}

            matches = re.findall(r'\["([a-zA-Z0-9_-]{28,45})",\["([^"]+)"', html)
            for fid, fname in matches:
                fname_clean = clean_file_display_name(fname)
                if any(ext in fname_clean.lower() for ext in [".jfif", ".mp4", ".mov", ".avi", ".mkv", ".jpg", ".png", ".jpeg", ".webp", ".pdf"]):
                    found_files[fid] = fname_clean

            if found_files:
                success_count = 0
                for fid, fname in found_files.items():
                    if download_single_gdrive_file(fid, target_dir, fname):
                        success_count += 1
                return success_count > 0
    except Exception as e:
        print(f"Lỗi phân tích Folder GDrive: {e}")

    return False

# ----------------- HÀM TẢI FILE TỔNG HỢP -----------------
def download_proof(url: str, target_dir: str) -> bool:
    final_url = resolve_proof_url(url)

    if "drive.google.com" in final_url:
        if "/folders/" in final_url:
            return download_gdrive_folder_files(final_url, target_dir)
        else:
            file_id_match = re.search(r'(?:/file/d/|id=)([a-zA-Z0-9_-]+)', final_url)
            if file_id_match:
                return download_single_gdrive_file(file_id_match.group(1), target_dir)

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        res = global_session.get(final_url, headers=headers, stream=True, timeout=90, verify=False)
        if res.status_code == 200:
            c_type = res.headers.get("content-type", "")
            if "text/html" in c_type and not any(ext in final_url.lower() for ext in [".mp4", ".png", ".jpg", ".mov", ".jfif"]):
                return False

            parsed = urllib.parse.urlparse(final_url)
            filename = os.path.basename(parsed.path)
            if not filename or "." not in filename:
                if "video" in c_type or "mp4" in final_url:
                    filename = f"video_proof_{len(os.listdir(target_dir)) + 1}.mp4"
                elif "image" in c_type:
                    filename = f"image_proof_{len(os.listdir(target_dir)) + 1}.jpg"
                else:
                    filename = f"proof_{len(os.listdir(target_dir)) + 1}.mp4"

            save_path = os.path.join(target_dir, filename)
            with open(save_path, "wb") as f:
                for chunk in res.iter_content(chunk_size=8 * 1024 * 1024):
                    if chunk:
                        f.write(chunk)
            return True
        return False
    except Exception as e:
        print(f"Lỗi tải trực tiếp: {e}")
        return False

# ----------------- XỬ LÝ SỰ KIỆN MENU & TRANSFER PROOF -----------------
def handle_menu_click(data) -> dict:
    """Bắt sự kiện khi bấm nút menu 'Send proof' có event_key = trigger_proof_template"""
    try:
        raw_body = json.loads(data.event) if isinstance(data.event, str) else data.event
        event_obj = raw_body.get("event", {})
        event_key = event_obj.get("event_key", "")
        
        operator = event_obj.get("operator", {})
        operator_id = operator.get("operator_id", {})
        open_id = operator_id.get("open_id", "") or operator.get("open_id", "")

        if event_key == "trigger_proof_template":
            template = (
                "📋 TEMPLATES CHECK PROOF \n\n"
                "Hãy copy đoạn bên dưới, dán vào ô chat rồi thêm Ticket_ID & link proof nhé:\n\n"
                "Take proof & hold after confirmation\n"
                "7686040088400168978\n"
                "https://bom.so/sf8t5L"
            )
            if open_id:
                send_text_message(open_id, template, receive_id_type="open_id")
    except Exception as e:
        print(f"Lỗi xử lý menu click: {e}")
    return {}

def execute_transfer_proof(message_id: str, chat_id: str, operator_id: str, ticket_id: str):
    token = get_tenant_access_token()
    if not token:
        return

    headers = {"Authorization": f"Bearer {token}"}
    target_user_id = None

    cached_mentions = THREAD_MENTIONS_CACHE.get(message_id, [])
    if cached_mentions:
        target_user_id = cached_mentions[-1]

    if not target_user_id:
        try:
            url_search = f"https://open.larksuite.com/open-apis/im/v1/messages/{message_id}"
            res = global_session.get(url_search, headers=headers, timeout=10)
            if res.status_code == 200:
                data_items = res.json().get("data", {}).get("items", [])
                if data_items:
                    for m in data_items[0].get("mentions", []):
                        m_id = m.get("id")
                        if m_id and m_id != APP_ID and m_id != operator_id:
                            target_user_id = m_id
                            break
        except Exception as e:
            print(f"Lỗi đọc tin nhắn thread: {e}")

    if target_user_id:
        thread_link = f"https://applink.larksuite.com/client/message/detail?openChatId={chat_id}&messageId={message_id}"

        dm_card_payload = {
            "header": {
                "template": "blue",
                "title": {
                    "tag": "plain_text",
                    "content": "🔔 ĐIỀU CHUYỂN PROOF TICKET MỚI"
                }
            },
            "elements": [
                {
                    "tag": "markdown",
                    "content": (
                        f"👋 Chào bạn, bạn vừa được <at id=\"{operator_id}\"></at> chuyển giao xử lý Proof cho Ticket:\n\n"
                        f"🎫 **Ticket ID:** `{ticket_id}`\n\n"
                        f"📌 Vui lòng bấm vào nút bên dưới để chuyển thẳng đến Thread chứng từ kiểm tra tệp:"
                    )
                },
                {
                    "tag": "action",
                    "actions": [
                        {
                            "tag": "button",
                            "text": {
                                "tag": "plain_text",
                                "content": "👉 Mở Ngay Thread Proof"
                            },
                            "type": "primary",
                            "url": thread_link
                        }
                    ]
                }
            ]
        }

        try:
            forward_dm_body = {
                "receive_id": target_user_id,
                "msg_type": "interactive",
                "content": json.dumps(dm_card_payload)
            }
            global_session.post(
                "https://open.larksuite.com/open-apis/im/v1/messages?receive_id_type=open_id",
                headers=headers,
                json=forward_dm_body,
                timeout=10
            )
        except Exception as e:
            print(f"Lỗi gửi tin nhắn riêng cho nhân viên: {e}")

        confirm_thread_text = f"📨 Deve đã chuyển Proof hoàn tất đến <at id=\"{target_user_id}\"></at> ạ!"
        try:
            body = ReplyMessageRequestBody.builder() \
                .content(json.dumps({"text": confirm_thread_text})) \
                .msg_type("text") \
                .reply_in_thread(True) \
                .build()
            req = ReplyMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(body) \
                .build()
            client.im.v1.message.reply(req)
        except Exception as e:
            print(f"Lỗi phản hồi xác nhận vào Thread: {e}")

    else:
        warning_msg = (
            "⚠️ <text_tag color='carmine'>𝐂𝐡𝐮̛𝐚 𝐜𝐨́ 𝐧𝐡𝐚̂𝐧 𝐯𝐢𝐞̂𝐧 đ𝐮̛𝐨̛̣𝐜 𝐠𝐚̆́𝐧 𝐭𝐡𝐞̉!</text_tag>\n\n"
            "Chị vui lòng **@tên_nhân_viên** vào Thread này trước, sau đó bấm lại nút **Transfer Proof** nhé!"
        )
        reply_thread_card(message_id, {
            "elements": [{"tag": "markdown", "content": warning_msg}]
        })

# ----------------- XỬ LÝ CHÍNH & RENDER THẺ -----------------
def process_request(message_id: str, chat_id: str, text: str, sender_id: str):
    urls = re.findall(r'https?://[^\s<>"]+', text)

    ticket_id = "N/A"
    proof_urls = []

    prefix_match = re.search(r"tickets?\s*id\s*[:\-_]?\s*(\d{15,21})", text, re.IGNORECASE)
    if prefix_match:
        ticket_id = prefix_match.group(1)
    else:
        csp_match = re.search(r"ticket\/detail\/(\d+)", text)
        if csp_match:
            ticket_id = csp_match.group(1)
        else:
            raw_id_match = re.search(r"\b(\d{15,21})\b", text)
            if raw_id_match:
                ticket_id = raw_id_match.group(1)

    for u in urls:
        if "csp.byteintl.com" not in u:
            proof_urls.append(u)

    if ticket_id == "N/A":
        reply_thread_card(message_id, {
            "elements": [{"tag": "markdown", "content": "<text_tag color='carmine'>⚠️ Vui lòng cung cấp Ticket ID (Ví dụ: **Tickets ID: 7684572634759235592**)!</text_tag>"}]
        })
        return

    if not proof_urls:
        reply_thread_card(message_id, {
            "elements": [{"tag": "markdown", "content": "<text_tag color='carmine'>⚠️ Không tìm thấy link Proof đính kèm nào!</text_tag>"}]
        })
        return

    req_count = get_current_request_count(ticket_id)

    for u in proof_urls:
        if "drive.google.com" in u and check_gdrive_error(u):
            card_err = {
                "elements": [
                    {
                        "tag": "markdown",
                        "content": f"🎫 <text_tag color='turquoise'>𝐓𝐢𝐜𝐤𝐞𝐭_𝐈𝐃:</text_tag> `{ticket_id}`\n\n<text_tag color='carmine'>Rất tiếc, không thể mở tệp tại thời điểm này.</text_tag>\n 🚨*Vui lòng kiểm tra lại địa chỉ liên kết và quyền chia sẻ của tệp Google Drive!*"
                    }
                ]
            }
            reply_thread_card(message_id, card_err)
            if os.path.exists(ERROR_IMG_PATH):
                upload_and_send_batch_proofs(message_id, [{"name": "error.png", "path": ERROR_IMG_PATH, "ext": ".png"}])
            return

    task_temp_dir = os.path.join(TEMP_DIR, message_id)
    shutil.rmtree(task_temp_dir, ignore_errors=True)
    os.makedirs(task_temp_dir, exist_ok=True)

    for u in proof_urls:
        download_proof(u, task_temp_dir)

    final_files = []
    for root, _, fs in os.walk(task_temp_dir):
        for f in fs:
            raw_path = os.path.join(root, f)
            if raw_path.lower().endswith(".zip") or os.path.getsize(raw_path) <= 100:
                continue
            converted = convert_single_file(raw_path)
            for cp in converted:
                if cp.lower().endswith(".zip") or os.path.getsize(cp) <= 100:
                    continue
                final_files.append({
                    "name": clean_file_display_name(os.path.basename(cp)),
                    "path": cp,
                    "size": os.path.getsize(cp),
                    "ext": os.path.splitext(cp)[1].lower()
                })

    if not final_files:
        reply_thread_card(message_id, {
            "elements": [{"tag": "markdown", "content": "<text_tag color='carmine'>🚨 Không thể tải dữ liệu từ link Proof (link bị khóa quyền hoặc lỗi tải). Vui lòng kiểm tra lại!</text_tag>"}]
        })
        shutil.rmtree(task_temp_dir, ignore_errors=True)
        return

    record_successful_request(ticket_id, req_count)
    total_size = sum(x["size"] for x in final_files)

    categorized = {}
    for x in final_files:
        ext = x["ext"]
        if ext in [".mp4", ".mov", ".mkv", ".webm"]:
            icon = "🎬"
        elif ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]:
            icon = "🖼️"
        elif ext in [".pdf", ".docx", ".xlsx", ".csv", ".txt"]:
            icon = "📄"
        else:
            icon = "📁"
        categorized.setdefault(icon, []).append(x)

    type_block_lines = []
    for icon, items in categorized.items():
        type_block_lines.append(f"• {icon}: {len(items)} file")
        for item in items:
            type_block_lines.append(f"        •  {item['name']}: [{format_size(item['size'])}]")
    type_content = "\n".join(type_block_lines)

    # THẺ 1: BÁO CÁO BAN ĐẦU
    card_element_top = (
        f"🎫 {ticket_id}\n"
        f" ╰┄▸💾 {format_size(total_size)}\n"
        f"     ╰┄▸📑 {len(final_files)}/{len(final_files)}\n\n"
        f"{type_content}\n\n"
    )

    loading_styled = "⏳️ <font color='yellow'> 𝐥 𝐨 𝐚 𝐝 𝐢 𝐧 𝐠 ..... </font>"
    right_badge_styled = f"<text_tag color='turquoise'>✎ᝰ┆</text_tag> <text_tag color='carmine'>[№ {req_count}]</text_tag>"

    report_card_payload = {
        "elements": [
            {
                "tag": "markdown",
                "content": card_element_top
            },
            {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "default",
                "columns": [
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 3,
                        "elements": [{"tag": "markdown", "content": loading_styled}]
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 2,
                        "horizontal_align": "right",
                        "elements": [{"tag": "markdown", "content": right_badge_styled}]
                    }
                ]
            }
        ]
    }
    reply_thread_card(message_id, report_card_payload)

    # GỬI TỆP VÀO THREAD
    upload_and_send_batch_proofs(message_id, final_files)

    # THẺ 2: HOÀN TẤT
    rabbit_side_md = "<font color='turquoise'>-ˋ (\\ (\\    .\n.(„• ֊ •„)\n─‌∪─‌∪࿎࿎</font>"
    title_side_md = "        <text_tag color='turquoise'>ᴄᴏᴍᴘʟᴇᴛᴇᴅ</text_tag>\n<text_tag color='turquoise'>-ˋˏ    𝐃𝐎𝐖𝐍𝐋𝐎𝐀𝐃 𝐏𝐑𝐎OF ˎˊ-</text_tag>"
    at_middle_md = f"### <font color='carmine'>♡</font> <at id=\"{sender_id}\"></at> ơi...\n     ╰┄▸🎫 *<text_tag color='carmine'>{ticket_id}</text_tag>*\n"
    thankyou_center_md = "<font color='turquoise'> ┊t h a n k y o u┊\n┈┈┈┈┈┈┈┈․° ••• °․┈┈┈┈┈┈┈┈</font>"

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
            {"tag": "markdown", "content": at_middle_md},
            {"tag": "div", "text": {"tag": "lark_md", "content": thankyou_center_md}, "text_align": "center"},
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "🚀 Transfer Proof"},
                        "type": "primary",
                        "value": {
                            "action": "transfer_proof",
                            "ticket_id": ticket_id,
                            "root_msg_id": message_id,
                            "chat_id": chat_id
                        }
                    }
                ]
            }
        ]
    }
    reply_thread_card(message_id, finish_card_payload)

    shutil.rmtree(task_temp_dir, ignore_errors=True)
    gc.collect()

def handle_message(data: lark.im.v1.P2MessageReceiveV1) -> None:
    try:
        event = data.event
        msg = event.message

        if msg.message_id in PROCESSED_MESSAGES:
            return
        PROCESSED_MESSAGES.add(msg.message_id)
        if len(PROCESSED_MESSAGES) > 500:
            PROCESSED_MESSAGES.pop()

        chat_id = msg.chat_id or ""
        thread_root_id = msg.root_id or msg.parent_id or msg.message_id

        if msg.mentions:
            THREAD_MENTIONS_CACHE.setdefault(thread_root_id, [])
            for m in msg.mentions:
                if m.key and m.id and m.id.open_id:
                    open_id = m.id.open_id
                    if open_id != APP_ID:
                        THREAD_MENTIONS_CACHE[thread_root_id].append(open_id)

        if msg.message_type == "text":
            content = json.loads(msg.content)
            text = content.get("text", "")
            sender_id = event.sender.sender_id.open_id if (event.sender and event.sender.sender_id) else ""
            
            if "http://" in text or "https://" in text:
                threading.Thread(target=process_request, args=(msg.message_id, chat_id, text, sender_id), daemon=True).start()
    except Exception as e:
        print(f"Lỗi handle_message: {e}")

def handle_card_action(data: lark.CustomizedEvent) -> dict:
    try:
        raw_body = json.loads(data.event) if isinstance(data.event, str) else data.event
        action_value = raw_body.get("action", {}).get("value", {})
        operator_id = raw_body.get("operator", {}).get("open_id", "")
        
        if action_value.get("action") == "transfer_proof":
            ticket_id = action_value.get("ticket_id", "N/A")
            root_msg_id = action_value.get("root_msg_id", "")
            chat_id = action_value.get("chat_id", "")
            
            threading.Thread(
                target=execute_transfer_proof, 
                args=(root_msg_id, chat_id, operator_id, ticket_id), 
                daemon=True
            ).start()
            
            return {"toast": {"type": "info", "content": "Đang chuyển giao Proof đến nhân viên..."}}
    except Exception as e:
        print(f"Lỗi xử lý button action: {e}")
    return {}

def silent_ignored_handler(data) -> None:
    pass

def start_bot():
    print("=" * 60)
    print("🚀 BOT LARK PROOF (PHIÊN BẢN CLOUD TĂNG TỐC & MENU EVENT 2026)...")
    print("=" * 60)
    
    threading.Thread(target=run_dummy_web_server, daemon=True).start()

    builder = lark.EventDispatcherHandler.builder("", "")
    builder.register_p2_im_message_receive_v1(handle_message)
    builder.register_p1_customized_event("card.action.trigger", handle_card_action)
    
    # Đăng ký bắt sự kiện Push Event từ Menu Bot (application.bot.menu_v6)
    builder.register_p1_customized_event("application.bot.menu_v6", handle_menu_click)
    
    event_handler = builder.build()

    if hasattr(event_handler, "_handlers"):
        event_handler._handlers["im.message.updated_v1"] = silent_ignored_handler

    ws_client = lark.ws.Client(
        APP_ID, 
        APP_SECRET, 
        event_handler=event_handler, 
        domain=lark.LARK_DOMAIN, 
        log_level=lark.LogLevel.INFO
    )
    ws_client.start()

if __name__ == "__main__":
    start_bot()
