import os
import sys
import json
import re
import time
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Import SDK Lark Suite
try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import *
except ImportError:
    print("Vui long cai dat lark-oapi: pip install lark-oapi")
    sys.exit(1)

# ==========================================
# 1. CẤU HÌNH BIẾN MÔI TRƯỜNG & DOMAIN LARK
# ==========================================
APP_ID = os.environ.get("LARK_APP_ID", "").strip()
APP_SECRET = os.environ.get("LARK_APP_SECRET", "").strip()
PORT = int(os.environ.get("PORT", 10000))

TARGET_DOMAIN = getattr(lark, "LARK_DOMAIN", "https://open.larksuite.com")

client = lark.Client.builder() \
    .app_id(APP_ID) \
    .app_secret(APP_SECRET) \
    .domain(TARGET_DOMAIN) \
    .log_level(lark.LogLevel.INFO) \
    .build()

# ==========================================
# 2. HTTP SERVER PHỤ DUY TRÌ CỔNG RENDER
# ==========================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Bot Proof Lark Suite is Live and Running!".encode("utf-8"))

    def log_message(self, format, *args):
        return

def run_dummy_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    print(f"Da mo cong HTTP {PORT} de duy tri dich vu Render...")
    server.serve_forever()

# ==========================================
# 3. HÀM GỬI THẺ TƯƠNG TÁC (ĐÃ BỎ HOÀN TOÀN NÚT BẤM)
# ==========================================
def send_detailed_card(receive_id: str, receive_id_type: str, order_id: str, file_name: str, file_size_mb: float):
    """Gửi Thẻ tương tác hiển thị thông tin mã đơn, tên file và dung lượng (không có button)"""
    try:
        card_content = {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "🎫 **{order_id}**\n—> 💾 **{file_size} MB**\n—> 📄 1/1".format(order_id=order_id, file_size=file_size_mb)
                    }
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "• 🎬 : 1 file\n  • {file_name}: [{file_size} MB]".format(file_name=file_name, file_size=file_size_mb)
                    }
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "⏳ *l o a d i n g .....*"
                    }
                }
            ]
        }

        req = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(CreateMessageRequestBody.builder()
                          .receive_id(receive_id)
                          .msg_type("interactive")
                          .content(json.dumps(card_content))
                          .build()) \
            .build()
        
        resp = client.im.v1.message.create(req)
        if not resp.success():
            print(f"Gui card that bai: code={resp.code}, msg={resp.msg}")
    except Exception as e:
        print(f"Loi gui interactive card: {e}")

def send_text_msg(receive_id: str, receive_id_type: str, content_text: str):
    """Gửi tin nhắn văn bản thông thường"""
    try:
        content_dict = {"text": content_text}
        req = CreateMessageRequest.builder() \
            .receive_id_type(receive_id_type) \
            .request_body(CreateMessageRequestBody.builder()
                          .receive_id(receive_id)
                          .msg_type("text")
                          .content(json.dumps(content_dict))
                          .build()) \
            .build()
        client.im.v1.message.create(req)
    except Exception as e:
        print(f"Loi gui text: {e}")

# ==========================================
# 4. BÓC TÁCH VÀ TẢI FILE TỪ GOOGLE DRIVE (HỖ TRỢ BOM.SO, ACESSE.ONE)
# ==========================================
def extract_clean_text(message_dict: dict):
    """Bóc tách text sạch từ tin nhắn text hoặc post rich text"""
    msg_type = message_dict.get("message_type", "")
    content_raw = message_dict.get("content", "{}")
    
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
        return " ".join(extracted_pieces)

    return ""

