"""
Bot本体の初期化: トークン読み込み、Intents、ロギング設定、
管理者へのエラー通知ヘルパー。
"""
import logging
import os
import time
import traceback
from logging.handlers import RotatingFileHandler

import discord
from discord.ext import commands
from dotenv import load_dotenv

from storage import ERROR_LOG_FILE

START_TIME = time.time()

# ---- ロギング設定（ファイル＋コンソールの両方に出力） ----
logger = logging.getLogger("channel_manager")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    _file_handler = RotatingFileHandler(ERROR_LOG_FILE, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    _file_handler.setFormatter(_formatter)
    logger.addHandler(_file_handler)
    _console_handler = logging.StreamHandler()
    _console_handler.setFormatter(_formatter)
    logger.addHandler(_console_handler)

load_dotenv()
TOKEN = os.getenv("CHANNEL_MANAGER_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

async def notify_admin_error(title: str, error: BaseException):
    """例外内容をログに記録し、管理者へDMで通知する共通ヘルパー"""
    tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    logger.error(f"{title}\n{tb}")
    try:
        admin = await bot.fetch_user(ADMIN_ID)
        snippet = tb[-1500:] if len(tb) > 1500 else tb
        await admin.send(f"🚨 **{title}**\n```\n{snippet}\n```")
    except Exception:
        # 通知自体が失敗してもログには残っているので、致命的にはしない
        pass
