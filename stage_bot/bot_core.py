"""
Bot本体の初期化: トークン読み込み、Intents、ロギング設定、
Bot所有者へのエラー通知ヘルパー。
"""
import logging
import os
import time
import traceback
from logging.handlers import RotatingFileHandler
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv

from storage import DATA_DIR

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN が .env に設定されていません。")

ERROR_LOG_PATH = DATA_DIR / "bot_errors.log"
START_TIME = time.time()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("stage-bot")

# コンソールに加えて、エラーログをファイルにも残す（ホスティング先のコンソールログが
# 消えても、後から /stage-recent-errors や直接ファイルを見て原因調査できるようにするため）
_file_handler = RotatingFileHandler(ERROR_LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
_file_handler.setLevel(logging.WARNING)
log.addHandler(_file_handler)

intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True

bot = commands.Bot(command_prefix="stagebot!", intents=intents)

# 複数サーバーで動く前提のBotのため、固定の管理者IDではなく
# Discord Developer Portal上のアプリケーション所有者を通知先として使う
BOT_OWNER_ID: Optional[int] = None


async def notify_owner_error(title: str, error: BaseException) -> None:
    """例外内容をログに記録し、Bot所有者へDMで通知する共通ヘルパー"""
    tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))
    log.error("%s\n%s", title, tb)
    if BOT_OWNER_ID is None:
        return
    try:
        owner = await bot.fetch_user(BOT_OWNER_ID)
        snippet = tb[-1500:] if len(tb) > 1500 else tb
        await owner.send(f"🚨 **{title}**\n```\n{snippet}\n```")
    except Exception:
        # 通知自体が失敗してもログには残っているので致命的にはしない
        pass
