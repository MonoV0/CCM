"""期限切れ・重複応答を安全に打ち切り、初回応答の遅延を記録する。"""
import os
import time
from datetime import datetime, timezone

import discord

from bot_core import bot, logger


def unusable_interaction(error):
    return isinstance(error, discord.HTTPException) and error.code in (10062, 40060)


def log_unusable_interaction(interaction, error, *, response_seconds=None):
    created = getattr(interaction, 'created_at', None)
    age = (datetime.now(timezone.utc) - created).total_seconds() if created is not None else None
    command = getattr(interaction, 'command', None)
    logger.warning(
        'Discord interaction rejected: command=%s code=%s interaction_id=%s guild_id=%s '
        'age_s=%s response_s=%s gateway_latency_s=%s pid=%s; '
        'この操作は再送しません。コマンドを再実行してください。'
        '繰り返す場合は通信遅延・処理詰まり・Botの同時起動を確認してください。',
        getattr(command, 'name', 'unknown'), error.code, getattr(interaction, 'id', None),
        getattr(interaction, 'guild_id', None), age, response_seconds, bot.latency, os.getpid(),
    )


async def defer_export(interaction):
    """初回応答は1回だけ。失敗した操作では収集・既存結果の破棄を始めない。"""
    started = time.monotonic()
    try:
        await interaction.response.defer(ephemeral=True)
    except discord.HTTPException as error:
        if not unusable_interaction(error):
            raise
        log_unusable_interaction(interaction, error, response_seconds=time.monotonic() - started)
        return False
    return True
