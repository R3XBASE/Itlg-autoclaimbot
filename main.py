import logging
import asyncio
import json
import os
import time
from datetime import datetime, timezone
import pytz
import httpx
import random

from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode, ChatAction
from telegram.error import BadRequest

# --- Konfigurasi Global ---
# Menggunakan environment variable untuk token bot
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN environment variable tidak ditemukan!")

ENABLE_DETAILED_CONSOLE_LOGS = os.environ.get('ENABLE_DETAILED_LOGS', 'true').lower() == 'true'
WIB = pytz.timezone('Asia/Jakarta')

# Menggunakan port dari environment variable untuk Render
PORT = int(os.environ.get('PORT', 8000))

BASE_API_HEADERS = {
    'User-Agent': "okhttp/4.12.0",
    'Accept-Encoding': "gzip",
    'sentry-trace': "8fe1b2cd351944b7b419337d39bab6a3-a4f52713e36553c5-1",
    'baggage': "sentry-environment=production,sentry-public_key=0b62d0d4eb3f9223954e188874dfea47,sentry-trace_id=8fe1b2cd351944b7b419337d39bab6a3,sentry-sample_rate=1,sentry-transaction=wallet-claim,sentry-sampled=true",
}

API_BASE_URL = "https://prod.interlinklabs.ai/api/v1"
AUTH_URL = f"{API_BASE_URL}/auth/current-user"
TOKEN_INFO_URL = f"{API_BASE_URL}/token/get-token"
CHECK_CLAIMABLE_URL = f"{API_BASE_URL}/token/check-is-claimable"
CLAIM_AIRDROP_URL = f"{API_BASE_URL}/token/claim-airdrop"

USER_DATA_DIR = "user_data"
CLAIM_HISTORY_DIR = "claim_history"
AUTO_CLAIM_TASKS = {}
USER_LOCKS = {}
LAST_LONG_WAIT_NOTIFICATION = {}

log_level = logging.INFO if ENABLE_DETAILED_CONSOLE_LOGS else logging.WARNING
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s (%(module)s.%(funcName)s:%(lineno)d) - %(message)s",
    level=log_level
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

os.makedirs(USER_DATA_DIR, exist_ok=True)
os.makedirs(CLAIM_HISTORY_DIR, exist_ok=True)

# --- Helper Functions (Sama seperti sebelumnya) ---
def get_user_data_file(user_id: int) -> str: return os.path.join(USER_DATA_DIR, f"{user_id}.json")
def get_user_claim_history_file(user_id: int) -> str: return os.path.join(CLAIM_HISTORY_DIR, f"{user_id}_history.json")
async def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in USER_LOCKS: USER_LOCKS[user_id] = asyncio.Lock()
    return USER_LOCKS[user_id]
