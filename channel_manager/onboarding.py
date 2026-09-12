"""入室完了の案内と、本人のようこそチャンネルの後片付け。"""
import asyncio

import discord

from bot_core import logger, notify_admin_error
from utils import make_embed


async def finish_onboarding(interaction, personal_channel, *, restored=False):
    """登録・権限設定の成功後に呼ぶ。通知失敗でも後片付けを実行する。"""
    welcome = interaction.channel
    safe_to_delete = (
        isinstance(welcome, discord.TextChannel)
        and welcome.id != personal_channel.id
        and welcome.category is not None
        and welcome.category.name == "ようこそ"
        and welcome.overwrites_for(interaction.user).view_channel is True
    )
    note = "\n\nこの案内用チャンネルは3秒後に自動削除されます。" if safe_to_delete else ""
    try:
        if restored:
            try:
                await personal_channel.send(f"{interaction.user.mention} おかえりなさい！あなたのチャンネルはここです👋")
            except discord.HTTPException:
                logger.exception("復帰案内の投稿に失敗しました。")
        await interaction.followup.send(
            embed=make_embed(
                step=3, title="✅ 入室手続きが完了しました！",
                description=f"{personal_channel.mention} から始めましょう🎉{note}",
                color=0x57F287,
            ), ephemeral=True,
        )
    except discord.HTTPException:
        logger.exception("入室完了通知に失敗しました。後片付けは続行します。")
    finally:
        if safe_to_delete:
            await delete_welcome_channel(interaction, welcome)


async def delete_welcome_channel(interaction, welcome):
    await asyncio.sleep(3)
    for attempt in range(3):
        try:
            await welcome.delete(reason="入室手続き完了による案内用チャンネル削除")
            return
        except discord.NotFound:
            return
        except discord.HTTPException as error:
            if not isinstance(error, discord.Forbidden) and attempt < 2:
                await asyncio.sleep(3)
                continue
            await notify_admin_error(f"入室完了後の案内用チャンネル削除に失敗（ID: {welcome.id}）", error)
            try:
                await interaction.followup.send(
                    "⚠️ 入室手続きは完了しましたが、案内用チャンネルを削除できませんでした。管理者に通知しました。",
                    ephemeral=True,
                )
            except discord.HTTPException:
                logger.exception("削除失敗の通知を送信できませんでした。")
            return
