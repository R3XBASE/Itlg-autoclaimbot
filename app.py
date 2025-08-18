import logging
import asyncio
import json
import os
import time
import signal
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
from telegram import Update
from telegram.ext import Application

# --- Konfigurasi Global ---
TELEGRAM_BOT_TOKEN = "7843023960:AAF27XE6jWkIioWj7YpT1N_UQPqrhokbbp8"
ENABLE_DETAILED_CONSOLE_LOGS = True 
WIB = pytz.timezone('Asia/Jakarta')
PORT = int(os.environ.get('PORT', 5000))  # Untuk Render.com

# ... (kode lainnya tetap sama sampai bagian main) ...

# --- Fungsi untuk Shutdown Bersih ---
async def cleanup_after_shutdown(application: Application) -> None:
    """Membersihkan resource setelah shutdown"""
    logger.info("Memulai proses shutdown bersih...")
    
    # Hentikan semua tugas auto-claim
    global AUTO_CLAIM_TASKS
    if AUTO_CLAIM_TASKS:
        logger.info(f"Membatalkan {len(AUTO_CLAIM_TASKS)} tugas auto-claim...")
        tasks = list(AUTO_CLAIM_TASKS.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        AUTO_CLAIM_TASKS.clear()
    
    logger.info("Cleanup selesai. Aplikasi siap dihentikan.")

# --- Webhook Server untuk Render.com ---
async def run_webhook_server(application: Application):
    """Menjalankan server webhook untuk Render.com"""
    webhook_url = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}/{TELEGRAM_BOT_TOKEN}"
    
    await application.bot.set_webhook(
        url=webhook_url,
        drop_pending_updates=True
    )
    
    server = await asyncio.create_server(
        application.updater._init_webhook_server,  # Gunakan internal server
        "0.0.0.0",
        PORT
    )
    
    logger.info(f"Server webhook berjalan di port {PORT}")
    logger.info(f"Webhook URL: {webhook_url}")
    
    async with server:
        await server.serve_forever()

# --- Health Check Endpoint ---
async def health_check():
    """Endpoint sederhana untuk health check Render.com"""
    return web.Response(text="OK", status=200)

# --- Main Function yang Diubah ---
def main() -> None:
    if TELEGRAM_BOT_TOKEN == "" or not TELEGRAM_BOT_TOKEN:
        logger.critical("Variabel TELEGRAM_BOT_TOKEN belum diatur! Bot tidak dapat dijalankan.")
        return
    
    logger.info("Memulai konfigurasi aplikasi bot...")
    
    # Setup application dengan cleanup handler
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_stop(cleanup_after_shutdown)  # Handler untuk cleanup
        .build()
    )
    
    # Daftarkan handler
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("settoken", set_token_command))
    application.add_handler(CallbackQueryHandler(button_tap_handler))
    # ... (daftarkan handler lainnya seperti sebelumnya) ...
    
    # Tangani sinyal untuk shutdown bersih
    loop = asyncio.get_event_loop()
    for signame in ("SIGINT", "SIGTERM", "SIGABRT"):
        loop.add_signal_handler(
            getattr(signal, signame),
            lambda: asyncio.create_task(application.stop())
        )
    
    logger.info("Aplikasi bot berhasil dikonfigurasi.")
    
    # Jalankan server webhook untuk Render.com
    if os.environ.get('RENDER'):
        logger.info("Mode deployment: Render.com (Webhook)")
        application.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            webhook_url=f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME')}/{TELEGRAM_BOT_TOKEN}",
            secret_token='WEBHOOK_SECRET_TOKEN'  # Ganti dengan token rahasia Anda
        )
    else:
        logger.info("Mode deployment: Local (Polling)")
        application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
