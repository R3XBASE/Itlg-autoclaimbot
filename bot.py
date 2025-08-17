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
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise ValueError("TELEGRAM_BOT_TOKEN must be set as an environment variable.")

ENABLE_DETAILED_CONSOLE_LOGS = os.environ.get("ENABLE_DETAILED_CONSOLE_LOGS", "True").lower() == "true"
WIB = pytz.timezone('Asia/Jakarta')

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

# For Railway persistence: Use a mounted volume path if available, else fallback to local
DATA_VOLUME_PATH = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", ".")
USER_DATA_DIR = os.path.join(DATA_VOLUME_PATH, "user_data")
CLAIM_HISTORY_DIR = os.path.join(DATA_VOLUME_PATH, "claim_history")
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

# --- Helper Functions ---
def get_user_data_file(user_id: int) -> str: 
    return os.path.join(USER_DATA_DIR, f"{user_id}.json")

def get_user_claim_history_file(user_id: int) -> str: 
    return os.path.join(CLAIM_HISTORY_DIR, f"{user_id}_history.json")

async def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in USER_LOCKS: 
        USER_LOCKS[user_id] = asyncio.Lock()
    return USER_LOCKS[user_id]

async def load_user_data(user_id: int) -> dict:
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
    data_file = get_user_data_file(user_id)
    async with await get_user_lock(user_id):
        try:
            with open(data_file, 'w') as f:
                json.dump(data, f, indent=4)
        except Exception as e:
            logger.error(f"Gagal menyimpan data untuk pengguna {user_id}: {e}")

async def load_claim_history(user_id: int) -> list:
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
    history_file = get_user_claim_history_file(user_id)
    async with await get_user_lock(user_id):
        try:
            with open(history_file, 'w') as f:
                json.dump(history, f, indent=4)
        except Exception as e:
            logger.error(f"Gagal menyimpan riwayat klaim untuk pengguna {user_id}: {e}")

# --- Fungsi API ---
async def make_api_request(method: str, url: str, headers: dict, json_data=None, content_data: str | bytes | None = None, timeout=20) -> tuple[dict | None, int | None]:
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
        except Exception as e:
            logger.error(f"Unexpected error in make_api_request for URL {url}: {e}", exc_info=True)
            return {"error_message": f"Unexpected API error: {str(e)}"}, None

def format_timestamp_wib(timestamp_ms: int | float) -> str:
    if isinstance(timestamp_ms, (int, float)) and timestamp_ms > 0:
        try:
            dt_utc = datetime.fromtimestamp(timestamp_ms / 1000, tz=timezone.utc)
            dt_wib = dt_utc.astimezone(WIB)
            return dt_wib.strftime('%Y-%m-%d %H:%M:%S %Z')
        except Exception as e:
            logger.warning(f"Gagal format timestamp {timestamp_ms}: {e}")
            return "Timestamp Tidak Valid"
    return "N/A"

async def get_auth_token(user_id: int) -> str | None: 
    return (await load_user_data(user_id)).get("auth_token")

async def api_get_user_profile(auth_token: str) -> tuple[dict | None, str]:
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

# --- Inline Keyboard Markups ---
def main_menu_keyboard(): 
    keyboard = [
        [InlineKeyboardButton("👤 Profil Pengguna", callback_data='menu_profile'), InlineKeyboardButton("💰 Informasi Token", callback_data='menu_tokens')],
        [InlineKeyboardButton("📊 Status Klaim", callback_data='menu_claimstatus'), InlineKeyboardButton("🎁 Lakukan Klaim", callback_data='menu_claim')],
        [InlineKeyboardButton("📜 Riwayat Klaim", callback_data='menu_history')],
        [InlineKeyboardButton("🤖 Pengaturan Auto-Claim", callback_data='menu_autoclaim_settings')],
        [InlineKeyboardButton("🔑 Atur Token Autentikasi", callback_data='menu_settoken_info')],
    ]
    return InlineKeyboardMarkup(keyboard)

def autoclaim_menu_keyboard(): 
    keyboard = [
        [InlineKeyboardButton("▶️ Aktifkan Auto-Claim", callback_data='autoclaim_start')],
        [InlineKeyboardButton("⏹️ Nonaktifkan Auto-Claim", callback_data='autoclaim_stop')],
        [InlineKeyboardButton("📊 Status Auto-Claim", callback_data='autoclaim_status')],
        [InlineKeyboardButton("🔙 Kembali ke Menu Utama", callback_data='main_menu')],
    ]
    return InlineKeyboardMarkup(keyboard)

def back_to_main_menu_button(): 
     return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali ke Menu Utama", callback_data='main_menu')]])

# --- Fungsi Utilitas Pesan ---
async def edit_or_send_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, reply_markup: InlineKeyboardMarkup = None, parse_mode: str = ParseMode.MARKDOWN, message_id: int | None = None):
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
    menu_text = (
        "👋 *Selamat Datang di Asisten Bot Interlink Anda!*\n\n"
        "Bot ini dirancang untuk membantu Anda mengelola akun Interlink dengan lebih efisien.\n"
        "Silakan pilih salah satu opsi di bawah ini untuk memulai.\n\n"
        "Pastikan token autentikasi Anda telah diatur dengan benar untuk fungsionalitas penuh."
    )
    await edit_or_send_message(context, chat_id, menu_text, main_menu_keyboard(), message_id=message_id)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    logger.info(f"Perintah /start diterima dari pengguna {user_id} di chat {chat_id}.")
    await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
    await send_main_menu(update, context, chat_id)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    logger.info(f"Perintah /help diterima dari pengguna {user_id} di chat {chat_id}.")
    await context.bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
    await send_main_menu(update, context, chat_id)