async def load_user_data(user_id: int) -> dict:
    # ... (kode sama)
    data_file = get_user_data_file(user_id)
    async with await get_user_lock(user_id):
        if os.path.exists(data_file):
            try:
                with open(data_file, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                logger.error(f"Gagal memuat data JSON untuk pengguna {user_id}. File rusak atau format tidak valid.")
                return {}
        return {}

async def save_user_data(user_id: int, data: dict):
    # ... (kode sama)
    data_file = get_user_data_file(user_id)
    async with await get_user_lock(user_id):
        try:
            with open(data_file, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.error(f"Gagal menyimpan data untuk pengguna {user_id}: {e}")

async def load_claim_history(user_id: int) -> list:
    # ... (kode sama)
    history_file = get_user_claim_history_file(user_id)
    async with await get_user_lock(user_id):
        if os.path.exists(history_file):
            try:
                with open(history_file, 'r') as f:
                    return json.load(f)
            except json.JSONDecodeError:
                logger.error(f"Gagal memuat riwayat klaim JSON untuk pengguna {user_id}. File rusak atau format tidak valid.")
                return []
        return []

async def save_claim_history(user_id: int, history: list):
    # ... (kode sama)
    history_file = get_user_claim_history_file(user_id)
    async with await get_user_lock(user_id):
        try:
            with open(history_file, 'w') as f:
                json.dump(history, f, indent=4)
        except Exception as e:
            logger.error(f"Gagal menyimpan riwayat klaim untuk pengguna {user_id}: {e}")

# --- Fungsi API (Sama seperti sebelumnya) ---
async def make_api_request(method: str, url: str, headers: dict, json_data=None, content_data: str | bytes | None = None, timeout=20) -> tuple[dict | None, int | None]:
    # ... (kode sama, pastikan penanganan error sudah baik)
    async with httpx.AsyncClient() as client:
        response_obj = None 
        try:
            if method.upper() == 'GET':
                response_obj = await client.get(url, headers=headers, timeout=timeout)
            elif method.upper() == 'POST':
                if json_data is not None:
                    headers_for_json_post = headers.copy()
                    headers_for_json_post.pop('content-length', None) 
                    headers_for_json_post.pop('Content-Length', None)
                    response_obj = await client.post(url, headers=headers_for_json_post, json=json_data, timeout=timeout)
                elif content_data is not None:
                    response_obj = await client.post(url, headers=headers, content=content_data, timeout=timeout)
                else:
                    response_obj = await client.post(url, headers=headers, timeout=timeout)
            else:
                logger.error(f"Metode HTTP tidak didukung: {method}")
                return None, None
            response_obj.raise_for_status()
            if not response_obj.content: 
                logger.info(f"Menerima respons kosong dari server untuk URL: {url} (Status: {response_obj.status_code})")
                return {"status": "success", "message": "Respons kosong dari server.", "data_from_api": None}, response_obj.status_code
            return response_obj.json(), response_obj.status_code
        except httpx.HTTPStatusError as e:
            logger.error(f"Error HTTP untuk URL {url}: Status {e.response.status_code} - Respons: {e.response.text[:200]}...")
            try:
                error_json = e.response.json()
                return error_json, e.response.status_code
            except json.JSONDecodeError:
                return {"error_message": e.response.text, "status_code_custom": e.response.status_code}, e.response.status_code
        except httpx.TimeoutException as e:
            logger.error(f"Request timeout untuk URL {url}: {e}")
            return {"error_message": "Request ke API timeout."}, None
        except httpx.RequestError as e: 
            logger.error(f"Error koneksi untuk URL {url}: {e}")
            return {"error_message": f"Gagal terhubung ke API: {e}"}, None
        except json.JSONDecodeError as e:
            raw_text = response_obj.text[:200] if response_obj and hasattr(response_obj, 'text') else "N/A"
            status_code = response_obj.status_code if response_obj else None
            logger.error(f"Gagal decode JSON dari URL {url} (Teks Respons Awal: {raw_text}...): {e}")
            return {"error_message": "Format respons dari API tidak valid (bukan JSON).", "raw_response": raw_text}, status_code

def format_timestamp_wib(timestamp_ms: int | float) -> str:
    # ... (kode sama)
    if isinstance(timestamp_ms, (int, float)) and timestamp_ms > 0:
        try:
            dt_utc = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
            dt_wib = dt_utc.astimezone(WIB)
            return dt_wib.strftime('%Y-%m-%d %H:%M:%S %Z')
        except Exception as e:
            logger.warning(f"Gagal format timestamp {timestamp_ms}: {e}")
            return "Timestamp Tidak Valid"
    return "N/A"

async def get_auth_token(user_id: int) -> str | None: return (await load_user_data(user_id)).get("auth_token")
async def api_get_user_profile(auth_token: str) -> tuple[dict | None, str]:
    # ... (kode sama)
    headers = {**BASE_API_HEADERS, 'authorization': f"Bearer {auth_token}"}
    data, status = await make_api_request('GET', AUTH_URL, headers)
    if data and status == 200 and 'data' in data:
        return data['data'], data.get("message", "Profil berhasil dimuat.")
    elif data and 'message' in data:
        return None, f"Gagal: {data['message']} (Status: {status})"
    elif data and 'error_message' in data:
        return None, f"Error API: {data['error_message']}"
    return None, "Gagal memuat profil. Respons tidak diketahui dari server."

async def api_get_token_info(auth_token: str) -> tuple[dict | None, str]:
    # ... (kode sama)
    headers = {**BASE_API_HEADERS, 'authorization': f"Bearer {auth_token}"}
    data, status = await make_api_request('GET', TOKEN_INFO_URL, headers)
    if data and status == 200 and 'data' in data:
        return data['data'], data.get("message", "Informasi token berhasil dimuat.")
    elif data and 'message' in data:
        return None, f"Gagal: {data['message']} (Status: {status})"
    elif data and 'error_message' in data:
        return None, f"Error API: {data['error_message']}"
    return None, "Gagal memuat informasi token. Respons tidak diketahui dari server."

async def api_check_claimable(auth_token: str) -> tuple[dict | None, str, int | None]:
    # ... (kode sama)
    headers = {**BASE_API_HEADERS, 'authorization': f"Bearer {auth_token}"}
    data_wrapper, status_code_response = await make_api_request('GET', CHECK_CLAIMABLE_URL, headers)
    message_to_return = "Gagal memeriksa status klaim. Respons tidak diketahui dari server."
    actual_data_payload = None
    if data_wrapper: 
        if status_code_response == 200 and 'data' in data_wrapper:
            actual_data_payload = data_wrapper['data']
            message_to_return = data_wrapper.get('message', "Status klaim berhasil diperiksa.")
        elif 'message' in data_wrapper:
            message_to_return = f"Gagal: {data_wrapper['message']} (Status: {status_code_response})"
        elif 'error_message' in data_wrapper:
            message_to_return = f"Error API: {data_wrapper['error_message']}"
    return actual_data_payload, message_to_return, status_code_response

async def api_claim_airdrop(auth_token: str) -> tuple[dict | None, str, int | None]:
    # ... (kode sama)
    headers = {**BASE_API_HEADERS, 'authorization': f"Bearer {auth_token}", 'content-length': '0'}
    response_data_dict, status_code_response = await make_api_request('POST', CLAIM_AIRDROP_URL, headers, content_data="", timeout=20)
    message_to_return = "Gagal melakukan klaim. Respons tidak diketahui dari server."
    actual_api_payload = response_data_dict 
    if response_data_dict:
        if status_code_response == 200 and 'data' in response_data_dict and isinstance(response_data_dict.get('data'), bool): 
            message_to_return = response_data_dict.get('message', 'Proses klaim selesai.')
        elif status_code_response == 400 and response_data_dict.get('message') == "TOKEN_CLAIM_TOO_EARLY":
            message_to_return = "Gagal Klaim: Waktu klaim token terlalu dini."
        elif 'message' in response_data_dict: 
            message_to_return = f"Gagal Klaim: {response_data_dict['message']} (Status: {status_code_response})"
        elif 'error_message' in response_data_dict: 
            message_to_return = f"Error API Internal: {response_data_dict['error_message']}"
        elif status_code_response == 200 and response_data_dict.get('message') == "Respons kosong dari server.":
             if response_data_dict.get('data_from_api') is None and 'data' not in response_data_dict: 
                 message_to_return = "Proses klaim selesai dengan respons kosong dari server. Silakan periksa status token Anda." 
    return actual_api_payload, message_to_return, status_code_response

# --- Inline Keyboard Markups (Sama seperti sebelumnya) ---
def main_menu_keyboard(): # ... (kode sama)
    keyboard = [
        [InlineKeyboardButton("👤 Profil Pengguna", callback_data='menu_profile'), InlineKeyboardButton("💰 Informasi Token", callback_data='menu_tokens')],
        [InlineKeyboardButton("📊 Status Klaim", callback_data='menu_claimstatus'), InlineKeyboardButton("🎁 Lakukan Klaim", callback_data='menu_claim')],
        [InlineKeyboardButton("📜 Riwayat Klaim", callback_data='menu_history')],
        [InlineKeyboardButton("🤖 Pengaturan Auto-Claim", callback_data='menu_autoclaim_settings')],
        [InlineKeyboardButton("🔑 Atur Token Autentikasi", callback_data='menu_settoken_info')],
    ]
    return InlineKeyboardMarkup(keyboard)

def autoclaim_menu_keyboard(): # ... (kode sama)
    keyboard = [
        [InlineKeyboardButton("▶️ Aktifkan Auto-Claim", callback_data='autoclaim_start')],
        [InlineKeyboardButton("⏹️ Nonaktifkan Auto-Claim", callback_data='autoclaim_stop')],
        [InlineKeyboardButton("📊 Status Auto-Claim", callback_data='autoclaim_status')], # Perbaikan typo di sini
        [InlineKeyboardButton("🔙 Kembali ke Menu Utama", callback_data='main_menu')],
    ]
    return InlineKeyboardMarkup(keyboard)

def back_to_main_menu_button(): # ... (kode sama)
     return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali ke Menu Utama", callback_data='main_menu')]])

# --- Fungsi Utilitas Pesan (Sama seperti sebelumnya) ---
async def edit_or_send_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup = None, parse_mode: str = ParseMode.MARKDOWN, message_id: int | None = None):
    # ... (kode sama)
    if message_id:
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
            return message_id
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                logger.info(f"Pesan tidak diubah untuk chat {chat_id}, message {message_id}.")
                return message_id 
            logger.warning(f"Gagal mengedit pesan (ID: {message_id}) di chat {chat_id}, mengirim pesan baru: {e}")
        except Exception as e:
            logger.error(f"Error tak terduga saat mengedit pesan (ID: {message_id}) di chat {chat_id}: {e}", exc_info=True)
    new_message = await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    return new_message.message_id

# --- Command Handlers and Action Functions ---
async def send_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int | None = None):
    # ... (kode sama)
    menu_text = (
        "👋 *Selamat Datang di Asisten Bot Interlink Anda!*\n\n"
        "Bot ini dirancang untuk membantu Anda mengelola akun Interlink dengan lebih efisien.\n"
        "Silakan pilih salah satu opsi di bawah ini untuk memulai.\n\n"
        "Pastikan token autentikasi Anda telah diatur dengan benar untuk fungsionalitas penuh."
    )
    await edit_or_send_message(context, chat_id, menu_text, main_menu_keyboard(), message_id=message_id)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): # ... (kode sama)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    logger.info(f"Perintah /start diterima dari pengguna {user_id} di chat {chat_id}.")
    await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
    await send_main_menu(update, context, chat_id)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE): # ... (kode sama)
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    logger.info(f"Perintah /help diterima dari pengguna {user_id} di chat {chat_id}.")
    await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
    await send_main_menu(update, context, chat_id)

