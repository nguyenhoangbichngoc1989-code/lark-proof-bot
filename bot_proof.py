import static_ffmpeg
static_ffmpeg.add_paths()

import os
import re
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
import lark_oapi as lark
from lark_oapi.api.im.v1 import *
import urllib3
from PIL import Image
import pillow_heif

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
pillow_heif.register_heif_opener()

# Lấy thông tin xác thực từ Biến môi trường trên Render
APP_ID = os.environ.get("APP_ID", "")
APP_SECRET = os.environ.get("APP_SECRET", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "temp_files")
ERROR_IMG_PATH = os.path.join(BASE_DIR, "gdrive_error.png")
HISTORY_FILE = os.path.join(BASE_DIR, "history_proof.json")
FFMPEG_BIN = "ffmpeg"

PROCESSED_MESSAGES = set()

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
    with socketserver.TCPServer(("", port), RenderHealthHandler) as httpd:
        print(f"🌐 Đã mở cổng HTTP {port} để giữ dịch vụ Render hoạt động...")
        httpd.serve_forever()

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
        res = requests.post(url, json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=15)
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

# ----------------- NÉN VIDEO VỀ DƯỚI 25MB -----------------
def compress_video_if_large(video_path: str) -> str:
    size_mb = os.path.getsize(video_path) / (1024 * 1024)
    if size_mb <= 27.0:
        return video_path

    print(f"⚡ Video {os.path.basename(video_path)} ({size_mb:.2f}MB) vượt ngưỡng 30MB. Đang nén tự động về < 25MB...")
    name, ext = os.path.splitext(video_path)
    compressed_path = f"{name}_compressed.mp4"

    scale_cmd = '-vf "scale=\'min(720,iw)\':-2"'
    bitrate_cmd = '-b:v 600k -maxrate 800k -bufsize 1000k'

    cmd = f'"{FFMPEG_BIN}" -y -i "{video_path}" -c:v libx264 {bitrate_cmd} {scale_cmd} -preset veryfast -c:a aac -b:a 48k "{compressed_path}"'
    try:
        subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=400)
        if os.path.exists(compressed_path) and os.path.getsize(compressed_path) > 0:
            compressed_mb = os.path.getsize(compressed_path) / (1024 * 1024)
            print(f"✅ Đã nén thành công về {compressed_mb:.2f}MB (< 25MB chuẩn Lark!).")
            return compressed_path
    except Exception as e:
        print(f"Lỗi nén video: {e}")
    return video_path