def get_ids_from_update(update: Update) -> tuple[int | None, int | None]: 
    user_id, chat_id = None, None
    if update.callback_query:
        user_id = update.callback_query.from_user.id
        chat_id = update.callback_query.message.chat_id
    elif update.message:
        user_id = update.message.from_user.id
        chat_id = update.message.chat_id
    return user_id, chat_id

async def set_token_command(update: Update, context: ContextTypes.DEFAULT_TYPE): 
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
    token_info_payload, message = await api_get_token_info(auth_token)
    reply_text = ""
    if token_info_payload:
        silver = token_info_payload.get('interlinkSilverTokenAmount', 0)
        gold = token_info_payload.get('interlinkGoldTokenAmount', 0)
        diamond = token_info_payload.get('interlinkDiamondTokenAmount', 0)
        total = silver + gold + diamond
        last_claim_time_wib = format_timestamp_wib(token_info_payload.get('lastClaimTime'))
        reply_text = (f"💰 *Informasi Saldo Token Anda*\n\n"
                      f"🥈 *Token Silver:* `{silver}`\n"
                      f"🌟 *Token Gold:* `{gold}`\n"
                      f"💎 *Token Diamond:* `{diamond}`\n"
                      f"--------------------\n"
                      f"📊 *Total Estimasi Token:* `{total}`\n\n"
                      f"⏱️ *Waktu Klaim Terakhir:* {last_claim_time_wib}\n"
                      f"🎁 *Klaim Token Login Pertama:* {'Sudah' if token_info_payload.get('isFirstLoginTokenClaim') else 'Belum'}")
        logger.info(f"Informasi token berhasil dimuat untuk pengguna {user_id}.")
    else:
        reply_text = f"Gagal memuat informasi token.\n*Pesan dari Server:* `{message}`"
        logger.warning(f"Gagal memuat informasi token untuk pengguna {user_id}. Pesan: {message}")
    await edit_or_send_message(context, chat_id, reply_text, back_to_main_menu_button(), message_id=message_id)

async def core_claim_status_action(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int | None = None):
    logger.info(f"Memeriksa status klaim untuk pengguna {user_id}.")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    auth_token = await get_auth_token(user_id)
    if not auth_token:
        error_text = "Token autentikasi belum diatur. Silakan atur token Anda melalui opsi 'Atur Token Autentikasi' pada menu utama, atau ketik perintah `/settoken <TOKEN_ANDA>`."
        await edit_or_send_message(context, chat_id, error_text, back_to_main_menu_button(), message_id=message_id)
        return
    if message_id:
        await edit_or_send_message(context, chat_id, "⏳ Memeriksa status klaim token Anda...", message_id=message_id, reply_markup=None)
    else:
        temp_msg = await context.bot.send_message(chat_id=chat_id, text="⏳ Memeriksa status klaim token Anda...")
        message_id = temp_msg.message_id
    claim_status_payload, message, _ = await api_check_claimable(auth_token)
    reply_text = ""
    if claim_status_payload:
        is_claimable = claim_status_payload.get('isClaimable', False)
        next_frame_ms = claim_status_payload.get('nextFrame', 0)
        next_frame_wib = format_timestamp_wib(next_frame_ms)
        reply_text = f"📊 *Status Klaim Token Saat Ini*\n\n"
        if is_claimable:
            reply_text += "✅ *Klaim Tersedia!* Anda dapat melakukan klaim sekarang melalui tombol 'Lakukan Klaim'."
            logger.info(f"Status klaim untuk pengguna {user_id}: Tersedia.")
        else:
            reply_text += f"❌ *Klaim Belum Tersedia.*\n"
            if next_frame_ms > 0:
                current_time_ms = int(time.time() * 1000)
                wait_time_ms = next_frame_ms - current_time_ms
                if wait_time_ms > 0:
                    hours, remainder = divmod(int(wait_time_ms / 1000), 3600)
                    minutes, seconds = divmod(remainder, 60)
                    countdown_str = f"{hours:02d} jam, {minutes:02d} menit, {seconds:02d} detik"
                    reply_text += f"⏳ *Jadwal Klaim Berikutnya:* {next_frame_wib}\n   (Perkiraan waktu tunggu: {countdown_str})"
                else:
                    reply_text += f"⏳ *Jadwal Klaim Berikutnya:* {next_frame_wib} (Waktu penjadwalan sepertinya sudah terlewat. Silakan coba periksa kembali atau lakukan klaim jika yakin.)"
            else:
                reply_text += "Informasi mengenai jadwal klaim berikutnya tidak tersedia saat ini."
            logger.info(f"Status klaim untuk pengguna {user_id}: Belum tersedia. Berikutnya: {next_frame_wib}")
    else:
        reply_text = f"Gagal memeriksa status klaim.\n*Pesan dari Server:* `{message}`"
        logger.warning(f"Gagal memeriksa status klaim untuk pengguna {user_id}. Pesan: {message}")
    await edit_or_send_message(context, chat_id, reply_text, back_to_main_menu_button(), message_id=message_id)