def resolve_target_url(short_url: str) -> str:
    """Giải mã link rút gọn (bom.so, acesse.one, encurtador.dev) để lấy link Google Drive thật"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    }
    try:
        session = requests.Session()
        res = session.get(short_url, headers=headers, timeout=15, allow_redirects=True)
        if "drive.google.com" in res.url:
            return res.url

        # Quét HTML tìm link Google Drive ẩn
        drive_links = re.findall(r'https://drive\.google\.com/[^\s"\'<>]+', res.text)
        if drive_links:
            return drive_links[0].replace(r'\/', '/').rstrip('\\')

        return res.url
    except Exception as e:
        print(f"Loi phan giai link {short_url}: {e}")
        return short_url

def download_file_from_url(raw_url: str, output_path: str) -> bool:
    """Thực hiện phân giải link và tải tệp video"""
    real_url = resolve_target_url(raw_url)
    print(f"🔗 Link dich sau khi phan giai: {real_url}")

    match_drive = re.search(r'/d/([a-zA-Z0-9_-]+)', real_url) or re.search(r'id=([a-zA-Z0-9_-]+)', real_url)
    
    if match_drive:
        file_id = match_drive.group(1)
        download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        
        session = requests.Session()
        response = session.get(download_url, stream=True)

        token = None
        for k, v in response.cookies.items():
            if k.startswith("download_warning"):
                token = v
                break

        if not token:
            confirm_matches = re.findall(r'confirm=([0-9A-Za-z_]+)', response.text)
            if confirm_matches:
                token = confirm_matches[0]

        if token:
            download_url = f"https://drive.google.com/uc?export=download&confirm={token}&id={file_id}"
            response = session.get(download_url, stream=True)

        if "text/html" in response.headers.get("Content-Type", ""):
            download_url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download"
            response = session.get(download_url, stream=True)

        if response.status_code == 200:
            with open(output_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 50000:
                return True

    # Thử tải từ link trực tiếp nếu không phải Google Drive
    try:
        r = requests.get(real_url, stream=True, timeout=30)
        if r.status_code == 200 and "text/html" not in r.headers.get("Content-Type", ""):
            with open(output_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 50000:
                return True
    except Exception as e:
        print(f"Loi tai file truc tiep: {e}")

    return False

# ==========================================
# 5. XỬ LÝ SỰ KIỆN MENU & NHẬN TIN NHẮN
# ==========================================
def handle_menu_click(data: dict):
    """Phản hồi khi bấm nút menu Send proof (không có @tên nhân viên)"""
    event = data.get("event", {})
    if event.get("event_key") == "trigger_proof_template":
        open_id = event.get("operator", {}).get("operator_id", {}).get("open_id", "") or event.get("operator", {}).get("open_id", "")
        template = (
            "📋 TEMPLATES CHECK PROOF \n\n"
            "Hãy copy đoạn bên dưới, dán vào ô chat rồi thêm Ticket_ID & link proof nhé:\n\n"
            "Take proof & hold after confirmation\n"
            "7686040088400168978\n"
            "https://bom.so/sf8t5L"
        )
        if open_id:
            send_text_msg(open_id, "open_id", template)

def handle_message_receive(data: dict):
    """Xử lý tin nhắn, quét qua các link để tải video và trả về Thẻ Card không nút bấm"""
    event = data.get("event", {})
    message = event.get("message", {})
    chat_id = message.get("chat_id", "")

    clean_text = extract_clean_text(message)
    if not clean_text:
        return

    normalized_text = clean_text.replace(r"\n", "\n")
    urls = re.findall(r"https?://[^\s<>\"']+", normalized_text)
    order_ids = re.findall(r"\b\d{15,20}\b", normalized_text)

    if not urls:
        return

    order_id = order_ids[0] if order_ids else "PROOF_DATA"
    send_text_msg(chat_id, "chat_id", f"Đã nhận link Proof của đơn {order_id}. Bot đang tiến hành tải video dữ liệu...")

    success = False
    file_name = f"{order_id}.mp4"
    
    # Duyệt qua tất cả các link được gửi trong tin nhắn (hỗ trợ bom.so, acesse.one,...)
    for target_url in urls:
        print(f"Dang thu tai tu link: {target_url}")
        if download_file_from_url(target_url, file_name):
            success = True
            break

    if success:
        file_size_mb = round(os.path.getsize(file_name) / (1024 * 1024), 2)
        send_detailed_card(chat_id, "chat_id", order_id, file_name, file_size_mb)
    else:
        send_text_msg(chat_id, "chat_id", f"⚠️ Không thể tải video từ các link trên, vui lòng kiểm tra lại quyền truy cập!")

# ==========================================
# 6. KHỞI CHẠY WEBSOCKET LARK CLIENT
# ==========================================
def run_lark_ws():
    print("BOT LARK PROOF DANG KHOI CHAY...")
    
    event_dispatcher = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(lambda data: handle_message_receive(json.loads(lark.JSON.marshal(data)))) \
        .register_p2_application_bot_menu_v6(lambda data: handle_menu_click(json.loads(lark.JSON.marshal(data)))) \
        .build()

    ws_client = lark.ws.Client(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        domain=TARGET_DOMAIN,
        event_handler=event_dispatcher,
        log_level=lark.LogLevel.INFO
    )
    ws_client.start()

if __name__ == "__main__":
    threading.Thread(target=run_dummy_server, daemon=True).start()
    run_lark_ws()