# ----------------- TẢI VIDEO TỪ YOUTUBE / SHORTS -----------------
def download_youtube_video(url: str, target_dir: str) -> bool:
    print(f"▶️ Đang tải video từ YouTube/Shorts: {url}")
    try:
        import yt_dlp
        output_template = os.path.join(target_dir, "youtube_video.mp4")
        ydl_opts = {
            'format': 'best',
            'outtmpl': output_template,
            'quiet': True,
            'no_warnings': True
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        for f in os.listdir(target_dir):
            if f.startswith("youtube_video") and os.path.getsize(os.path.join(target_dir, f)) > 1000:
                print(f"📥 Tải thành công video YouTube: {f} ({format_size(os.path.getsize(os.path.join(target_dir, f)))})")
                return True
    except Exception as e:
        print(f"Lỗi tải YouTube video: {e}")
        try:
            output_template = os.path.join(target_dir, "youtube_video.mp4")
            cmd = f'python -m yt_dlp -o "{output_template}" "{url}"'
            subprocess.run(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
            if os.path.exists(output_template) and os.path.getsize(output_template) > 1000:
                return True
        except Exception:
            pass
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
                img.convert("RGB").save(out_path, "JPEG", quality=95)
            os.remove(file_path)
            print(f"🖼️ Đã chuyển đổi tệp {os.path.basename(file_path)} (.jfif) sang định dạng JPEG!")
            return [out_path]
        except Exception as e:
            print(f"Lỗi chuyển .jfif sang jpeg: {e}")
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
                image = page.render(scale=2).to_pil()
                page_path = f"{name}_trang_{i+1}.png"
                image.save(page_path, "PNG")
                converted_files.append(page_path)
            print(f"📄 Đã bung {len(converted_files)} trang PDF thành ảnh PNG trực tiếp!")
            converted_files.append(file_path)
            return converted_files
        except Exception as e:
            print(f"Lỗi render PDF: {e}")
            return [file_path]

    elif ext_lower == ".heic":
        out_path = f"{name}.jpg"
        try:
            with Image.open(file_path) as img:
                img.convert("RGB").save(out_path, "JPEG", quality=95)
            os.remove(file_path)
            return [out_path]
        except Exception:
            return [file_path]

    elif ext_lower in [".webm", ".mkv"]:
        out_path = f"{name}.mp4"
        try:
            cmd = f'"{FFMPEG_BIN}" -y -i "{file_path}" -c:v libx264 -preset fast -c:a aac "{out_path}"'
            subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                os.remove(file_path)
                return [out_path]
        except Exception:
            pass
        return [file_path]

    return [file_path]

# ----------------- UPLOAD FILE VÀ GỬI GỘP VÀO THREAD -----------------
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
            res = requests.post(url, headers=headers, data=data, files=files, timeout=300)
            if res.status_code == 200:
                body = res.json()
                if body.get("code") == 0:
                    return body["data"]["file_key"]
                else:
                    print(f"❌ Lark API Error ({file_type}): Code={body.get('code')} | Msg={body.get('msg')}")
            else:
                print(f"❌ HTTP Error {res.status_code} khi tải {safe_name}")
    except Exception as e:
        print(f"Lỗi upload: {e}")
    return ""

def upload_and_send_batch_proofs(message_id: str, final_files: list):
    try:
        print(f"🚀 Bắt đầu đẩy gộp {len(final_files)} tệp vào Thread...")
        image_keys = []
        for f in final_files:
            file_path = f["path"]
            file_name = f["name"]
            file_ext = f["ext"]

            if file_ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]:
                with open(file_path, "rb") as img_f:
                    create_req = CreateImageRequest.builder() \
                        .request_body(CreateImageRequestBody.builder().image_type("message").image(img_f).build()) \
                        .build()
                    create_resp = client.im.v1.image.create(create_req)
                    if create_resp and create_resp.success():
                        image_keys.append(create_resp.data.image_key)
                        print(f"✅ Đã chuẩn bị ảnh: {file_name}")

        for img_k in image_keys:
            body = ReplyMessageRequestBody.builder().content(json.dumps({"image_key": img_k})).msg_type("image").reply_in_thread(True).build()
            client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(body).build())

        for f in final_files:
            file_path = f["path"]
            file_name = f["name"]
            file_ext = f["ext"]

            if file_ext in [".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"]:
                continue

            if file_ext in [".mp4", ".mov"]:
                upload_path = compress_video_if_large(file_path)

                media_key = upload_file_direct(upload_path, "mp4")
                if media_key:
                    media_body = ReplyMessageRequestBody.builder().content(json.dumps({"file_key": media_key})).msg_type("media").reply_in_thread(True).build()
                    client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(media_body).build())
                    print(f"✅ Đã gửi khung phát video: {file_name}")

                stream_key = upload_file_direct(upload_path, "stream")
                if stream_key:
                    file_body = ReplyMessageRequestBody.builder().content(json.dumps({"file_key": stream_key})).msg_type("file").reply_in_thread(True).build()
                    client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(file_body).build())
                    print(f"✅ Đã gửi tệp đính kèm video: {file_name}")
            else:
                file_key = upload_file_direct(file_path, "stream")
                if file_key:
                    file_body = ReplyMessageRequestBody.builder().content(json.dumps({"file_key": file_key})).msg_type("file").reply_in_thread(True).build()
                    client.im.v1.message.reply(ReplyMessageRequest.builder().message_id(message_id).request_body(file_body).build())
                    print(f"✅ Đã gửi tệp: {file_name}")

    except Exception as e:
        print(f"Lỗi xử lý gửi gộp media: {e}")

# ----------------- TẢI FILE GOOGLE DRIVE (TÍCH HỢP GDOWN) -----------------
def check_gdrive_error(url: str) -> bool:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        res = requests.get(url, headers=headers, timeout=10, verify=False)
        text = res.text
        return ("không thể mở tệp tại thời điểm này" in text or "unable to open the file at this time" in text.lower())
    except Exception:
        return False