async def core_claim_action(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int | None = None):
    logger.info(f"Memulai proses klaim manual untuk pengguna {user_id}.")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    auth_token = await get_auth_token(user_id)
    if not auth_token:
        error_text = "Token autentikasi belum diatur. Silakan atur token Anda melalui opsi 'Atur Token Autentikasi' pada menu utama, atau ketik perintah `/settoken <TOKEN_ANDA>`."
        await edit_or_send_message(context, chat_id, error_text, back_to_main_menu_button(), message_id=message_id)
        return

    # Ambil saldo token sebelum klaim
    tokens_before_payload, msg_before = await api_get_token_info(auth_token)
    if not tokens_before_payload:
        logger.warning(f"Gagal mendapatkan saldo token sebelum klaim untuk pengguna {user_id}. Pesan: {msg_before}")
        await edit_or_send_message(context, chat_id, f"Gagal mendapatkan saldo token awal: {msg_before}", back_to_main_menu_button(), message_id=message_id)
        return

    s_before = tokens_before_payload.get('interlinkSilverTokenAmount', 0)
    g_before = tokens_before_payload.get('interlinkGoldTokenAmount', 0)
    d_before = tokens_before_payload.get('interlinkDiamondTokenAmount', 0)

    # Edit pesan yang ada atau kirim pesan baru "memproses..."
    processing_text = "⏳ Sedang memproses permintaan klaim Anda..."
    if message_id:
        await edit_or_send_message(context, chat_id, processing_text, message_id=message_id, reply_markup=None)
    else:
        temp_msg = await context.bot.send_message(chat_id=chat_id, text=processing_text)
        message_id = temp_msg.message_id

    claim_api_response, message_from_api_call, status_code = await api_claim_airdrop(auth_token) 
    
    claim_successful = False
    if claim_api_response and isinstance(claim_api_response.get('data'), bool):
        claim_successful = claim_api_response.get('data', False)

    claimed_s, claimed_g, claimed_d = 0, 0, 0
    tokens_after_payload = None

    if claim_successful:
        logger.info(f"Klaim berhasil (API) untuk pengguna {user_id}. Pesan: {message_from_api_call}")
        await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        tokens_after_payload, msg_after = await api_get_token_info(auth_token)
        if tokens_after_payload:
            s_after = tokens_after_payload.get('interlinkSilverTokenAmount', 0)
            g_after = tokens_after_payload.get('interlinkGoldTokenAmount', 0)
            d_after = tokens_after_payload.get('interlinkDiamondTokenAmount', 0)
            
            claimed_s = s_after - s_before
            claimed_g = g_after - g_before
            claimed_d = d_after - d_before
            logger.info(f"Pengguna {user_id} memperoleh: Silver +{claimed_s}, Gold +{claimed_g}, Diamond +{claimed_d}")
        else:
            logger.warning(f"Gagal mendapatkan saldo token setelah klaim berhasil untuk pengguna {user_id}. Pesan: {msg_after}")
            message_from_api_call += " (Namun, gagal memverifikasi saldo baru)"
    else:
        logger.warning(f"Klaim gagal (API) untuk pengguna {user_id}. Pesan: {message_from_api_call}")

    history_entry = {
        'timestamp': datetime.now(WIB).strftime('%Y-%m-%d %H:%M:%S %Z'),
        'success': claim_successful, 
        'message': message_from_api_call,
        'claimed_silver': claimed_s if claim_successful else 0,
        'claimed_gold': claimed_g if claim_successful else 0,
        'claimed_diamond': claimed_d if claim_successful else 0,
        'total_silver_after': tokens_after_payload.get('interlinkSilverTokenAmount', s_before) if tokens_after_payload else s_before,
        'total_gold_after': tokens_after_payload.get('interlinkGoldTokenAmount', g_before) if tokens_after_payload else g_before,
        'total_diamond_after': tokens_after_payload.get('interlinkDiamondTokenAmount', d_before) if tokens_after_payload else d_before,
    }
    
    user_history = await load_claim_history(user_id)
    user_history.append(history_entry)
    await save_claim_history(user_id, user_history)
    
    reply_text = f"🎁 *Hasil Proses Klaim Token*\n\n{message_from_api_call}"
    if claim_successful:
        reply_text += f"\n\n🎉 Selamat! Anda berhasil mengklaim:\n"
        if claimed_s > 0: reply_text += f"🥈 `+{claimed_s}` Silver\n"
        if claimed_g > 0: reply_text += f"🌟 `+{claimed_g}` Gold\n"
        if claimed_d > 0: reply_text += f"💎 `+{claimed_d}` Diamond\n"
        if claimed_s <= 0 and claimed_g <= 0 and claimed_d <= 0: 
             reply_text += "Tidak ada perubahan saldo token yang terdeteksi setelah klaim ini.\n"
    else:
        reply_text += "\n\n⚠️ Maaf, terjadi masalah saat memproses klaim Anda. Silakan periksa pesan di atas."
    
    await edit_or_send_message(context, chat_id, reply_text, back_to_main_menu_button(), message_id=message_id)
    
    if claim_successful :
        await context.bot.send_message(chat_id=chat_id, text="Anda dapat memeriksa pembaruan saldo token Anda melalui opsi 'Informasi Token' di menu utama.", reply_markup=None)

