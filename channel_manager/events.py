"""
Botのイベントハンドラ群と定期タスク: メンバー参加/退出時の処理、
起動処理、エラー通知、非表示チャンネルの自動削除タスクなど。
"""
import asyncio
import sys
from datetime import datetime, timezone, timedelta

import discord
from discord.ext import tasks

from bot_core import bot, ADMIN_ID, logger, notify_admin_error
from storage import (
    RULES_TEXT,
    HIDDEN_RETENTION_DAYS,
    load_data,
    save_data,
    load_hidden_data,
    save_hidden_data,
)
from utils import get_existing_channel, hide_channel_from_others, make_embed, make_privacy_embed
from views import RulesView, ChannelSettingsView


@bot.event
async def on_member_join(member):
    guild = member.guild

    welcome_category = discord.utils.get(guild.categories, name="ようこそ")
    if not welcome_category:
        category_overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        }
        welcome_category = await guild.create_category("ようこそ", overwrites=category_overwrites)

    # チャンネルごとに個別で権限を明示設定
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False, send_messages=False),
        member: discord.PermissionOverwrite(read_messages=True, send_messages=False),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    channel = await guild.create_text_channel(
        f"ようこそ-{member.name}",
        category=welcome_category,
        overwrites=overwrites
    )

    embed = make_embed(
        step=1,
        title="📋 利用規約",
        description=f"{member.mention} ようこそ！\nまず以下の利用規約とデータの取り扱いについてお読みください。\n\n{RULES_TEXT}"
    )
    await channel.send(content=member.mention, embeds=[embed, make_privacy_embed()], view=RulesView())

@tasks.loop(hours=24)
async def cleanup_expired_hidden_channels():
    """非表示化から HIDDEN_RETENTION_DAYS 日経過したチャンネルを完全削除する定期タスク"""
    hidden_data = load_hidden_data()
    if not hidden_data:
        return

    now = datetime.now(timezone.utc)
    expired_ids = []

    for channel_id, info in hidden_data.items():
        try:
            hidden_at = datetime.fromisoformat(info["hidden_at"])
        except Exception:
            expired_ids.append(channel_id)
            continue

        if now - hidden_at >= timedelta(days=HIDDEN_RETENTION_DAYS):
            channel = bot.get_channel(int(channel_id))
            if channel:
                try:
                    await channel.delete(reason=f"非表示化から{HIDDEN_RETENTION_DAYS}日経過による自動削除")
                except Exception:
                    pass
            # チャンネルが既に手動削除されている場合も含めて記録を掃除
            expired_ids.append(channel_id)
            # メインの紐付けデータにも残っていれば削除
            data = load_data()
            user_id = info.get("user_id")
            if user_id and data.get(user_id) == channel_id:
                del data[user_id]
                save_data(data)

    if expired_ids:
        for channel_id in expired_ids:
            del hidden_data[channel_id]
        save_hidden_data(hidden_data)


@cleanup_expired_hidden_channels.before_loop
async def before_cleanup_expired_hidden_channels():
    await bot.wait_until_ready()

@bot.event
async def on_member_remove(member):
    guild = member.guild

    # ようこそチャンネルが残っていたら削除
    channel_name = f"ようこそ-{member.name}"
    welcome_category = discord.utils.get(guild.categories, name="ようこそ")
    if welcome_category:
        for channel in welcome_category.channels:
            if channel.name == channel_name:
                await channel.delete(reason="メンバー退出に伴う自動削除")
                break

    # 個人チャンネルの扱いを /leave を経由したかどうかに関わらず統一する。
    # /leave で「削除して退出」を選んだ場合は既にチャンネルが無いので何もしない。
    # /leave で「非表示にして退出」を選んだ場合は既に hidden_channels.json に記録済みなので二重処理しない。
    # それ以外（/leave を使わない自主退出・キック・BANなど）は、ここで初めて
    # 非表示化＋保持期限のカウントを開始することで、退会経路による扱いの差をなくす。
    existing = await get_existing_channel(guild, member)
    if existing:
        hidden_data = load_hidden_data()
        if str(existing.id) not in hidden_data:
            try:
                await hide_channel_from_others(existing, guild, member.id)
            except Exception:
                pass

@bot.event
async def on_ready():
    await bot.tree.sync()
    if not cleanup_expired_hidden_channels.is_running():
        cleanup_expired_hidden_channels.start()

    # チャンネル設定パネルの永続View登録。所有者情報を持たないテンプレートなので
    # これを1回登録するだけで、既存の全チャンネルに設置済みのパネルもBot再起動後に機能し続ける。
    bot.add_view(ChannelSettingsView())

    print(f"起動しました：{bot.user}")

    # 起動時に管理者へ通知。クラッシュ→再起動を繰り返している場合はDMが連続で届くので気づきやすい。
    try:
        admin = await bot.fetch_user(ADMIN_ID)
        await admin.send(f"🟢 Botが起動しました（{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC）")
    except Exception:
        pass

@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
    """スラッシュコマンド実行中に起きた例外を共通で捕捉する。
    これが無いと、コマンドがエラーで失敗しても本人にも管理者にも何も伝わらず「反応しない」ように見える。"""
    command_name = interaction.command.name if interaction.command else "不明"
    await notify_admin_error(f"コマンドエラー：/{command_name}（実行者：{interaction.user}）", error)

    try:
        message = "⚠️ 内部エラーが発生しました。管理者に自動で通知されています。"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        pass

@bot.event
async def on_error(event_name, *args, **kwargs):
    """on_member_join などコマンド以外のイベントハンドラで起きた例外を共通で捕捉する"""
    exc_type, exc_value, exc_tb = sys.exc_info()
    if exc_value:
        await notify_admin_error(f"イベントエラー：{event_name}", exc_value)
    else:
        logger.error(f"イベントエラー：{event_name}（詳細不明）")

@cleanup_expired_hidden_channels.error
async def cleanup_expired_hidden_channels_error(error):
    """定期タスクは例外が起きると何も知らせずに完全停止するため、通知した上で再起動を試みる"""
    await notify_admin_error(
        "定期タスク（非表示チャンネル自動削除）でエラーが発生し停止しました。自動で再起動を試みます。",
        error
    )
    await asyncio.sleep(60)
    if not cleanup_expired_hidden_channels.is_running():
        cleanup_expired_hidden_channels.start()