def resolve_proof_url(url: str) -> str:
    if "bom.so" not in url:
        return url
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    try:
        r = requests.get(url, headers=headers, allow_redirects=True, timeout=15, verify=False)
        if r.url != url and "bom.so" not in r.url:
            return r.url
        html = r.text
        meta_match = re.search(r'<meta[^>]*?content=["\']\d+;\s*url=([^"\'>\s]+)["\']', html, re.IGNORECASE)
        if meta_match:
            return meta_match.group(1).replace("&amp;", "&")
        js_match = re.search(r'(?:window\.location(?:\.href)?|location\.href)\s*=\s*["\']([^"\']+)["\']', html)
        if js_match:
            return js_match.group(1).replace("&amp;", "&")
        a_match = re.findall(r'href=["\'](https?://(?!bom\.so)[^"\']+)["\']', html)
        for link in a_match:
            if any(ext in link for ext in [".mp4", ".jpg", ".png", "fptcloud.com", "aliyuncs.com", "drive.google.com"]):
                return link.replace("&amp;", "&")
        return r.url
    except Exception:
        return url

def download_single_gdrive_file(file_id: str, target_dir: str, preferred_name: str = "") -> bool:
    # 1. Thử tải bằng gdown (Vượt cảnh báo file lớn và quét virus tốt nhất)
    try:
        import gdown
        save_name = preferred_name or f"gdrive_{file_id}.mp4"
        save_path = os.path.join(target_dir, save_name)
        url = f"https://drive.google.com/uc?id={file_id}"
        output = gdown.download(url, save_path, quiet=True, fuzzy=True)
        if output and os.path.exists(output) and os.path.getsize(output) > 2000:
            print(f"📥 [gdown] Tải thành công tệp GDrive: {os.path.basename(output)} ({format_size(os.path.getsize(output))})")
            return True
    except Exception as e:
        print(f"gdown thất bại, chuyển sang phương án Session: {e}")

    # 2. Phương án dự phòng dùng Session tự bóc tách confirm_token
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "*/*"
    }

    try:
        url = f"https://drive.google.com/uc?export=download&id={file_id}"
        res = session.get(url, headers=headers, stream=True, verify=False, timeout=60)
        confirm_token = None
        for k, v in res.cookies.items():
            if k.startswith("download_warning"):
                confirm_token = v
                break
        if not confirm_token:
            match = re.search(r'confirm=([0-9A-Za-z_]+)', res.text)
            if match:
                confirm_token = match.group(1)

        if confirm_token:
            url = f"https://drive.google.com/uc?export=download&id={file_id}&confirm={confirm_token}"
            res = session.get(url, headers=headers, stream=True, verify=False, timeout=120)

        if res.status_code == 200 and "text/html" not in res.headers.get("Content-Type", ""):
            save_name = preferred_name or f"gdrive_{file_id}.mp4"
            save_path = os.path.join(target_dir, save_name)
            with open(save_path, "wb") as f:
                for chunk in res.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
            if os.path.exists(save_path) and os.path.getsize(save_path) > 2000:
                print(f"📥 Tải thành công tệp GDrive (Session): {save_name} ({format_size(os.path.getsize(save_path))})")
                return True
    except Exception as e:
        print(f"Lỗi Session download: {e}")

    return False

def download_gdrive_folder_files(folder_url: str, target_dir: str) -> bool:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    }
    try:
        res = requests.get(folder_url, headers=headers, timeout=20, verify=False)
        if res.status_code != 200:
            return False

        html = res.text
        found_files = {}

        matches = re.findall(r'\["([a-zA-Z0-9_-]{28,45})",\["([^"]+)"', html)
        for fid, fname in matches:
            fname_clean = clean_file_display_name(fname)
            if any(ext in fname_clean.lower() for ext in [".jfif", ".mp4", ".mov", ".avi", ".mkv", ".jpg", ".png", ".jpeg", ".webp", ".pdf"]):
                found_files[fid] = fname_clean

        if not found_files:
            matches_alt = re.findall(r'\["([a-zA-Z0-9_-]{28,45})","([^"]+)"', html)
            for fid, fname in matches_alt:
                fname_clean = clean_file_display_name(fname)
                if any(ext in fname_clean.lower() for ext in [".jfif", ".mp4", ".mov", ".jpg", ".png", ".jpeg", ".webp", ".pdf"]):
                    found_files[fid] = fname_clean

        if not found_files:
            folder_id_match = re.search(r'/folders/([a-zA-Z0-9_-]+)', folder_url)
            folder_id = folder_id_match.group(1) if folder_id_match else ""
            raw_ids = set(re.findall(r'["\']([a-zA-Z0-9_-]{28,40})["\']', html))
            idx = 1
            for rid in raw_ids:
                if rid != folder_id:
                    found_files[rid] = f"gdrive_item_{idx}"
                    idx += 1

        if found_files:
            print(f"📂 Đã tìm thấy {len(found_files)} tệp trong Folder Google Drive. Đang tải...")
            success_count = 0
            for fid, fname in found_files.items():
                if download_single_gdrive_file(fid, target_dir, fname):
                    success_count += 1
            return success_count > 0

    except Exception as e:
        print(f"Lỗi phân tích Folder GDrive: {e}")

    return False