async def core_history_action(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int | None = None):
    logger.info(f"Memuat riwayat klaim untuk pengguna {user_id}.")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    user_history = await load_claim_history(user_id)
    reply_text = ""
    if not user_history:
        reply_text = "Belum terdapat riwayat klaim untuk akun Anda."
        logger.info(f"Tidak ada riwayat klaim ditemukan untuk pengguna {user_id}.")
    else:
        reply_text = "📜 *Riwayat 10 Transaksi Klaim Terakhir Anda*\n\n"
        for entry_idx, entry in enumerate(reversed(user_history[-10:])): 
            status_icon = "✅ Berhasil" if entry.get('success') else "❌ Gagal"
            reply_text += (
                f"*Transaksi #{len(user_history) - entry_idx}*\n" 
                f"🗓️ *Waktu:* `{entry.get('timestamp', 'N/A')}`\n"
                f"🚦 *Status:* {status_icon}\n"
            )
            if entry.get('success'):
                cs = entry.get('claimed_silver', 0)
                cg = entry.get('claimed_gold', 0)
                cd = entry.get('claimed_diamond', 0)
                
                gained_text_parts = []
                if cs > 0: gained_text_parts.append(f"🥈 `+{cs}` S")
                if cg > 0: gained_text_parts.append(f"🌟 `+{cg}` G")
                if cd > 0: gained_text_parts.append(f"💎 `+{cd}` D")
                
                if gained_text_parts:
                    reply_text += f"➕ *Diperoleh:* {', '.join(gained_text_parts)}\n"
                else: 
                    reply_text += f"➕ *Diperoleh:* Tidak ada perubahan saldo terdeteksi.\n"

                reply_text += (
                    f"💰 *Total Setelah:* S: `{entry.get('total_silver_after', 'N/A')}` "
                    f"G: `{entry.get('total_gold_after', 'N/A')}` "
                    f"D: `{entry.get('total_diamond_after', 'N/A')}`\n"
                )
            reply_text += f"💬 *Pesan Server:* `{entry.get('message', 'N/A')}`\n"
            reply_text += "--------------------\n"

        if len(user_history) > 10:
            reply_text += f"\n\n*[Menampilkan 10 dari total {len(user_history)} riwayat klaim]*"
        logger.info(f"Riwayat klaim berhasil dimuat untuk pengguna {user_id}. Jumlah entri: {len(user_history)}.")
            
    await edit_or_send_message(context, chat_id, reply_text, back_to_main_menu_button(), message_id=message_id)

