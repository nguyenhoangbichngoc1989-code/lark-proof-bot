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

# ----------------- 1. CẤU HÌNH BIẾN MÔI TRƯỜNG & SDK LARK -----------------
APP_ID = os.environ.get("APP_ID", "").strip() or os.environ.get("LARK_APP_ID", "").strip()
APP_SECRET = os.environ.get("APP_SECRET", "").strip() or os.environ.get("LARK_APP_SECRET", "").strip()
TARGET_DOMAIN = getattr(lark, "LARK_DOMAIN", "https://open.larksuite.com")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "temp_files")
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

# ----------------- 2. SERVER HTTP DUY TRÌ RENDER (TỐI ƯU CHO CRON-JOB) -----------------
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
        with socketserver.TCPServer(("", port), RenderHealthHandler) as httpd:
            print(f"🌐 Đã mở cổng HTTP {port} để duy trì Render...")
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

def add_reaction_to_message(message_id: str, emoji_type: str = "KeepYourSpiritsAwake"):
    """Chỉ thả reaction bé rắn khi toàn bộ media đã bung thành công vào thread"""
    token = get_tenant_access_token()
    if not token or not message_id:
        return

    url = f"{TARGET_DOMAIN}/open-apis/im/v1/messages/{message_id}/reactions"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8"
    }
    data = {
        "reaction_type": {
            "emoji_type": emoji_type
        }
    }
    try:
        res = global_session.post(url, headers=headers, json=data, timeout=10)
        if res.status_code == 200:
            print(f"🐍 Đã hoàn tất 100%! Auto reaction [{emoji_type}] vào message_id: {message_id}")
    except Exception as e:
        print(f"Lỗi gọi API reaction: {e}")

# ----------------- 4. NÉN SIÊU TỐC VÀ CHUYỂN ĐỔI VIDEO -----------------
def compress_and_convert_video(video_path: str, original_name: str = "") -> str:
    try:
        dir_name = os.path.dirname(video_path)
        ext = os.path.splitext(video_path)[1].lower()

        safe_in = os.path.join(dir_name, "temp_render_in" + ext)
        if not os.path.exists(safe_in):
            shutil.copy2(video_path, safe_in)

        base_name = os.path.splitext(original_name or os.path.basename(video_path))[0]
        clean_base = sanitize_filename(base_name)
        out_path = os.path.join(dir_name, f"{clean_base}.mp4")

        cmd = [
            FFMPEG_EXEC, "-y", "-nostdin",
            "-threads", "2",
            "-i", safe_in,
            "-vf", "fps=12,scale='min(320,iw)':-2",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "38",
            "-pix_fmt", "yuv420p",
            "-an",
            "-movflags", "+faststart",
            out_path
        ]
        
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=75)
        gc.collect()

        if proc.returncode == 0 and os.path.exists(out_path):
            new_size = os.path.getsize(out_path) / (1024 * 1024)
            if new_size > 0 and new_size <= 28.0:
                print(f"⚡ Nén video thành công: {os.path.basename(out_path)} ({new_size:.2f} MB)")
                return out_path
    except Exception as e:
        print(f"Lỗi nén video: {e}")
    return video_path

def upload_lark_file(file_path: str, file_type: str = "stream") -> str:
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
    except Exception as e:
        print(f"Lỗi upload: {e}")
    return ""

def upload_and_send_batch_proofs(message_id: str, final_files: list) -> int:
    actual_sent_count = 0

    for f in final_files:
        file_path = f["path"]
        file_ext = f["ext"]
        file_name = f["name"]

        try:
            # 1. Hình ảnh
            if file_ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]:
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
            elif file_ext in [".mp4", ".mov", ".avi", ".mkv"]:
                send_path = compress_and_convert_video(file_path, original_name=file_name)
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
                        print(f"📥 Đã bung video: {os.path.basename(send_path)}")

            # 3. Tệp khác
            else:
                if os.path.getsize(file_path) / (1024 * 1024) <= 28.0:
                    file_key = upload_lark_file(file_path, "stream")
                    if file_key:
                        file_body = ReplyMessageRequestBody.builder().content(json.dumps({"file_key": file_key})).msg_type("file").reply_in_thread(True).build()
                        resp = client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(file_body).build())
                        if resp and resp.success():
                            actual_sent_count += 1
        except Exception as e:
            print(f"Lỗi gửi media: {e}")

        gc.collect()

    return actual_sent_count