# ----------------- TẢI FILE SHAREPOINT / ONEDRIVE -----------------
def extract_all_zips(target_dir: str):
    has_zip = True
    while has_zip:
        has_zip = False
        for root, _, files in os.walk(target_dir):
            for file in files:
                if file.lower().endswith(".zip"):
                    zip_p = os.path.join(root, file)
                    try:
                        if zipfile.is_zipfile(zip_p) and os.path.getsize(zip_p) > 200:
                            print(f"📦 Đang giải nén: {file} ({format_size(os.path.getsize(zip_p))})...")
                            with zipfile.ZipFile(zip_p, 'r') as zf:
                                zf.extractall(target_dir)
                            has_zip = True
                        os.remove(zip_p)
                    except Exception:
                        try:
                            os.remove(zip_p)
                        except Exception:
                            pass

def download_onedrive_sharepoint(url: str, target_dir: str) -> bool:
    print(f"☁️ Đang xử lý link OneDrive/SharePoint: {url}")
    session = requests.Session()
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "*/*"
    }

    try:
        clean_share_url = url.split("?")[0]
        encoded = base64.b64encode(clean_share_url.encode('utf-8')).decode('utf-8')
        sharing_token = "u!" + encoded.rstrip('=').replace('/', '_').replace('+', '-')

        parsed = urllib.parse.urlparse(url)
        base_domain = f"{parsed.scheme}://{parsed.netloc}"

        api_endpoints = [
            f"{base_domain}/_api/v2.0/shares/{sharing_token}/driveItem/children",
            f"https://api.onedrive.com/v1.0/shares/{sharing_token}/root/children"
        ]

        for ep in api_endpoints:
            try:
                res = session.get(ep, headers={**headers, "Accept": "application/json"}, timeout=20, verify=False)
                if res.status_code == 200:
                    data = res.json()
                    items = data.get("value", [])
                    if items:
                        downloaded = 0
                        for it in items:
                            fname = clean_file_display_name(it.get("name", f"file_{downloaded}"))
                            dl_url = it.get("@microsoft.graph.downloadUrl") or it.get("@content.downloadUrl")
                            if dl_url:
                                save_p = os.path.join(target_dir, fname)
                                f_res = session.get(dl_url, headers=headers, stream=True, timeout=90, verify=False)
                                if f_res.status_code == 200:
                                    with open(save_p, "wb") as f:
                                        for chunk in f_res.iter_content(chunk_size=1024 * 1024):
                                            if chunk:
                                                f.write(chunk)
                                    print(f"📥 Tải thành công: {fname} ({format_size(os.path.getsize(save_p))})")
                                    downloaded += 1
                        if downloaded > 0:
                            extract_all_zips(target_dir)
                            return True
            except Exception as e:
                print(f"Thử API {ep} lỗi: {e}")

        r_page = session.get(url, headers=headers, timeout=20, verify=False, allow_redirects=True)
        final_page_url = r_page.url
        user_match = re.search(r'(/personal/[^/]+)', final_page_url)
        web_path = user_match.group(1) if user_match else ""

        token_match = re.search(r'/:f:/p/[^/]+/([a-zA-Z0-9_-]+)', url)
        share_token_simple = token_match.group(1) if token_match else None

        zip_download_urls = []
        if share_token_simple and web_path:
            zip_download_urls.append(f"{base_domain}{web_path}/_layouts/15/download.aspx?share={share_token_simple}")
        if "download=1" not in final_page_url:
            sep = "&" if "?" in final_page_url else "?"
            zip_download_urls.append(f"{final_page_url}{sep}download=1")

        for dl_url in zip_download_urls:
            try:
                bundle_path = os.path.join(target_dir, "share_bundle.zip")
                b_res = session.get(dl_url, headers=headers, stream=True, timeout=120, verify=False)
                if b_res.status_code == 200:
                    c_type = b_res.headers.get("Content-Type", "").lower()
                    if "text/html" not in c_type:
                        with open(bundle_path, "wb") as f:
                            for chunk in b_res.iter_content(chunk_size=1024 * 1024):
                                if chunk:
                                    f.write(chunk)
                        if os.path.exists(bundle_path) and os.path.getsize(bundle_path) > 500 and zipfile.is_zipfile(bundle_path):
                            print(f"✅ Đã tải gói thư mục thành công ({format_size(os.path.getsize(bundle_path))})")
                            extract_all_zips(target_dir)
                            return len(os.listdir(target_dir)) > 0
                        else:
                            try:
                                os.remove(bundle_path)
                            except Exception:
                                pass
            except Exception as e:
                print(f"Lỗi tải bundle {dl_url}: {e}")

    except Exception as e:
        print(f"Lỗi xử lý OneDrive / SharePoint: {e}")

    extract_all_zips(target_dir)
    return len(os.listdir(target_dir)) > 0