def get_ids_from_update(update: Update) -> tuple[int | None, int | None]: # ... (kode sama)
    user_id, chat_id = None, None
    if update.callback_query:
        user_id = update.callback_query.from_user.id
        chat_id = update.callback_query.message.chat_id
    elif update.message:
        user_id = update.message.from_user.id
        chat_id = update.message.chat_id
    return user_id, chat_id

async def set_token_command(update: Update, context: ContextTypes.DEFAULT_TYPE): # ... (kode sama)
    user_id, chat_id = get_ids_from_update(update)
    if not chat_id or not user_id : return 
    logger.info(f"Perintah /settoken diterima dari pengguna {user_id}.")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    if not context.args:
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                "ℹ️ *Informasi Pengaturan Token Autentikasi*\n\n"
                "Untuk mengatur atau memperbarui token Anda, gunakan format perintah:\n"
                "`/settoken TOKEN_ANDA_DISINI`\n\n"
                "*Contoh Penggunaan:*\n"
                "`/settoken eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJsb2dp...`\n\n"
                "Token ini biasanya dapat ditemukan menggunakan Developer Tools pada browser Anda (umumnya pada tab 'Network' atau 'Jaringan') saat Anda masuk ke layanan Interlink."
            ), parse_mode=ParseMode.MARKDOWN
        )
        return
    token = context.args[0]
    if not token.startswith("ey"): 
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Format token yang Anda masukkan tampak tidak valid. Token Bearer umumnya diawali dengan 'ey'. Mohon periksa kembali.")
        return
    user_data = await load_user_data(user_id)
    user_data["auth_token"] = token
    await save_user_data(user_id, user_data)
    await context.bot.send_message(chat_id=chat_id, text="✅ Token autentikasi Anda telah berhasil disimpan.")
    logger.info(f"Token autentikasi berhasil disimpan untuk pengguna {user_id}.")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    verif_message = await context.bot.send_message(chat_id=chat_id, text="Sedang melakukan verifikasi token dengan mengambil data profil Anda...")
    profile_data, message = await api_get_user_profile(token) 
    reply_text = ""
    if profile_data:
        username = profile_data.get('username', 'N/A')
        email = profile_data.get('email', 'N/A')
        reply_text = (f"✅ Token berhasil diverifikasi!\n\n"
                      f"👤 *Nama Pengguna:* `{username}`\n"
                      f"📧 *Alamat Email:* `{email}`")
        logger.info(f"Token untuk pengguna {user_id} berhasil diverifikasi. Username: {username}")
    else:
        reply_text = f"⚠️ Verifikasi token gagal.\n*Pesan dari Server:* `{message}`\n\nPastikan token yang Anda masukkan sudah benar dan masih berlaku."
        logger.warning(f"Gagal verifikasi token untuk pengguna {user_id}. Pesan: {message}")
    await edit_or_send_message(context, chat_id, reply_text, back_to_main_menu_button(), message_id=verif_message.message_id)