async def auto_claim_task_function(user_id: int, context_like_object):
    logger.info(f"Tugas Auto-Claim dimulai untuk pengguna {user_id}.")
    auth_token = await get_auth_token(user_id)
    bot_instance = context_like_object.bot if hasattr(context_like_object, 'bot') else context_like_object.application.bot

    if not auth_token:
        logger.warning(f"Auto-Claim untuk pengguna {user_id} dihentikan: Token autentikasi tidak ditemukan.")
        await bot_instance.send_message(user_id, "⚠️ *Auto-Claim Dihentikan* ⚠️\nToken autentikasi Anda tidak ditemukan. Mohon atur ulang token Anda melalui menu utama untuk melanjutkan layanan auto-claim.")
        user_data = await load_user_data(user_id)
        user_data["auto_claim_active"] = False
        await save_user_data(user_id, user_data)
        return

    user_data = await load_user_data(user_id)

    while user_data.get("auto_claim_active", False):
        sleep_after_action_sec = random.uniform(5*60, 10*60) 
        try:
            await bot_instance.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
            current_auth_token = await get_auth_token(user_id)
            if not current_auth_token:
                raise ValueError("Token autentikasi menjadi tidak tersedia saat proses auto-claim.")

            claim_status_payload, msg_claim_stat, status_code_check = await api_check_claimable(current_auth_token)
            
            if status_code_check == 401: 
                logger.warning(f"Auto-Claim untuk pengguna {user_id}: Token tidak valid (401) saat cek status. Auto-Claim dihentikan.")
                await bot_instance.send_message(user_id, "⚠️ *Auto-Claim Dihentikan* ⚠️\nToken autentikasi Anda tidak valid atau telah kedaluwarsa. Mohon perbarui token Anda.")
                user_data["auto_claim_active"] = False
                await save_user_data(user_id, user_data)
                break 

            if claim_status_payload and claim_status_payload.get('isClaimable'):
                logger.info(f"Auto-Claim untuk pengguna {user_id}: Terdeteksi dapat melakukan klaim.")
                
                tokens_before_payload_auto, msg_before_auto = await api_get_token_info(current_auth_token)
                if not tokens_before_payload_auto:
                    logger.warning(f"Auto-Claim {user_id}: Gagal mendapatkan saldo token sebelum klaim. Pesan: {msg_before_auto}. Melewati siklus ini.")
                    await bot_instance.send_message(user_id, f"🤖 *Auto-Claim*: Gagal mendapatkan info token sebelum klaim. Mencoba lagi nanti.")
                    await asyncio.sleep(random.uniform(3*60, 7*60)) 
                    continue 

                s_before_auto = tokens_before_payload_auto.get('interlinkSilverTokenAmount', 0)
                g_before_auto = tokens_before_payload_auto.get('interlinkGoldTokenAmount', 0)
                d_before_auto = tokens_before_payload_auto.get('interlinkDiamondTokenAmount', 0)

                await bot_instance.send_message(user_id, "🤖 *Auto-Claim*: Terdeteksi dapat melakukan klaim! Sedang mencoba memproses klaim Anda...")
                await asyncio.sleep(random.uniform(5, 15)) 
                await bot_instance.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
                
                claim_api_response, claim_msg_from_api, claim_status_code = await api_claim_airdrop(current_auth_token)
                
                claim_successful_auto = False
                if claim_api_response and isinstance(claim_api_response.get('data'), bool):
                    claim_successful_auto = claim_api_response.get('data', False)

                claimed_s_auto, claimed_g_auto, claimed_d_auto = 0, 0, 0
                tokens_after_payload_auto = None

                if claim_successful_auto:
                    logger.info(f"Auto-Claim untuk pengguna {user_id}: Klaim berhasil (API). Pesan: {claim_msg_from_api}")
                    await bot_instance.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)
                    tokens_after_payload_auto, msg_after_auto = await api_get_token_info(current_auth_token)
                    if tokens_after_payload_auto:
                        s_after_auto = tokens_after_payload_auto.get('interlinkSilverTokenAmount', 0)
                        g_after_auto = tokens_after_payload_auto.get('interlinkGoldTokenAmount', 0)
                        d_after_auto = tokens_after_payload_auto.get('interlinkDiamondTokenAmount', 0)
                        
                        claimed_s_auto = s_after_auto - s_before_auto
                        claimed_g_auto = g_after_auto - g_before_auto
                        claimed_d_auto = d_after_auto - d_before_auto
                        logger.info(f"Auto-Claim {user_id} memperoleh: Silver +{claimed_s_auto}, Gold +{claimed_g_auto}, Diamond +{claimed_d_auto}")
                    else:
                        logger.warning(f"Auto-Claim {user_id}: Gagal mendapatkan saldo token setelah klaim berhasil. Pesan: {msg_after_auto}")
                        claim_msg_from_api += " (Namun, gagal memverifikasi saldo baru)"
                else:
                    logger.warning(f"Auto-Claim untuk pengguna {user_id}: Klaim gagal (API). Pesan: {claim_msg_from_api}")

                history_entry_auto = {
                    'timestamp': datetime.now(WIB).strftime('%Y-%m-%d %H:%M:%S %Z'),
                    'success': claim_successful_auto, 
                    'message': claim_msg_from_api,
                    'claimed_silver': claimed_s_auto if claim_successful_auto else 0,
                    'claimed_gold': claimed_g_auto if claim_successful_auto else 0,
                    'claimed_diamond': claimed_d_auto if claim_successful_auto else 0,
                    'total_silver_after': tokens_after_payload_auto.get('interlinkSilverTokenAmount', s_before_auto) if tokens_after_payload_auto else s_before_auto,
                    'total_gold_after': tokens_after_payload_auto.get('interlinkGoldTokenAmount', g_before_auto) if tokens_after_payload_auto else g_before_auto,
                    'total_diamond_after': tokens_after_payload_auto.get('interlinkDiamondTokenAmount', d_before_auto) if tokens_after_payload_auto else d_before_auto,
                }
                
                current_history = await load_claim_history(user_id)
                current_history.append(history_entry_auto)
                await save_claim_history(user_id, current_history)
                
                if claim_successful_auto:
                    success_msg_parts = ["🎉 *Auto-Claim Berhasil!*"]
                    gained_tokens_text_parts = []
                    if claimed_s_auto > 0: gained_tokens_text_parts.append(f"🥈 `+{claimed_s_auto}` Silver")
                    if claimed_g_auto > 0: gained_tokens_text_parts.append(f"🌟 `+{claimed_g_auto}` Gold")
                    if claimed_d_auto > 0: gained_tokens_text_parts.append(f"💎 `+{claimed_d_auto}` Diamond")

                    if gained_tokens_text_parts:
                        success_msg_parts.append(f"Anda memperoleh: {', '.join(gained_tokens_text_parts)}.")
                    else:
                        success_msg_parts.append("Tidak ada perubahan saldo token yang terdeteksi setelah klaim ini.")
                    success_msg_parts.append(f"\nDetail Pesan Server: {claim_msg_from_api}")
                    await bot_instance.send_message(user_id, "\n".join(success_msg_parts), parse_mode=ParseMode.MARKDOWN)
                else:
                    await bot_instance.send_message(user_id, f"⚠️ *Auto-Claim Gagal Diproses.*\nPesan Server: {claim_msg_from_api}")
                    if claim_status_code == 401: 
                        logger.warning(f"Auto-Claim untuk pengguna {user_id}: Token tidak valid (401) saat proses klaim. Auto-Claim dihentikan.")
                        await bot_instance.send_message(user_id, "⚠️ *Auto-Claim Dihentikan* ⚠️\nToken autentikasi Anda tidak valid saat mencoba klaim. Mohon perbarui token Anda.")
                        user_data["auto_claim_active"] = False
                        await save_user_data(user_id, user_data)
                        break

                sleep_after_action_sec = random.uniform(5*60, 10*60) 
                logger.info(f"Auto-Claim untuk pengguna {user_id}: Menunggu {int(sleep_after_action_sec/60)} menit.")
                await bot_instance.send_message(user_id, f"🤖 *Auto-Claim*: Proses selesai. Pengecekan berikutnya sekitar {int(sleep_after_action_sec/60)} menit lagi.")
            
            elif claim_status_payload: 
                next_frame_ms = claim_status_payload.get('nextFrame', 0)
                if next_frame_ms > 0:
                    current_time_ms = int(time.time() * 1000)
                    wait_time_ms = next_frame_ms - current_time_ms
                    wait_time_sec = max(0, wait_time_ms / 1000) 
                    if wait_time_sec > 0:
                        wait_hours, rem_secs = divmod(int(wait_time_sec), 3600)
                        wait_mins, wait_secs_final = divmod(rem_secs, 60)
                        countdown_str = f"{wait_hours:02d} jam, {wait_mins:02d} menit, {wait_secs_final:02d} detik"
                        notify_user = False
                        current_time_for_notification = time.time()
                        last_notif_time = LAST_LONG_WAIT_NOTIFICATION.get(user_id, 0)
                        if wait_time_sec > 60 * 60 * 1: 
                            if current_time_for_notification - last_notif_time > 2 * 3600: notify_user = True
                            sleep_after_action_sec = random.uniform(25*60, 35*60) 
                        elif wait_time_sec > 15 * 60 : 
                            if current_time_for_notification - last_notif_time > 45 * 60: notify_user = True
                            sleep_after_action_sec = random.uniform(5*60, 10*60) 
                        else: 
                            notify_user = True 
                            sleep_after_action_sec = wait_time_sec + random.uniform(10, 45) 
                        if notify_user:
                            logger.info(f"Auto-Claim untuk pengguna {user_id}: Klaim berikutnya dalam {countdown_str}. Notifikasi dikirim.")
                            await bot_instance.send_message(user_id, f"🤖 *Auto-Claim*: Klaim berikutnya dijadwalkan dalam {countdown_str}. Bot akan memeriksa kembali secara otomatis.")
                            LAST_LONG_WAIT_NOTIFICATION[user_id] = current_time_for_notification
                        logger.info(f"Auto-Claim untuk pengguna {user_id}: Menunggu selama {int(sleep_after_action_sec)} detik.")
                    else: 
                        logger.info(f"Auto-Claim untuk pengguna {user_id}: Jadwal klaim terdeteksi sudah lewat. Memeriksa ulang segera.")
                        sleep_after_action_sec = random.uniform(30, 90) 
                else: 
                    logger.warning(f"Auto-Claim untuk pengguna {user_id}: Tidak dapat menentukan jadwal klaim berikutnya. Menunggu {int(sleep_after_action_sec/60)} menit.")
                    await bot_instance.send_message(user_id, f"🤖 *Auto-Claim*: Tidak dapat menentukan jadwal klaim berikutnya. Mencoba lagi dalam {int(sleep_after_action_sec/60)} menit.")
            else: 
                 logger.error(f"Auto-Claim untuk pengguna {user_id}: Gagal memeriksa status klaim. Pesan Server: {msg_claim_stat}. Mencoba lagi dalam {int(sleep_after_action_sec/60)} menit.")
                 await bot_instance.send_message(user_id, f"🤖 *Auto-Claim*: Gagal memeriksa status klaim dari server ({msg_claim_stat}). Mencoba lagi dalam {int(sleep_after_action_sec/60)} menit.")
                 if status_code_check == 429: 
                    logger.warning(f"Auto-Claim untuk pengguna {user_id}: Menerima status 429 (Too Many Requests) saat cek status. Menambah waktu tunggu.")
                    sleep_after_action_sec = random.uniform(45*60, 75*60) 
                    await bot_instance.send_message(user_id, f"🤖 *Auto-Claim*: Terlalu banyak permintaan ke server. Waktu tunggu ditambah menjadi sekitar {int(sleep_after_action_sec/60)} menit.")
        
        except ValueError as e: 
            logger.error(f"Auto-Claim untuk pengguna {user_id} mengalami error konfigurasi: {e}")
            await bot_instance.send_message(user_id, f"⚠️ *Auto-Claim Dihentikan* ⚠️\nTerjadi error konfigurasi: {e}. Mohon periksa pengaturan token Anda.")
            user_data["auto_claim_active"] = False
            await save_user_data(user_id, user_data)
            break 
        except httpx.HTTPStatusError as e: 
            logger.error(f"Error HTTPStatus dalam Auto-Claim untuk pengguna {user_id}: Status {e.response.status_code} - {e.response.text[:150]}...", exc_info=ENABLE_DETAILED_CONSOLE_LOGS) 
            await bot_instance.send_message(user_id, f"🤖 *Auto-Claim*: Terjadi error HTTP ({e.response.status_code}) saat berkomunikasi dengan server. Akan dicoba kembali nanti.")
            if e.response.status_code == 401: 
                user_data["auto_claim_active"] = False
                await save_user_data(user_id, user_data)
                await bot_instance.send_message(user_id, "⚠️ *Auto-Claim Dihentikan* ⚠️\nToken autentikasi Anda tidak valid (Error 401). Mohon perbarui token Anda.")
                break
            elif e.response.status_code == 429: 
                sleep_after_action_sec = random.uniform(45*60, 75*60) 
                await bot_instance.send_message(user_id, f"🤖 *Auto-Claim*: Terlalu banyak permintaan ke server (Error 429). Waktu tunggu ditambah menjadi sekitar {int(sleep_after_action_sec/60)} menit.")
        except Exception as e: 
            logger.error(f"Error tak terduga dalam Auto-Claim untuk pengguna {user_id}: {e}", exc_info=ENABLE_DETAILED_CONSOLE_LOGS)
            error_message_short = str(e).split('\n')[0][:150] 
            await bot_instance.send_message(user_id, f"🤖 *Auto-Claim*: Terjadi kendala tak terduga. Sistem akan mencoba kembali dalam beberapa menit.\nDetail: `{error_message_short}`")
            sleep_after_action_sec = random.uniform(2*60, 5*60) 

        logger.debug(f"Auto-Claim untuk pengguna {user_id}: Siklus selesai, tidur selama {sleep_after_action_sec:.2f} detik.")
        await asyncio.sleep(sleep_after_action_sec)
        user_data = await load_user_data(user_id) 

    logger.info(f"Tugas Auto-Claim dihentikan untuk pengguna {user_id}.")
    if user_id in AUTO_CLAIM_TASKS: del AUTO_CLAIM_TASKS[user_id]
    if user_id in LAST_LONG_WAIT_NOTIFICATION: del LAST_LONG_WAIT_NOTIFICATION[user_id]

