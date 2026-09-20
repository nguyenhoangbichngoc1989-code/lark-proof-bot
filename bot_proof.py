import os
import sys
import json
import re
import time
import requests
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Import thư viện Lark Suite SDK
try:
    import lark_oapi as lark
    from lark_oapi.api.im.v1 import *
except ImportError:
    print("Vui lòng cài đặt lark-oapi: pip install lark-oapi")
    sys.exit(1)

# ==========================================
# 1. CẤU HÌNH BIẾN MÔI TRƯỜNG (LARK & RENDER)
# ==========================================
APP_ID = os.environ.get("LARK_APP_ID", "").strip()
APP_SECRET = os.environ.get("LARK_APP_SECRET", "").strip()
PORT = int(os.environ.get("PORT", 10000))

if not APP_ID or not APP_SECRET:
    print("❌ LỖI: Chưa cấu hình LARK_APP_ID hoặc LARK_APP_SECRET trong biến môi trường!")

# Khởi tạo Lark Client
client = lark.Client.builder().app_id(APP_ID).app_secret(APP_SECRET).log_level(lark.LogLevel.INFO).build()

# ==========================================
# 2. HTTP SERVER PHỤ ĐỂ GIỮ RENDER WEBSERVICE
# ==========================================
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write("Bot Proof Lark Suite is Live and Running!".encode("utf-8"))

    def log_message(self, format, *args):
        return  # Tắt bớt log request định kỳ

def run_dummy_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    print(f"🌐 Đã mở cổng HTTP {PORT} để duy trì dịch vụ Render...")[cite: 12]
    server.serve_forever()

# ==========================================
# 3. CÁC HÀM TIỆN ÍCH GỬI TIN NHẮN & BÓC TÁCH
# ==========================================
def send_text_msg(receive_id: str, receive_id_type: str, content_text: str):
    """Gửi tin nhắn văn bản thông qua Lark Open API"""
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
            print(f"❌ Gửi tin nhắn thất bại: code={resp.code}, msg={resp.msg}")
    except Exception as e:
        print(f"❌ Lỗi ngoại lệ khi gửi tin nhắn: {e}")

def extract_clean_text(message_dict: dict) -> tuple[str, list[str]]:
    """
    Bóc tách nội dung văn bản sạch và danh sách user_id được mention
    xử lý trơn tru cả tin nhắn 'text' thông thường lẫn tin nhắn 'post' (Rich Text).
    """
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
        # Cấu trúc post có thể là dạng phân đoạn nội bộ hoặc locale
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
# 4. XỬ LÝ SỰ KIỆN NÚT BẤM MENU (🫆 Send proof)
# ==========================================
def handle_menu_click(data: dict):
    """Bắt sự kiện click Custom Bot Menu (application.bot.menu_v6)"""
    event = data.get("event", {})
    event_key = event.get("event_key", "")
    operator_id = event.get("operator", {}).get("operator_id", {})
    open_id = operator_id.get("open_id", "")

    print(f"📌 Đã bấm nút Menu: key='{event_key}', open_id='{open_id}'")

    if event_key == "trigger_proof_template":
        template = (
            "📋 MẪU GỬI PROOF TIÊU CHUẨN\n\n"
            "Chị copy nguyên đoạn bên dưới, dán vào ô chat rồi điền thông tin nhé:\n\n"
            "Take proof & hold, send to @Nhân_Viên after confirmation\n"
            "7687158181478451220\n"
            "https://acesse.one/link-proof-cua-chi"
        )
        if open_id:
            send_text_msg(open_id, "open_id", template)

# ==========================================
# 5. XỬ LÝ SỰ KIỆN TIN NHẮN (im.message.receive_v1)
# ==========================================
def handle_message_receive(data: dict):
    """Bắt và phân tích tin nhắn người dùng gửi tới Bot"""
    event = data.get("event", {})
    message = event.get("message", {})
    chat_type = message.get("chat_type", "")      # "p2p" (1-1) hoặc "group"
    chat_id = message.get("chat_id", "")
    message_id = message.get("message_id", "")
    
    clean_text, mentions = extract_clean_text(message)
    if not clean_text:
        return

    print(f"📩 Nhận tin nhắn [{chat_type}]: {clean_text}")

    # Tìm liên kết Proof (acesse, bom.so, drive, docs, ...)
    urls = re.findall(r"https?://[^\s<>\"']+", clean_text)
    
    # Tìm mã đơn hàng (chuỗi số 15 - 20 ký tự)
    order_ids = re.findall(r"\b\d{15,20}\b", clean_text)

    # Nếu tin nhắn không chứa link, bỏ qua không xử lý
    if not urls:
        return

    target_url = urls[0]
    order_id = order_ids[0] if order_ids else "PROOF_DATA"

    print(f"🚀 BẮT ĐƯỢC LINK: {target_url} | ĐƠN HÀNG: {order_id}")

    # Thực thi quy trình tải file, nén và phản hồi thẻ
    # (Đoạn này bot gọi các logic nén file/tải link của chị)
    confirm_msg = f" Đã nhận link Proof của đơn {order_id}. Bot đang tiến hành xử lý tải dữ liệu..."
    send_text_msg(chat_id, "chat_id", confirm_msg)

# ==========================================
# 6. BỘ ĐIỀU PHỐI SỰ KIỆN TỔNG (EVENT DISPATCHER)
# ==========================================
def ws_event_handler(data: dict):
    """Hàm lắng nghe và phân luồng sự kiện từ WebSocket"""
    header = data.get("header", {})
    event_type = header.get("event_type", "")

    if event_type == "application.bot.menu_v6":
        handle_menu_click(data)
    elif event_type == "im.message.receive_v1":
        handle_message_receive(data)
    elif event_type == "card.action.trigger":
        # Nhánh nhận sự kiện bấm nút Transfer Proof / Callback thẻ[cite: 1, 2]
        print(f"🎯 Nhận Callback từ Card Action!")[cite: 1]

# ==========================================
# 7. KHỞI CHẠY WEBSOCKET LARK CLIENT
# ==========================================
def run_lark_ws():
    print("🚀 BOT LARK PROOF (PHIÊN BẢN ĐẦY ĐỦ 2026 ĐANG KHỞI CHẠY)...")[cite: 12]
    
    # Đăng ký handler bắt các sự kiện
    event_dispatcher = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(lambda data: handle_message_receive(json.loads(lark.JSON.marshal(data)))) \
        .build()

    # Kết nối qua WebSocket (Long Connection)
    ws_client = lark.ws.Client(
        app_id=APP_ID,
        app_secret=APP_SECRET,
        event_handler=event_dispatcher,
        log_level=lark.LogLevel.INFO
    )
    ws_client.start()

if __name__ == "__main__":
    # 1. Chạy HTTP Server ngầm ở luồng phụ để Render không bị tắt cổng
    threading.Thread(target=run_dummy_server, daemon=True).start()
    
    # 2. Chạy WebSocket chính của Lark
    run_lark_ws()
