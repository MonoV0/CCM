"""
Botのイベントハンドラ群と定期タスク: メンバー参加/退出時の処理、
起動処理、エラー通知、非表示チャンネルの自動削除タスクなど。
"""
import sys
import asyncio
from datetime import datetime, timezone, timedelta

import discord
import aiohttp
from discord.ext import tasks

from bot_core import bot, ADMIN_ID, logger, notify_admin_error
from timeline import TimelineBoundaryError
from storage import (
    RULES_TEXT,
    HIDDEN_RETENTION_DAYS,
    load_data,
    save_data,
    load_hidden_data,
    save_hidden_data,
    INDEX_CHANNEL_NAME,
)
from utils import get_existing_channel, hide_channel_from_others, make_embed, make_privacy_embed, refresh_existing_channel_indexes
from views import RulesView, ChannelSettingsView, ApprovalView, register_onboarding_views
from invitations import restore_approval_views

_command_sync_task = None
_commands_synced = False
_index_refresh_task = None


async def refresh_indexes_on_ready():
    for guild in bot.guilds:
        await refresh_existing_channel_indexes(guild)


@bot.event
async def on_guild_channel_update(before, after):
    if before.overwrites == after.overwrites and before.category_id == after.category_id:
        return
    categories = [after] if isinstance(after, discord.CategoryChannel) else [before.category, after.category]
    await refresh_existing_channel_indexes(after.guild, categories)


@bot.event
async def on_guild_channel_create(channel):
    if not isinstance(channel, discord.CategoryChannel) and channel.name != INDEX_CHANNEL_NAME:
        await refresh_existing_channel_indexes(channel.guild, [channel.category])


@bot.event
async def on_guild_channel_delete(channel):
    if not isinstance(channel, discord.CategoryChannel) and channel.name != INDEX_CHANNEL_NAME:
        await refresh_existing_channel_indexes(channel.guild, [channel.category])


@bot.event
async def on_guild_role_update(before, after):
    if before.permissions != after.permissions:
        await refresh_existing_channel_indexes(after.guild)


@bot.event
async def on_guild_role_delete(role):
    await refresh_existing_channel_indexes(role.guild)


@bot.event
async def on_member_update(before, after):
    if before.roles != after.roles:
        await refresh_existing_channel_indexes(after.guild)


async def sync_commands_with_retry():
    """一時的なAPI障害では起動処理を止めず、間隔を広げて同期を再試行する。"""
    global _commands_synced
    delay = 30
    while not bot.is_closed():
        try:
            commands = await bot.tree.sync()
        except discord.HTTPException as error:
            if error.status < 500 and error.status != 429:
                logger.exception('コマンド同期に失敗しました（設定・権限を確認してください）')
                return
            logger.warning('コマンド同期のHTTP障害。%s秒後に再試行します（status=%s）', delay, error.status)
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as error:
            logger.warning('コマンド同期の通信障害。%s秒後に再試行します（%s）', delay, type(error).__name__)
        except Exception:
            logger.exception('コマンド同期に失敗しました（実装・コマンド定義を確認してください）')
            return
        else:
            _commands_synced = True
            logger.info('スラッシュコマンドを%s件同期しました。', len(commands))
            return
        await asyncio.sleep(delay)
        delay = min(delay * 2, 300)


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
    """非表示化から HIDDEN_RETENTION_DAYS 日経過したチャンネルを完全削除する定期タスク。
    discord.pyのループは例外で停止し、エラーハンドラ内からは再開できないため、ここで捕捉して継続する。"""
    try:
        await _cleanup_expired_hidden_channels()
    except Exception as error:
        await notify_admin_error(
            "定期タスク（非表示チャンネル自動削除）でエラーが発生しました。タスクは継続し、次回の実行で再試行します。",
            error
        )


async def _cleanup_expired_hidden_channels():
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
                except discord.NotFound:
                    pass  # Discord側で既に削除済み
                except Exception:
                    logger.exception("非表示チャンネル %s の削除に失敗。次回再試行します。", channel_id)
                    continue
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
    global _command_sync_task, _index_refresh_task
    try:
        restore_approval_views(ApprovalView)
    except Exception as error:
        await notify_admin_error("招待審査の復元失敗（招待は停止、既存機能は継続）", error)
    if not cleanup_expired_hidden_channels.is_running():
        cleanup_expired_hidden_channels.start()

    # チャンネル設定パネルの永続View登録。所有者情報を持たないテンプレートなので
    # これを1回登録するだけで、既存の全チャンネルに設置済みのパネルもBot再起動後に機能し続ける。
    bot.add_view(ChannelSettingsView())
    # オンボーディングのボタンも、手続き途中で再起動しても押せるようにする。
    register_onboarding_views()

    # on_readyは再接続でも呼ばれる。実行中・成功済みの同期を重複させない。
    if not _commands_synced and (_command_sync_task is None or _command_sync_task.done()):
        _command_sync_task = asyncio.create_task(sync_commands_with_retry())
    if bot.guilds and (_index_refresh_task is None or _index_refresh_task.done()):
        _index_refresh_task = asyncio.create_task(refresh_indexes_on_ready())

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
    original = getattr(error, 'original', error)
    boundary_error = isinstance(original, TimelineBoundaryError)
    # クールダウン・権限不足・DMでの実行などは利用者側の状況なので、管理者へ通知しない。
    if isinstance(error, discord.app_commands.CommandOnCooldown):
        message = f"⏳ 連続実行はできません。{error.retry_after:.0f}秒後にもう一度お試しください。"
    elif isinstance(error, discord.app_commands.NoPrivateMessage):
        message = "⚠️ このコマンドはサーバー内で実行してください。"
    elif isinstance(error, discord.app_commands.CheckFailure):
        message = "⚠️ このコマンドを実行する権限がありません。"
    elif boundary_error:
        message = str(original)
    else:
        command_name = interaction.command.name if interaction.command else "不明"
        await notify_admin_error(f"コマンドエラー：/{command_name}（実行者：{interaction.user}）", error)
        message = "⚠️ 内部エラーが発生しました。管理者に自動で通知されています。"

    try:
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