async def core_autoclaim_start(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int | None = None):
    logger.info(f"Memulai Auto-Claim untuk pengguna {user_id}.")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    auth_token = await get_auth_token(user_id)
    msg_text = ""
    if not auth_token:
        msg_text = "Token autentikasi belum diatur. Silakan atur token Anda terlebih dahulu."
    else:
        user_data = await load_user_data(user_id)
        if user_data.get("auto_claim_active", False) and user_id in AUTO_CLAIM_TASKS and AUTO_CLAIM_TASKS[user_id] and not AUTO_CLAIM_TASKS[user_id].done():
            msg_text = "✅ Layanan Auto-Claim sudah aktif untuk akun Anda."
        else:
            user_data["auto_claim_active"] = True
            await save_user_data(user_id, user_data)
            class SimpleBotContext: 
                def __init__(self, bot_instance, app_instance): self.bot = bot_instance; self.application = app_instance
            simple_context = SimpleBotContext(context.bot, context.application)
            task = asyncio.create_task(auto_claim_task_function(user_id, simple_context))
            AUTO_CLAIM_TASKS[user_id] = task
            msg_text = "▶️ Layanan Auto-Claim berhasil diaktifkan! Bot akan secara otomatis memeriksa dan melakukan klaim untuk Anda sesuai jadwal."
            logger.info(f"Auto-Claim diaktifkan oleh pengguna {user_id}.")
    await edit_or_send_message(context, chat_id, msg_text, autoclaim_menu_keyboard(), message_id=message_id)

