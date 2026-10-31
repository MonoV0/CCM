"""
Botのイベントハンドラ群: 起動処理、エラー通知、ボイスチャンネル参加時の
一時ステージ作成・削除など。
"""
import asyncio
import sys
from typing import Optional

import discord
from discord import app_commands

from operations import maybe_delete_temp_channel, participant_overwrites, repair_participant_permissions

import bot_core
from bot_core import bot, log, notify_owner_error
from storage import guild_config, temp_channels, save_temp_channels, DEFAULT_NAME_TEMPLATE, DEFAULT_TOPIC_TEMPLATE
from views import (
    StageConsoleView,
    SummonPanelView,
    SummonAndReactionView,
    JoinPromptView,
    SimplePanelView,
    ReactionTriggerView,
)

STAGE_JOIN_TIMEOUT = 10


async def invite_creator_to_speak(member: discord.Member, channel: discord.StageChannel) -> bool:
    """移動先のキャッシュ更新を待ち、別チャンネルに発言操作を送らない。"""
    def has_joined():
        voice = member.voice
        return voice is not None and voice.channel is not None and voice.channel.id == channel.id

    def joined_stage(changed_member, before, after):
        return (
            changed_member.id == member.id
            and changed_member.guild.id == member.guild.id
            and after.channel is not None and after.channel.id == channel.id
        )

    if not has_joined():
        try:
            # wait_forは呼び出し時にリスナーを登録する。上のキャッシュ確認から
            # 登録までawaitを挟まないため、その間の入室イベントを取りこぼさない。
            await bot.wait_for("voice_state_update", check=joined_stage, timeout=STAGE_JOIN_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("作成者のステージ入室確認がタイムアウトしました: guild_id=%s channel_id=%s user_id=%s",
                        member.guild.id, channel.id, member.id)
            return False

    # 入室イベント後、実行が再開するまでに退出・移動している場合も操作しない。
    if not has_joined():
        log.warning("作成者が対象ステージから移動したため発言招待を中止しました: guild_id=%s channel_id=%s user_id=%s",
                    member.guild.id, channel.id, member.id)
        return False
    try:
        await member.edit(suppress=False)
    except discord.HTTPException:
        log.exception("作成者のスピーカー招待に失敗しました: guild_id=%s channel_id=%s user_id=%s",
                      member.guild.id, channel.id, member.id)
        return False
    return True


async def setup_hook():
    try:
        app_info = await bot.application_info()
        bot_core.BOT_OWNER_ID = app_info.owner.id
    except Exception:
        log.exception("Bot所有者情報の取得に失敗しました。エラー時のDM通知が無効になります。")

    bot.add_view(StageConsoleView())
    bot.add_view(SummonPanelView())
    bot.add_view(SummonAndReactionView())
    bot.add_view(JoinPromptView())
    bot.add_view(SimplePanelView())
    bot.add_view(ReactionTriggerView())
    try:
        synced = await bot.tree.sync()
        log.info("スラッシュコマンドを %d 件同期しました。", len(synced))
    except Exception:
        log.exception("スラッシュコマンドの同期に失敗しました。")

bot.setup_hook = setup_hook


@bot.event
async def on_ready():
    log.info("ログイン完了: %s (ID: %s)", bot.user, bot.user.id)

    for channel_id_str in list(temp_channels.keys()):
        channel = bot.get_channel(int(channel_id_str))
        if channel is None:
            temp_channels.pop(channel_id_str, None)
            continue
        if isinstance(channel, discord.StageChannel) and len(channel.members) == 0:
            await maybe_delete_temp_channel(channel)
        elif isinstance(channel, discord.StageChannel):
            await repair_participant_permissions(channel)
    save_temp_channels()

    # 起動時に所有者へ通知。クラッシュ→再起動を繰り返している場合はDMが連続で届くので気づきやすい。
    if bot_core.BOT_OWNER_ID is not None:
        try:
            owner = await bot.fetch_user(bot_core.BOT_OWNER_ID)
            await owner.send(f"🟢 ステージBotが起動しました（{bot.user}）")
        except Exception:
            pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """スラッシュコマンド実行中に起きた例外を共通で捕捉する。
    これが無いと、コマンドがエラーで失敗しても本人には「このインタラクションは失敗しました」としか
    表示されず、原因も開発者への通知も一切ないまま「反応しない」ように見えてしまう。"""

    # 権限不足はエラー通知ではなく、わかりやすい案内だけ返す
    if isinstance(error, app_commands.MissingPermissions):
        try:
            await interaction.response.send_message(
                "⚠️ このコマンドを実行する権限がありません（サーバー管理権限が必要です）。",
                ephemeral=True
            )
        except Exception:
            pass
        return

    command_name = interaction.command.name if interaction.command else "不明"
    guild_name = interaction.guild.name if interaction.guild else "不明"
    await notify_owner_error(
        f"コマンドエラー：/{command_name}（実行者：{interaction.user} / サーバー：{guild_name}）",
        error
    )

    try:
        message = "⚠️ 内部エラーが発生しました。開発者に自動で通知されています。"
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        pass


@bot.event
async def on_error(event_name, *args, **kwargs):
    """on_voice_state_update などコマンド以外のイベントハンドラで起きた例外を共通で捕捉する"""
    exc_type, exc_value, exc_tb = sys.exc_info()
    if exc_value:
        await notify_owner_error(f"イベントエラー：{event_name}", exc_value)
    else:
        log.error("イベントエラー：%s（詳細不明）", event_name)



async def create_temp_stage(member: discord.Member, config: dict) -> Optional[discord.StageChannel]:
    guild = member.guild
    category = guild.get_channel(config.get("category_id")) if config.get("category_id") else None
    if not isinstance(category, discord.CategoryChannel):
        category = None

    name = DEFAULT_NAME_TEMPLATE.format(username=member.display_name)[:100]

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=True, stream=True),
        guild.me: discord.PermissionOverwrite(
            view_channel=True, connect=True, manage_channels=True, move_members=True, mute_members=True, send_messages=True, read_message_history=True, stream=True
        ),
        member: discord.PermissionOverwrite(
            view_channel=True, connect=True, manage_channels=True, move_members=True, mute_members=True, request_to_speak=True, stream=True,
            send_messages=True, read_message_history=True
        ),
    }
    overwrites.update(participant_overwrites(guild))

    try:
        stage_channel = await guild.create_stage_channel(
            name=name, category=category, overwrites=overwrites, reason=f"{member} のための一時ステージ"
        )
    except discord.Forbidden:
        return None
    except discord.HTTPException:
        return None

    temp_channels[str(stage_channel.id)] = {"guild_id": guild.id, "password": None, "invite_msg_ids": [], "sub_admins": [], "blocked_users": [], "tickets": {}, "reaction_thread_id": None}
    save_temp_channels()

    warnings = []
    moved = False
    try:
        await member.move_to(stage_channel)
        moved = True
    except discord.HTTPException:
        log.exception("作成者のステージへの移動に失敗しました: guild_id=%s channel_id=%s user_id=%s",
                      guild.id, stage_channel.id, member.id)
        warnings.append("⚠️ 自動移動に失敗しました。上のチャンネルから手動で入室してください。")

    started = False
    try:
        await stage_channel.create_instance(topic=DEFAULT_TOPIC_TEMPLATE.format(username=member.display_name)[:120])
        started = True
    except discord.HTTPException:
        log.exception("ステージの開始に失敗しました: guild_id=%s channel_id=%s user_id=%s",
                      guild.id, stage_channel.id, member.id)
        warnings.append("⚠️ ステージの開始に失敗しました。管理者にBotの権限とログを確認してもらってください。")

    invited = False
    if moved and started:
        invited = await invite_creator_to_speak(member, stage_channel)
        if not invited:
            warnings.append("⚠️ 自動のスピーカー設定を完了できませんでした。ステージ内の操作でスピーカーになるか、管理者に確認してください。")

    try:
        speaker_note = "スピーカー招待を送信しました。承認の案内が表示された場合は承認してください。\n" if invited else ""
        await stage_channel.send(
            f"{member.mention} さん、一時ステージのチャンネルを作成しました！ {stage_channel.mention}\n"
            + speaker_note
            + "設定の変更やステージの終了は 🎛️ ボタンから、絵文字リアクションは 🎉 ボタンからどうぞ。"
            + ("\n" + "\n".join(warnings) if warnings else ""),
            view=SummonAndReactionView()
        )
    except discord.HTTPException:
        log.exception("ステージ作成結果の案内に失敗しました: guild_id=%s channel_id=%s user_id=%s",
                      guild.id, stage_channel.id, member.id)

    return stage_channel


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return

    # ── チャンネル移動処理 ────────────────────────────────────────
    if before.channel == after.channel:
        return

    if before.channel is not None:
        await maybe_delete_temp_channel(before.channel)

    if after.channel is not None:
        config = guild_config.get(str(member.guild.id))
        if config and after.channel.id == config.get("trigger_channel_id"):
            await create_temp_stage(member, config)
        # ── 入室ウェルカム通知＋リアクションパネル ────────────────
        elif (
            isinstance(after.channel, discord.StageChannel)
            and str(after.channel.id) in temp_channels
        ):
            try:
                await after.channel.send(
                    f"👋 **{member.display_name}** さんが参加しました！\n"
                    "絵文字リアクションは 🎉 ボタンからどうぞ。",
                    view=ReactionTriggerView()
                )
            except discord.HTTPException:
                pass