# ----------------- 5. GIẢI MÃ LINK (HỖ TRỢ GDRIVE, SHAREPOINT, ONEDRIVE) -----------------
def resolve_proof_url(url: str) -> str:
    if any(ext in url.lower() for ext in [".mp4", ".mov", ".png", ".jpg", ".jfif", ".webm"]):
        return url

    # Tự động chuyển link tệp SharePoint / OneDrive sang link tải trực tiếp
    if "sharepoint.com" in url or "1drv.ms" in url:
        if ":f:/" not in url:
            sep = "&" if "?" in url else "?"
            if "download=1" not in url:
                return f"{url}{sep}download=1"
        return url

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    cur_url = url

    if "acesse.one" in cur_url or "encurtador.dev" in cur_url:
        try:
            code = cur_url.rstrip("/").split("/")[-1]
            api_url = f"https://encurtador.dev/api/link/{code}"
            r_api = global_session.get(api_url, headers=headers, timeout=6, verify=False)
            if r_api.status_code == 200:
                data = r_api.json()
                dest = data.get("link", {}).get("destination") or data.get("destination") or data.get("url")
                if dest:
                    return dest
        except Exception:
            pass

    if "bom.so" in cur_url:
        try:
            r_bom = global_session.get(cur_url, headers=headers, allow_redirects=True, timeout=8, verify=False)
            if r_bom.url != cur_url:
                return r_bom.url
        except Exception:
            pass

    for _ in range(3):
        try:
            r = global_session.get(cur_url, headers=headers, allow_redirects=True, timeout=10, verify=False)
            if r.url != cur_url:
                cur_url = r.url
            if any(k in cur_url for k in ["drive.google.com", "sharepoint.com", ".mp4", ".mov", ".png", ".jpg"]):
                return cur_url

            meta_match = re.search(r'<meta[^>]*?content=["\']\d+;\s*url=([^"\'>\s]+)["\']', r.text, re.IGNORECASE)
            if meta_match:
                cur_url = urllib.parse.urljoin(cur_url, meta_match.group(1).replace("&amp;", "&"))
                continue
            break
        except Exception:
            break

    return cur_url