async def core_autoclaim_stop(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int | None = None):
    logger.info(f"Menghentikan Auto-Claim untuk pengguna {user_id}.")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    user_data = await load_user_data(user_id)
    msg_text = ""
    is_currently_active_in_data = user_data.get("auto_claim_active", False)
    task_object = AUTO_CLAIM_TASKS.get(user_id)
    is_task_running = task_object and not task_object.done()
    if not is_currently_active_in_data and not is_task_running:
        msg_text = "ℹ️ Layanan Auto-Claim memang sedang tidak aktif untuk akun Anda."
    else:
        user_data["auto_claim_active"] = False
        await save_user_data(user_id, user_data)
        if is_task_running:
            logger.info(f"Membatalkan tugas Auto-Claim aktif untuk pengguna {user_id}.")
            task_object.cancel()
            try: await task_object 
            except asyncio.CancelledError: logger.info(f"Tugas Auto-Claim untuk pengguna {user_id} berhasil dibatalkan.")
            except Exception as e: logger.error(f"Error saat proses pembatalan tugas Auto-Claim untuk pengguna {user_id}: {e}")
        if user_id in AUTO_CLAIM_TASKS: del AUTO_CLAIM_TASKS[user_id]
        if user_id in LAST_LONG_WAIT_NOTIFICATION: del LAST_LONG_WAIT_NOTIFICATION[user_id]
        msg_text = "⏹️ Layanan Auto-Claim telah berhasil dinonaktifkan."
        logger.info(f"Auto-Claim dinonaktifkan oleh pengguna {user_id}.")
    await edit_or_send_message(context, chat_id, msg_text, autoclaim_menu_keyboard(), message_id=message_id)

async def core_autoclaim_status(user_id: int, chat_id: int, context: ContextTypes.DEFAULT_TYPE, message_id: int | None = None):
    logger.info(f"Memeriksa status Auto-Claim untuk pengguna {user_id}.")
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    user_data = await load_user_data(user_id)
    msg_text = ""
    is_active_in_data = user_data.get("auto_claim_active", False)
    task = AUTO_CLAIM_TASKS.get(user_id)
    task_running = task and not task.done()
    if is_active_in_data and task_running: msg_text = "✅ Layanan Auto-Claim saat ini dalam status *AKTIF*."
    elif is_active_in_data and not task_running: 
        msg_text = "⚠️ Layanan Auto-Claim tercatat aktif pada data Anda, namun tugas latar belakang sepertinya tidak berjalan. Anda dapat mencoba menonaktifkan lalu mengaktifkannya kembali untuk me-reset."
        logger.warning(f"Diskrepansi status Auto-Claim untuk pengguna {user_id}. Data: Aktif, Tugas: Tidak berjalan.")
    else: msg_text = "❌ Layanan Auto-Claim saat ini dalam status *TIDAK AKTIF*."
    logger.info(f"Status Auto-Claim untuk pengguna {user_id}: {msg_text}")
    await edit_or_send_message(context, chat_id, msg_text, autoclaim_menu_keyboard(), message_id=message_id)

