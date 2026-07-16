"""
汎用ヘルパー関数: ステージのチャットログ生成、チケットコード生成、
配信URLからのプラットフォーム判別。
"""
import io
import secrets
from typing import Optional

import discord

TICKET_LENGTH = 8  # トークンの文字数


async def generate_log_file(channel: discord.StageChannel) -> Optional[discord.File]:
    messages = []
    async for msg in channel.history(limit=200, oldest_first=True):
        if msg.author.bot:
            continue
        messages.append(msg)

    if not messages:
        return None

    lines = [f"━━━━━━━━━━━━━━━━━━━━━━━━", f" ステージチャット履歴: {channel.name}", f"━━━━━━━━━━━━━━━━━━━━━━━━", ""]
    for msg in messages:
        time_str = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        lines.append(f"[{time_str}] {msg.author.display_name}: {msg.content}")

    log_content = "\n".join(lines)
    file_bytes = io.BytesIO(log_content.encode('utf-8'))
    return discord.File(fp=file_bytes, filename=f"stage_log_{channel.name}.txt")


def generate_ticket() -> str:
    """英数字8文字のランダムトークンを生成する"""
    return secrets.token_urlsafe(TICKET_LENGTH)[:TICKET_LENGTH].upper()


def detect_platform(url: str) -> tuple[str, str]:
    """URLからプラットフォームを判別してアイコンと名前を返す"""
    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "🔴", "YouTube"
    elif "twitch.tv" in url_lower:
        return "🟣", "Twitch"
    elif "tiktok.com" in url_lower:
        return "⬛", "TikTok"
    elif "nicovideo.jp" in url_lower or "nico.ms" in url_lower:
        return "⬜", "ニコニコ動画"
    elif "openrec.tv" in url_lower:
        return "🟦", "OPENREC"
    elif "mirrativ.com" in url_lower:
        return "🟧", "Mirrativ"
    elif "showroom-live.com" in url_lower:
        return "🟩", "SHOWROOM"
    else:
        return "📡", "配信"
