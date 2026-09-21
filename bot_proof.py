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

# ----------------- 1. CẤU HÌNH BIẾN MÔI TRƯỜNG & SDK LARK -----------------
APP_ID = os.environ.get("APP_ID", "").strip() or os.environ.get("LARK_APP_ID", "").strip()
APP_SECRET = os.environ.get("APP_SECRET", "").strip() or os.environ.get("LARK_APP_SECRET", "").strip()
TARGET_DOMAIN = getattr(lark, "LARK_DOMAIN", "https://open.larksuite.com")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "temp_files")
ERROR_IMG_PATH = os.path.join(BASE_DIR, "gdrive_error.png")
HISTORY_FILE = os.path.join(BASE_DIR, "history_proof.json")
FFMPEG_BIN = "ffmpeg"

PROCESSED_MESSAGES = set()
THREAD_MENTIONS_CACHE = {}

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
            print(f"🌐 Đã mở cổng HTTP {port} để giữ dịch vụ Render hoạt động...")
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

# ----------------- 4. NÉN VIDEO & XỬ LÝ MEDIA -----------------
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

def convert_single_file(file_path: str) -> list[str]:
    name, ext = os.path.splitext(file_path)
    ext_lower = ext.lower()

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

    return [file_path]

def upload_file_direct(file_path: str, file_type: str) -> str:
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
            else:
                file_key = upload_file_direct(file_path, "stream")
                if file_key:
                    file_body = ReplyMessageRequestBody.builder().content(json.dumps({"file_key": file_key})).msg_type("file").reply_in_thread(True).build()
                    client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(file_body).build())

        gc.collect()
    except Exception as e:
        print(f"Lỗi xử lý gửi media: {e}")

# ----------------- 5. GIẢI MÃ LINK RÚT GỌN & GOOGLE DRIVE -----------------
def resolve_proof_url(url: str) -> str:
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

    return False

def download_proof(url: str, target_dir: str) -> bool:
    final_url = resolve_proof_url(url)
    if "drive.google.com" in final_url:
        match = re.search(r'(?:/file/d/|id=)([a-zA-Z0-9_-]+)', final_url)
        if match:
            return download_single_gdrive_file(match.group(1), target_dir)

    try:
        res = global_session.get(final_url, stream=True, timeout=90, verify=False)
        if res.status_code == 200 and "text/html" not in res.headers.get("content-type", ""):
            filename = f"proof_{len(os.listdir(target_dir)) + 1}.mp4"
            save_path = os.path.join(target_dir, filename)
            with open(save_path, "wb") as f:
                for chunk in res.iter_content(chunk_size=8 * 1024 * 1024):
                    if chunk:
                        f.write(chunk)
            return True
    except Exception:
        pass
    return False

# ----------------- 6. XỬ LÝ TIN NHẮN & KHÔI PHỤC ĐẦY ĐỦ 2 THẺ -----------------
def process_request(message_id: str, chat_id: str, text: str, sender_id: str):
    urls = re.findall(r'https?://[^\s<>"]+', text)
    order_match = re.search(r"\b(\d{15,21})\b", text)
    ticket_id = order_match.group(1) if order_match else "PROOF_DATA"

    if not urls:
        return

    req_count = get_current_request_count(ticket_id)
    task_temp_dir = os.path.join(TEMP_DIR, message_id)
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

    # ---------------- THẺ 1: BÁO CÁO DANH SÁCH FILE & LOADING ----------------
    file_list_md = []
    for item in final_files:
        file_list_md.append(f"  • {item['name']}: [{format_size(item['size'])}]")
    files_str = "\n".join(file_list_md)

    report_card_payload = {
        "elements": [
            {
                "tag": "markdown",
                "content": f"🎫 **{ticket_id}**\n╰┄▸ 💾 **{format_size(total_size)}**\n╰┄▸ 📑 {len(final_files)}/{len(final_files)}\n\n• 🎬 : {len(final_files)} file\n{files_str}\n\n⏳ *l o a d i n g .....*"
            }
        ]
    }
    reply_thread_card(message_id, report_card_payload)

    # ---------------- GỬI GỘP TỆP VÀO THREAD ----------------
    upload_and_send_batch_proofs(message_id, final_files)

    # ---------------- THẺ 2: THÔNG BÁO KẾT QUẢ HOÀN TẤT (KHÔNG CÓ NÚT BẤM) ----------------
    rabbit_side_md = "<font color='turquoise'>-ˋ (\\ (\\    .\n.(„• ֊ •„)\n─‌∪─‌∪࿎࿎</font>"
    title_side_md = "        <text_tag color='turquoise'>ᴄᴏᴍᴘʟᴇᴛᴇᴅ</text_tag>\n<text_tag color='turquoise'>-ˋˏ    𝐃𝐎𝐖𝐍𝐋𝐎𝐀𝐃 𝐏𝐑𝐎𝐎𝐅 ˎˊ-</text_tag>"
    
    sender_mention = f"<at id=\"{sender_id}\"></at>" if sender_id else "chị"
    at_middle_md = f"### <font color='carmine'>♡</font> {sender_mention} ơi...\n     ╰┄▸🎫 *<text_tag color='carmine'>{ticket_id}</text_tag>*\n"
    thankyou_center_md = "<font color='turquoise'>        ┊t h a n k y o u┊\n┈┈┈┈┈┈┈┈․° ••• °․┈┈┈┈┈┈┈┈</font>"

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
            {"tag": "div", "text": {"tag": "lark_md", "content": thankyou_center_md}, "text_align": "center"}
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

        if msg.message_type == "text":
            text = json.loads(msg.content).get("text", "")
            if "http" in text:
                threading.Thread(target=process_request, args=(msg.message_id, chat_id, text, sender_id), daemon=True).start()
    except Exception as e:
        print(f"Lỗi message: {e}")

def handle_menu_click(data: dict) -> None:
    """Xử lý sự kiện Push Event khi bấm menu Send proof"""
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

# ----------------- 7. KHỞI CHẠY WEBSOCKET LARK CLIENT -----------------
def start_bot():
    print("🚀 BOT LARK PROOF (WEBSOCKET PERSISTENT CONNECTION SẴN SÀNG)...")
    threading.Thread(target=run_dummy_web_server, daemon=True).start()

    builder = lark.EventDispatcherHandler.builder("", "")
    builder.register_p2_im_message_receive_v1(handle_message)
    builder.register_p2_application_bot_menu_v6(lambda d: handle_menu_click(json.loads(lark.JSON.marshal(d))))
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