async def core_profile_action(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int | None = None):
    # ... (kode sama)
    logger.info(f"Memuat profil untuk pengguna {user_id}.")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    auth_token = await get_auth_token(user_id)
    if not auth_token:
        error_text = "Token autentikasi belum diatur. Silakan atur token Anda melalui opsi 'Atur Token Autentikasi' pada menu utama, atau ketik perintah `/settoken <TOKEN_ANDA>`."
        await edit_or_send_message(context, chat_id, error_text, back_to_main_menu_button(), message_id=message_id)
        return
    if message_id: 
        await edit_or_send_message(context, chat_id, "⏳ Memuat informasi profil Anda...", message_id=message_id, reply_markup=None)
    else: 
        temp_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ Memuat informasi profil Anda...")
        message_id = temp_msg.message_id
    profile_data, message = await api_get_user_profile(auth_token)
    reply_text = ""
    if profile_data:
        reply_text = (f"👤 *Profil Pengguna Akun Interlink Anda*\n\n"
                      f"▫️ *Nama Pengguna:* `{profile_data.get('username', 'N/A')}`\n"
                      f"▫️ *Alamat Email:* `{profile_data.get('email', 'N/A')}`\n"
                      f"▫️ *Peran Akun:* `{profile_data.get('role', 'N/A')}`\n"
                      f"▫️ *ID Login:* `{profile_data.get('loginId', 'N/A')}`\n"
                      f"▫️ *Tanggal Dibuat:* `{profile_data.get('createdAt', 'N/A')}`")
        logger.info(f"Profil berhasil dimuat untuk pengguna {user_id}. Username: {profile_data.get('username', 'N/A')}")
    else:
        reply_text = f" Gagal memuat profil.\n*Pesan dari Server:* `{message}`"
        logger.warning(f"Gagal memuat profil untuk pengguna {user_id}. Pesan: {message}")
    await edit_or_send_message(context, chat_id, reply_text, back_to_main_menu_button(), message_id=message_id)