# ----------------- HÀM TẢI FILE TỔNG HỢP -----------------
def download_proof(url: str, target_dir: str) -> bool:
    final_url = resolve_proof_url(url)
    print(f"🌐 Đang xử lý link: {final_url}")

    if "docs.google.com/spreadsheets" in final_url:
        match = re.search(r'/d/([a-zA-Z0-9-_]+)', final_url)
        if match:
            doc_id = match.group(1)
            export_url = f"https://docs.google.com/spreadsheets/d/{doc_id}/export?format=xlsx"
            res = requests.get(export_url, verify=False)
            if res.status_code == 200:
                with open(os.path.join(target_dir, f"Sheet_{doc_id[:8]}.xlsx"), "wb") as f:
                    f.write(res.content)
                return True

    elif "docs.google.com/document" in final_url:
        match = re.search(r'/d/([a-zA-Z0-9-_]+)', final_url)
        if match:
            doc_id = match.group(1)
            export_url = f"https://docs.google.com/document/d/{doc_id}/export?format=docx"
            res = requests.get(export_url, verify=False)
            if res.status_code == 200:
                with open(os.path.join(target_dir, f"Doc_{doc_id[:8]}.docx"), "wb") as f:
                    f.write(res.content)
                return True

    if "drive.google.com" in final_url:
        if "/folders/" in final_url:
            return download_gdrive_folder_files(final_url, target_dir)
        else:
            file_id_match = re.search(r'(?:/file/d/|id=)([a-zA-Z0-9_-]+)', final_url)
            if file_id_match:
                return download_single_gdrive_file(file_id_match.group(1), target_dir)

    if any(kw in final_url.lower() for kw in ["sharepoint.com", "1drv.ms", "onedrive.live.com"]):
        return download_onedrive_sharepoint(final_url, target_dir)

    if any(kw in final_url.lower() for kw in ["youtube.com", "youtu.be"]):
        return download_youtube_video(final_url, target_dir)

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        res = requests.get(final_url, headers=headers, stream=True, timeout=90, verify=False)
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
                for chunk in res.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
            print(f"📥 Đã tải thành công: {filename}")
            return True
        return False
    except Exception as e:
        print(f"Lỗi tải trực tiếp: {e}")
        return False