def download_single_gdrive_file(file_id: str, target_dir: str, preferred_name: str = "") -> bool:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

    try:
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
        res = global_session.get(url, headers=headers, stream=True, verify=False, timeout=20)
        
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
            res = global_session.get(url, headers=headers, stream=True, verify=False, timeout=90)

        content_disposition = res.headers.get("Content-Disposition", "")
        extracted_name = ""
        if "filename=" in content_disposition:
            fn_match = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';]+)["\']?', content_disposition)
            if fn_match:
                try:
                    extracted_name = urllib.parse.unquote(fn_match.group(1))
                except Exception:
                    extracted_name = fn_match.group(1)

        raw_save_name = preferred_name or extracted_name or f"gdrive_{file_id}.mp4"
        clean_save_name = sanitize_filename(raw_save_name)
        save_path = os.path.join(target_dir, clean_save_name)

        if res.status_code == 200 and "text/html" not in res.headers.get("Content-Type", ""):
            with open(save_path, "wb") as f:
                for chunk in res.iter_content(chunk_size=4 * 1024 * 1024):
                    if chunk:
                        f.write(chunk)
            if os.path.exists(save_path) and os.path.getsize(save_path) > 2000:
                return True
    except Exception as e:
        print(f"Lỗi tải Drive: {e}")

    try:
        import gdown
        clean_save_name = sanitize_filename(preferred_name or f"gdrive_{file_id}.mp4")
        fallback_path = os.path.join(target_dir, clean_save_name)
        output = gdown.download(id=file_id, output=fallback_path, quiet=True)
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
        res = global_session.get(clean_url, headers=headers, timeout=15, verify=False)
        if res.status_code == 200:
            html = res.text
            found_files = {}

            matches = re.findall(r'\["([a-zA-Z0-9_-]{28,45})",\["([^"]+)"', html)
            for fid, fname in matches:
                if any(ext in fname.lower() for ext in [".jfif", ".mp4", ".mov", ".avi", ".mkv", ".jpg", ".png", ".jpeg", ".webp"]):
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
    
    # 1. Google Drive
    if "drive.google.com" in final_url:
        if "/folders/" in final_url:
            return download_gdrive_folder(final_url, target_dir)
        else:
            match = re.search(r'(?:/file/d/|id=)([a-zA-Z0-9_-]{25,50})', final_url)
            if match:
                return download_single_gdrive_file(match.group(1), target_dir)

    # 2. Tải trực tiếp (Bao gồm file đơn lẻ SharePoint có ?download=1)
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        res = global_session.get(final_url, headers=headers, stream=True, timeout=90, verify=False)
        
        # Nếu link trả về HTML thì không phải file tải trực tiếp
        if "text/html" in res.headers.get("Content-Type", ""):
            return False

        parsed_url = urllib.parse.urlparse(final_url)
        path_name = os.path.basename(parsed_url.path)
        raw_name = path_name if (path_name and "." in path_name) else f"video_{len(os.listdir(target_dir)) + 1}.mp4"
        save_name = sanitize_filename(raw_name)
        save_path = os.path.join(target_dir, save_name)

        if res.status_code == 200:
            with open(save_path, "wb") as f:
                for chunk in res.iter_content(chunk_size=4 * 1024 * 1024):
                    if chunk:
                        f.write(chunk)
            if os.path.exists(save_path) and os.path.getsize(save_path) > 1000:
                return True
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
    try:
        req_count = get_current_request_count(ticket_id)
        task_temp_dir = os.path.join(TEMP_DIR, f"{message_id}_{ticket_id}")
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
                        "name": f,
                        "path": raw_path,
                        "size": os.path.getsize(raw_path),
                        "ext": os.path.splitext(f)[1].lower()
                    })

        # Nếu không tải được tệp nào về máy
        if not final_files:
            first_url = urls[0] if urls else ""
            # Kiểm tra nếu là thư mục SharePoint / OneDrive thì gửi thẻ hướng dẫn xem trực tiếp
            if "sharepoint.com" in first_url or "1drv.ms" in first_url:
                sharepoint_card = {
                    "elements": [
                        {
                            "tag": "markdown",
                            "content": (
                                f"📁 **ĐƠN HÀNG: {ticket_id}**\n\n"
                                f"<font color='orange'>⚠️ Link được chia sẻ là **Thư mục SharePoint nội bộ**, bot không thể tải tự động do cơ chế bảo mật của Microsoft.</font>\n\n"
                                f"👉 [**Bấm vào đây để mở trực tiếp Thư mục SharePoint**]({first_url})\n\n"
                                f"<font color='grey'>💡 *Mẹo: Nếu muốn bot bung video trực tiếp vào thread, hãy bấm vào file video trong thư mục và Copy liên kết của riêng file đó nhé!*</font>"
                            )
                        }
                    ]
                }
                reply_thread_card(message_id, sharepoint_card)
            else:
                reply_thread_card(message_id, {
                    "elements": [{"tag": "markdown", "content": f"<text_tag color='carmine'>🚨 Không thể tải video của đơn {ticket_id}, vui lòng kiểm tra lại quyền truy cập link!</text_tag>"}]
                })
            shutil.rmtree(task_temp_dir, ignore_errors=True)
            return

        record_successful_request(ticket_id, req_count)
        total_size = sum(x["size"] for x in final_files)
        file_count = len(final_files)

        # ---------------- THẺ 1: BÁO CÁO BAN ĐẦU (9 KHOẢNG TRẮNG CĂN LỀ ╰┄‌•) ----------------
        file_lines = []
        for item in final_files:
            file_lines.append(f"         <font color='carmine'>╰┄‌•  </font>{item['name']}: <text_tag color='carmine'>[{format_size(item['size'])}]</text_tag>")
        files_str = "\n".join(file_lines)

        header_block = (
            f"*<font color='turquoise'>          ≽^•⩊•^≼  </font>*\n"
            f"*<font color='turquoise'> ✧; Ｗｅｌｃｏｍｅ ;✧</font>*\n\n"
            f"🎫<text_tag color='turquoise'>{ticket_id}</text_tag>\n"
            f"   ╰┄▸ 💾<text_tag color='carmine'>{format_size(total_size)}</text_tag>\n"
            f"         ╰┄▸ 🗂️ <text_tag color='indigo'>{file_count}/{file_count}</text_tag>\n\n"
            f"• 🎬 : {file_count} file\n"
            f"{files_str}\n\n"
            f"⌛*<text_tag color='yellow'>Ｌｏａｄｉｎｇ．．．███████▒▒▒ 8O %</text_tag>*"
        )

        reply_thread_card(message_id, {"elements": [{"tag": "markdown", "content": header_block}]})

        # ---------------- BUNG TỆP VÀO THREAD VÀ ĐẾM SỐ LƯỢNG THÀNH CÔNG THỰC TẾ ----------------
        actual_bung_success = upload_and_send_batch_proofs(message_id, final_files)

        # ---------------- THẺ 2: KẾT QUẢ IN ĐẬM VÀ CĂN GIỮA TUYỆT ĐỐI ----------------
        rabbit_side_md = "<font color='turquoise'>-ˋ (\\ (\\    .\n.(„• ֊ •„)\n─‌∪─‌∪࿎࿎</font>"
        title_side_md = "        <text_tag color='turquoise'>ᴄᴏᴍᴘʟᴇᴛᴇᴅ</text_tag>\n<text_tag color='turquoise'>-ˋˏ    𝐃𝐎𝐖𝐍𝐋𝐎𝐀𝐃 𝐏𝐑𝐎OF ˎˊ-</text_tag>"
        sender_mention = f"<at id=\"{sender_id}\"></at>" if sender_id else "chị"
        
        heading_md = f"<font color='carmine'>**♡ {sender_mention} ơi...</font>**\n      ╰┄▸ 🎫 *<text_tag color='carmine'>{ticket_id}</text_tag>*"
        thankyou_md = "<font color='turquoise'>      ┊ t h a n k y o u ┊\n┈┈┈┈┈┈┈┈․° ••• °․┈┈┈┈┈┈┈┈</font>"

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
                {"tag": "markdown", "content": heading_md},
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": thankyou_md},
                    "text_align": "center"
                }
            ]
        }
        reply_thread_card(message_id, finish_card_payload)

        # ---------------- AUTO REACTION BÉ RẮN SAU KHI ĐÃ BUNG ĐẦY ĐỦ 100% VÀO THREAD ----------------
        if actual_bung_success > 0 and actual_bung_success >= len(final_files):
            add_reaction_to_message(message_id, "KeepYourSpiritsAwake")
        else:
            print(f"⚠️ Chưa bung đủ media ({actual_bung_success}/{len(final_files)}) -> Không thả reaction!")

        shutil.rmtree(task_temp_dir, ignore_errors=True)
        gc.collect()

    except Exception as e:
        print(f"Lỗi trong process_single_task: {e}")

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
            t = threading.Thread(target=parse_and_dispatch, args=(msg.message_id, chat_id, text, sender_id))
            t.daemon = True
            t.start()
    except Exception as e:
        print(f"Lỗi message: {e}")

def silent_ignored_handler(data) -> None:
    pass

# ----------------- 8. KHỞI CHẠY WEBSOCKET LARK CLIENT -----------------
def start_bot():
    print("🚀 BOT LARK PROOF (TỐI ƯU SIÊU TỐC & KHÔNG NGHẼN CPU)...")
    threading.Thread(target=run_dummy_web_server, daemon=True).start()

    builder = lark.EventDispatcherHandler.builder("", "")
    builder.register_p2_im_message_receive_v1(handle_message)
    
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