async def core_tokens_action(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int | None = None):
    # ... (kode sama)
    logger.info(f"Memuat informasi token untuk pengguna {user_id}.")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    auth_token = await get_auth_token(user_id)
    if not auth_token:
        error_text = "Token autentikasi belum diatur. Silakan atur token Anda melalui opsi 'Atur Token Autentikasi' pada menu utama, atau ketik perintah `/settoken <TOKEN_ANDA>`."
        await edit_or_send_message(context, chat_id, error_text, back_to_main_menu_button(), message_id=message_id)
        return
    if message_id:
        await edit_or_send_message(context, chat_id, "⏳ Memuat informasi token Anda...", message_id=message_id, reply_markup=None)
    else:
        temp_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ Memuat informasi token Anda...")
        message_id = temp_msg.message_id
    token_info_payload, message = await api_get_token_info

# Modifikasi fungsi main untuk mendukung webhook jika diperlukan
def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("TELEGRAM_BOT_TOKEN environment variable belum diatur! Bot tidak dapat dijalankan.")
        return
        
    logger.info("Memulai konfigurasi aplikasi bot...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    
    # Handler setup
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settoken", set_token_command))
    application.add_handler(CallbackQueryHandler(button_tap_handler))
    application.add_handler(CommandHandler("profile", profile_command_typed))
    application.add_handler(CommandHandler("tokens", tokens_command_typed))
    application.add_handler(CommandHandler("claimstatus", claim_status_command_typed))
    application.add_handler(CommandHandler("claim", claim_command_typed))
    application.add_handler(CommandHandler("history", history_command_typed))
    application.add_handler(CommandHandler("autoclaim_start", autoclaim_start_command_typed))
    application.add_handler(CommandHandler("autoclaim_stop", autoclaim_stop_command_typed))
    application.add_handler(CommandHandler("autoclaim_status", autoclaim_status_command_typed))
    
    logger.info("Aplikasi bot berhasil dikonfigurasi. Memulai polling...")
    
    # Gunakan polling untuk Render (lebih mudah daripada webhook)
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