# --- Callback Query Handler ---
async def button_tap_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    message_id = query.message.message_id
    callback_data = query.data
    logger.info(f"Tombol inline ditekan oleh pengguna {user_id} di chat {chat_id}. Callback data: '{callback_data}', Message ID: {message_id}")

    action_map = {
        'main_menu': lambda: send_main_menu(update, context, chat_id, message_id),
        'menu_profile': lambda: core_profile_action(user_id, chat_id, context, message_id),
        'menu_tokens': lambda: core_tokens_action(user_id, chat_id, context, message_id),
        'menu_claimstatus': lambda: core_claim_status_action(user_id, chat_id, context, message_id),
        'menu_claim': lambda: core_claim_action(user_id, chat_id, context, message_id),
        'menu_history': lambda: core_history_action(user_id, chat_id, context, message_id),
        'menu_settoken_info': lambda: edit_or_send_message(
            context, chat_id,
            ("🔑 *Panduan Pengaturan Token Autentikasi*\n\n"
             "Untuk mengatur atau memperbarui token autentikasi Anda, silakan ketik dan kirim perintah berikut langsung di chat ini:\n\n"
             "`/settoken TOKEN_ANDA_DISINI`\n\n"
             "*Contoh Penggunaan:*\n"
             "`/settoken eyJhbGciOiJIUzI1NiIs...dst`\n\n"
             "Anda dapat menemukan token ini menggunakan fitur Developer Tools pada browser Anda (biasanya dapat diakses dengan menekan F12). Buka tab 'Network' atau 'Jaringan', lakukan login ke layanan Interlink, lalu cari permintaan (request) ke API Interlink. Token Bearer biasanya terdapat pada bagian 'Headers' di bawah 'Authorization'."),
            back_to_main_menu_button(), message_id=message_id
        ),
        'menu_autoclaim_settings': lambda: edit_or_send_message(
            context, chat_id, "🤖 *Pengaturan Layanan Auto-Claim*\n\nPilih salah satu opsi di bawah ini:",
            autoclaim_menu_keyboard(), message_id=message_id
        ),
        'autoclaim_start': lambda: core_autoclaim_start(user_id, chat_id, context, message_id),
        'autoclaim_stop': lambda: core_autoclaim_stop(user_id, chat_id, context, message_id),
        'autoclaim_status': lambda: core_autoclaim_status(user_id, chat_id, context, message_id)
    }

    if callback_data in action_map:
        await action_map[callback_data]()
    else:
        logger.warning(f"Callback data tidak dikenal: '{callback_data}' dari pengguna {user_id}.")
        await context.bot.send_message(chat_id=chat_id, text="Maaf, aksi tidak dikenal atau sedang dalam pengembangan.")

# --- Command Handlers Typed ---
async def profile_command_typed(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id, chat_id = get_ids_from_update(update); await core_profile_action(user_id, chat_id, context) if user_id and chat_id else None
async def tokens_command_typed(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id, chat_id = get_ids_from_update(update); await core_tokens_action(user_id, chat_id, context) if user_id and chat_id else None
async def claim_status_command_typed(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id, chat_id = get_ids_from_update(update); await core_claim_status_action(user_id, chat_id, context) if user_id and chat_id else None
async def claim_command_typed(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id, chat_id = get_ids_from_update(update); await core_claim_action(user_id, chat_id, context) if user_id and chat_id else None
async def history_command_typed(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id, chat_id = get_ids_from_update(update); await core_history_action(user_id, chat_id, context) if user_id and chat_id else None
async def autoclaim_start_command_typed(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id, chat_id = get_ids_from_update(update); await core_autoclaim_start(user_id, chat_id, context) if user_id and chat_id else None
async def autoclaim_stop_command_typed(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id, chat_id = get_ids_from_update(update); await core_autoclaim_stop(user_id, chat_id, context) if user_id and chat_id else None
async def autoclaim_status_command_typed(update: Update, context: ContextTypes.DEFAULT_TYPE): user_id, chat_id = get_ids_from_update(update); await core_autoclaim_status(user_id, chat_id, context) if user_id and chat_id else None

# --- post_init dan main ---
class SimpleBotContextForTasks: 
    def __init__(self, bot_instance, app_instance): self.bot = bot_instance; self.application = app_instance

async def post_init(application: Application): 
    bot_commands = [ BotCommand("start", "🚀 Mulai bot & tampilkan menu utama"), BotCommand("help", "❓ Tampilkan menu bantuan"), BotCommand("settoken", "🔑 Atur token autentikasi Anda (Contoh: /settoken <TOKEN>)"),]
    await application.bot.set_my_commands(bot_commands)
    logger.info(f"Perintah bot telah berhasil diatur: {[cmd.command for cmd in bot_commands]}")
    logger.info("Memeriksa tugas auto-claim yang tertunda untuk di-restart...")
    active_restarts = 0
    for filename in os.listdir(USER_DATA_DIR):
        if filename.endswith(".json"):
            try:
                user_id_str = filename.split(".")[0]
                if not user_id_str.isdigit(): logger.warning(f"Format nama file user_data tidak valid: {filename}. Dilewati."); continue
                user_id = int(user_id_str)
                user_data = await load_user_data(user_id)
                if user_data.get("auto_claim_active"):
                    task_exists = user_id in AUTO_CLAIM_TASKS
                    is_task_done_or_none = True 
                    if task_exists:
                        task_object = AUTO_CLAIM_TASKS.get(user_id)
                        if task_object: is_task_done_or_none = task_object.done()
                    if not task_exists or is_task_done_or_none :
                        logger.info(f"Me-restart tugas Auto-Claim untuk pengguna {user_id} saat bot dimulai.")
                        context_for_task = SimpleBotContextForTasks(application.bot, application)
                        new_task = asyncio.create_task(auto_claim_task_function(user_id, context_for_task))
                        AUTO_CLAIM_TASKS[user_id] = new_task
                        active_restarts +=1
                    else: logger.info(f"Tugas Auto-Claim untuk pengguna {user_id} (dari file {filename}) sudah berjalan atau telah dicoba untuk dimulai sebelumnya.")
            except ValueError as ve: logger.warning(f"Format user ID tidak valid pada nama file di user_data: {filename}. Error: {ve}")
            except Exception as e: logger.error(f"Gagal me-restart Auto-Claim untuk pengguna dari file {filename}: {e}", exc_info=ENABLE_DETAILED_CONSOLE_LOGS)
    logger.info(f"Pemeriksaan restart Auto-Claim selesai. {active_restarts} tugas di-restart.")

def main() -> None: 
    logger.info("Memulai konfigurasi aplikasi bot...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
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
    logger.info("Aplikasi bot berhasil dikonfigurasi. Memulai polling update dari Telegram...")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == "__main__":
    main()