# ----------------- XỬ LÝ CHÍNH & RENDER THẺ -----------------
def process_request(message_id: str, text: str, sender_id: str):
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

    extract_all_zips(task_temp_dir)

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
        elif ext in [".mp3", ".wav"]:
            icon = "🎵"
        else:
            icon = "📁"
        categorized.setdefault(icon, []).append(x)

    type_block_lines = []
    for icon, items in categorized.items():
        type_block_lines.append(f"• {icon}: {len(items)} file")
        for item in items:
            type_block_lines.append(f"       •  {item['name']}: [{format_size(item['size'])}]")
    type_content = "\n".join(type_block_lines)

    # THẺ 1: BÁO CÁO BAN ĐẦU
    card_element_top = (
        f"🎫<text_tag color='turquoise'>𝐓𝐢𝐜𝐤𝐞𝐭_𝐈𝐃:</text_tag> {ticket_id}\n"
        f"💾<text_tag color='carmine'>ᴛᴏᴛᴀʟ ғɪʟᴇ sɪᴢᴇ:</text_tag> {format_size(total_size)}\n"
        f"   ╰┄▸<text_tag color='carmine'>𝐀𝐭𝐭𝐚𝐜𝐡𝐦𝐞𝐧𝐭𝐬: </text_tag> {len(final_files)}/{len(final_files)}\n\n"
        f"{type_content}\n\n"
        f"      <text_tag color='yellow'>   ⇓ ⇓ ⇓   </text_tag>"
    )

    loading_styled = "⏳ <text_tag color='yellow'> 𝐥 𝐨 𝐚 𝐝 𝐢 𝐧 𝐠 ..... </text_tag>"
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
                        "elements": [
                            {
                                "tag": "markdown",
                                "content": loading_styled
                            }
                        ]
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 2,
                        "horizontal_align": "right",
                        "elements": [
                            {
                                "tag": "markdown",
                                "content": right_badge_styled
                            }
                        ]
                    }
                ]
            }
        ]
    }
    reply_thread_card(message_id, report_card_payload)

    # GỬI GỘP TẤT CẢ TỆP VÀO THREAD
    upload_and_send_batch_proofs(message_id, final_files)

    # THẺ 2: THÔNG BÁO HOÀN TẤT
    rabbit_side_md = (
        "<font color='turquoise'>-ˋ (\\ (\\   .\n"
        ".(„• ֊ •„)\n"
        "─‌∪─‌∪࿎࿎</font>"
    )

    title_side_md = (
        "<text_tag color='turquoise'>ᴄᴏᴍᴘʟᴇᴛᴇᴅ</text_tag>\n"
        "<text_tag color='turquoise'>-ˋˏ   𝐃𝐎𝐖𝐍𝐋𝐎𝐀𝐃 𝐏𝐑𝐎OF ˎˊ-</text_tag>"
    )

    at_middle_md = (
        f"<text_tag color='carmine'>♡</text_tag> <at id=\"{sender_id}\"></at> ơi...\n"
        f"    ╰┄▸<text_tag color='turquoise'>Ticket_ID</text_tag>_<text_tag color='carmine'>『{ticket_id}』</text_tag>"
    )

    thankyou_center_md = (
        "┊t h a n k y o u┊\n"
        "<font color='turquoise'>┈┈┈┈┈┈┈┈․° ••• °․┈┈┈┈┈┈┈┈</font>"
    )

    finish_card_payload = {
        "elements": [
            {
                "tag": "column_set",
                "flex_mode": "none",
                "background_style": "default",
                "columns": [
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [
                            {
                                "tag": "markdown",
                                "content": rabbit_side_md
                            }
                        ]
                    },
                    {
                        "tag": "column",
                        "width": "weighted",
                        "weight": 1,
                        "elements": [
                            {
                                "tag": "markdown",
                                "content": title_side_md
                            }
                        ]
                    }
                ]
            },
            {
                "tag": "markdown",
                "content": at_middle_md
            },
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": thankyou_center_md
                },
                "text_align": "center"
            }
        ]
    }
    reply_thread_card(message_id, finish_card_payload)

    shutil.rmtree(task_temp_dir, ignore_errors=True)

def handle_message(data: lark.im.v1.P2MessageReceiveV1) -> None:
    try:
        event = data.event
        msg = event.message

        if msg.message_id in PROCESSED_MESSAGES:
            return
        PROCESSED_MESSAGES.add(msg.message_id)

        if len(PROCESSED_MESSAGES) > 500:
            PROCESSED_MESSAGES.pop()

        if msg.message_type == "text":
            content = json.loads(msg.content)
            text = content.get("text", "")
            sender_id = event.sender.sender_id.open_id if (event.sender and event.sender.sender_id) else ""
            threading.Thread(target=process_request, args=(msg.message_id, text, sender_id), daemon=True).start()
    except Exception as e:
        print(f"Lỗi handle_message: {e}")

def handle_message_update(data) -> None:
    pass

def start_bot():
    print("=" * 60)
    print("🚀 BOT LARK PROOF (PHIÊN BẢN CLOUD BẢO MẬT 2026)...")
    print("=" * 60)
    
    # Khởi chạy cổng web ngầm để Render xác nhận Live
    threading.Thread(target=run_dummy_web_server, daemon=True).start()

    # Đăng ký nhận sự kiện WebSocket từ Lark
    event_handler = lark.EventDispatcherHandler.builder("", "") \
        .register_p2_im_message_receive_v1(handle_message) \
        .build()

    if hasattr(event_handler, "_handlers"):
        event_handler._handlers["im.message.updated_v1"] = handle_message_update

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
