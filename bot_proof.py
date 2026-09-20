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
# 3. HÀM GỬI TIN NHẮN & BÓC TÁCH RICH TEXT
# ==========================================
def send_text_msg(receive_id: str, receive_id_type: str, content_text: str):
    """Gửi tin nhắn phản hồi qua Lark Open API"""
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
        
        resp = client.im.v1.message.create(req)
        if not resp.success():
            print(f"Gui tin nhan that bai: code={resp.code}, msg={resp.msg}")
    except Exception as e:
        print(f"Loi ngoai le khi gui tin nhan: {e}")

def extract_clean_text(message_dict: dict):
    """Bóc tách text sạch từ text thường và post rich text có mention"""
    msg_type = message_dict.get("message_type", "")
    content_raw = message_dict.get("content", "{}")
    mentions_found = []
    
    try:
        content_json = json.loads(content_raw)
    except Exception:
        return "", []

    if msg_type == "text":
        return content_json.get("text", ""), []

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
                    uid = item.get("user_id", "")
                    uname = item.get("user_name", "")
                    if uid:
                        mentions_found.append(uid)
                    extracted_pieces.append(f"@{uname or uid}")
                    
        return " ".join(extracted_pieces), mentions_found

    return "", []

# ==========================================
# 4. BỘ GIẢI MÃ LINK TRUNG GIAN & TẢI DRIVE TỰ ĐỘNG
# ==========================================
def resolve_target_url(short_url: str) -> str:
    """Giải mã link rút gọn acesse.one / encurtador.dev để lấy link Google Drive thật"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    }
    try:
        session = requests.Session()
        res = session.get(short_url, headers=headers, timeout=15, allow_redirects=True)
        final_url = res.url
        
        if "drive.google.com" in final_url:
            return final_url

        html_text = res.text

        # 1. Tìm trực tiếp URL Google Drive trong HTML hoặc Script
        drive_links = re.findall(r'https://drive\.google\.com/[^\s"\'<>]+', html_text)
        if drive_links:
            clean_link = drive_links[0].replace(r'\/', '/').rstrip('\\')
            return clean_link

        # 2. Tìm link đích trong thuộc tính href của nút 'Go to destination'
        destination_matches = re.findall(r'href=["\'](https?://[^"\']+)["\'][^>]*>[\s\r\n]*Go to destination', html_text, re.IGNORECASE)
        if destination_matches:
            return destination_matches[0]

        # 3. Tìm link chuyển hướng trong thẻ meta refresh hoặc window.location
        redirect_matches = re.findall(r'(?:window\.location(?:\.href)?|url)\s*=\s*["\'](https?://[^"\']+)["\']', html_text, re.IGNORECASE)
        for cand in redirect_matches:
            if "encurtador" not in cand and "acesse.one" not in cand:
                return cand

        return final_url
    except Exception as e:
        print(f"Loi phan giai link: {e}")
        return short_url

def download_file_proof(raw_url: str, output_path: str) -> bool:
    """Tải tệp video từ Google Drive (hỗ trợ cả tệp lớn) hoặc từ link trực tiếp"""
    real_url = resolve_target_url(raw_url)
    print(f"🔗 Link dich sau khi phan giai: {real_url}")

    # Nhận diện Google Drive File ID
    match_drive = re.search(r'/d/([a-zA-Z0-9_-]+)', real_url) or re.search(r'id=([a-zA-Z0-9_-]+)', real_url)
    if match_drive:
        file_id = match_drive.group(1)
        download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
        
        session = requests.Session()
        response = session.get(download_url, stream=True)

        # Kiểm tra xác nhận virus scan cho file dung lượng lớn
        token = None
        for k, v in response.cookies.items():
            if k.startswith("download_warning"):
                token = v
                break

        if not token:
            # Tìm token trong thẻ form xác nhận nếu cookie không có
            confirm_matches = re.findall(r'confirm=([0-9A-Za-z_]+)', response.text)
            if confirm_matches:
                token = confirm_matches[0]

        if token:
            download_url = f"https://drive.google.com/uc?export=download&confirm={token}&id={file_id}"
            response = session.get(download_url, stream=True)

        content_type = response.headers.get("Content-Type", "")
        # Nếu trả về HTML tức là chưa trúng file tải, thử endpoint trực tiếp dự phòng
        if "text/html" in content_type:
            direct_api = f"https://drive.usercontent.google.com/download?id={file_id}&export=download"
            response = session.get(direct_api, stream=True)

        if response.status_code == 200:
            with open(output_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            
            # Kiểm tra tệp tải về có dung lượng hợp lệ (> 50KB)
            if os.path.exists(output_path) and os.path.getsize(output_path) > 50000:
                print(f"✅ Da tai video thanh cong ve: {output_path} ({os.path.getsize(output_path)} bytes)")
                return True
            else:
                print("⚠️ Tep tai ve qua nho hoac la trang HTML loi.")
                return False

    # Tải file từ các nguồn trực tiếp khác
    try:
        r = requests.get(real_url, stream=True, timeout=30)
        if r.status_code == 200 and "text/html" not in r.headers.get("Content-Type", ""):
            with open(output_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
            return True
    except Exception as e:
        print(f"Loi tai file truc tiep: {e}")

    return False

# ==========================================
# 5. XỬ LÝ SỰ KIỆN MENU & NHẬN TIN NHẮN
# ==========================================
def handle_menu_click(data: dict):
    """Phản hồi khi người dùng bấm nút menu Send proof"""
    event = data.get("event", {})
    event_key = event.get("event_key", "")
    operator = event.get("operator", {})
    operator_id = operator.get("operator_id", {})
    open_id = operator_id.get("open_id", "") or operator.get("open_id", "")

    print(f"📌 Da nhan click Menu Bot: key='{event_key}', open_id='{open_id}'")

    if event_key == "trigger_proof_template":
        template = (
            "📋 MẪU GỬI PROOF TIÊU CHUẨN\n\n"
            "Chị copy đoạn bên dưới, dán vào ô chat rồi thêm mã đơn & link proof nhé:\n\n"
            "Take proof & hold, send to @Tên_Nhân_Viên after confirmation\n"
            "7687158181478451220\n"
            "https://acesse.one/link-proof-cua-chi"
        )
        if open_id:
            send_text_msg(open_id, "open_id", template)

def handle_message_receive(data: dict):
    """Bắt và phân tích tin nhắn người dùng gửi"""
    event = data.get("event", {})
    message = event.get("message", {})
    chat_type = message.get("chat_type", "")
    chat_id = message.get("chat_id", "")
    
    clean_text, mentions = extract_clean_text(message)
    if not clean_text:
        return

    urls = re.findall(r"https?://[^\s<>\"']+", clean_text)
    order_ids = re.findall(r"\b\d{15,20}\b", clean_text)

    if not urls:
        return

    target_url = urls[0]
    order_id = order_ids[0] if order_ids else "PROOF_DATA"
    print(f"Bat duoc link: {target_url} | Ma don: {order_id}")

    confirm_msg = f"Đã nhận link Proof của đơn {order_id}. Bot đang tiến hành tải video dữ liệu..."
    send_text_msg(chat_id, "chat_id", confirm_msg)

    file_name = f"{order_id}.mp4"
    if download_file_proof(target_url, file_name):
        file_size_mb = round(os.path.getsize(file_name) / (1024 * 1024), 2)
        send_text_msg(chat_id, "chat_id", f"✅ Đã tải thành công video cho đơn {order_id} ({file_size_mb} MB)!")
    else:
        send_text_msg(chat_id, "chat_id", f"⚠️ Không thể tải video từ link trên, vui lòng kiểm tra lại quyền truy cập!")

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
