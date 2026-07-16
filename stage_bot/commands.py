"""
スラッシュコマンド定義まとめ。
"""
import io
import time
from typing import Optional

import discord
from discord import app_commands

from bot_core import bot, START_TIME, ERROR_LOG_PATH
from storage import (
    guild_config,
    temp_channels,
    save_guild_config,
    load_json,
    GUILD_CONFIG_PATH,
    TEMP_CHANNELS_PATH,
    PRESETS_PATH,
)
from views import JoinStageView, TicketJoinModal, StageConsoleView, ReactionPickerView


@bot.tree.command(name="stage-setup", description="一時ステージの「作成用」ボイスチャンネルを設定します。")
@app_commands.checks.has_permissions(manage_guild=True)
async def stage_setup(interaction: discord.Interaction, trigger: discord.VoiceChannel, category: Optional[discord.CategoryChannel] = None):
    guild_id = str(interaction.guild_id)
    guild_config[guild_id] = {
        "trigger_channel_id": trigger.id,
        "category_id": category.id if category else (trigger.category_id or None),
    }
    save_guild_config()
    await interaction.response.send_message("✅ 作成用ボイスチャンネルを設定しました。", ephemeral=True)


@bot.tree.command(name="stage-join", description="パスワードがかかった非公開ステージに参加します（招待パネル用予備コマンド）。")
async def stage_join(interaction: discord.Interaction):
    guild = interaction.guild
    stage_options = []
    
    for ch_id_str, data in temp_channels.items():
        if data.get("guild_id") == guild.id and data.get("password"):
            ch = guild.get_channel(int(ch_id_str))
            if isinstance(ch, discord.StageChannel):
                stage_options.append(discord.SelectOption(
                    label=ch.name,
                    value=str(ch.id),
                    description="パスワードが必要です",
                    emoji="🔐"
                ))
                
    if not stage_options:
        await interaction.response.send_message("ℹ️ 現在、パスワードが設定されているステージはありません。", ephemeral=True)
        return
        
    view = JoinStageView(stage_options[:25])
    await interaction.response.send_message("🔐 参加したいステージを選んでください。", view=view, ephemeral=True)


@bot.tree.command(name="stage-ticket", description="チケットコードを入力してプライベートステージに入室します。")
async def stage_ticket(interaction: discord.Interaction):
    await interaction.response.send_modal(TicketJoinModal())


@bot.tree.command(name="stage-panel", description="自分がいる一時ステージのコントロールパネルを呼び出します。")
async def stage_panel(interaction: discord.Interaction):
    member = interaction.guild.get_member(interaction.user.id)
    if not member or not member.voice or not isinstance(member.voice.channel, discord.StageChannel):
        await interaction.response.send_message(
            "⚠️ 現在ステージチャンネルに参加していません。\nまずステージに入室してからコマンドを使用してください。",
            ephemeral=True
        )
        return

    channel = member.voice.channel
    channel_id_str = str(channel.id)

    if channel_id_str not in temp_channels:
        await interaction.response.send_message(
            "⚠️ このステージはこのボットで作成されたものではないため、操作できません。",
            ephemeral=True
        )
        return

    is_owner = channel.permissions_for(interaction.user).manage_channels
    is_sub_admin = interaction.user.id in temp_channels.get(channel_id_str, {}).get("sub_admins", [])

    if not (is_owner or is_sub_admin):
        await interaction.response.send_message(
            "⚠️ コントロールパネルを開けるのはステージ作成者またはサブ管理者のみです。",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🎛️ ステージ・コントロールパネル",
        description=(
            f"**{channel.name}** の設定を変更できます。\n"
            "*(※このパネルはあなたにしか見えていません)*"
        ),
        color=discord.Color.blurple()
    )
    await interaction.response.send_message(embed=embed, view=StageConsoleView(), ephemeral=True)


@bot.tree.command(name="stage-reaction", description="リアクションピッカーを開きます。自分がいるステージチャットに絵文字を送れます。")
async def stage_reaction(interaction: discord.Interaction):
    member = interaction.guild.get_member(interaction.user.id)
    if not member or not member.voice or not isinstance(member.voice.channel, discord.StageChannel):
        await interaction.response.send_message(
            "⚠️ 現在ステージチャンネルに参加していません。\nまずステージに入室してからコマンドを使用してください。",
            ephemeral=True
        )
        return

    channel = member.voice.channel
    if str(channel.id) not in temp_channels:
        await interaction.response.send_message(
            "⚠️ このステージはこのボットで作成されたものではないため、操作できません。",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        "送りたい絵文字を選んでください👇\n*(この画面はあなたにしか見えていません)*",
        view=ReactionPickerView(channel),
        ephemeral=True
    )


@bot.tree.command(name="stage-help", description="このボットの使い方・機能一覧を表示します。")
async def stage_help(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🎤 一時ステージBot — 使い方ガイド",
        description=(
            "指定されたボイスチャンネルに参加すると、あなた専用の一時ステージが自動で作成されます。\n"
            "ステージに誰もいなくなると自動で削除されます。"
        ),
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="🚀 ステージを始めるには",
        value=(
            "サーバーに設置された **「ステージを作成」** ボイスチャンネルに参加するだけ！\n"
            "自動でステージが作られ、あなたがスピーカーになります。\n"
            "チャット欄の「コントロールパネルを開く」ボタンで各種設定を行えます。"
        ),
        inline=False
    )

    embed.add_field(
        name="🎛️ コントロールパネルでできること",
        value=(
            "📝 **名前を変更** — ステージ名を好きな名前に変更\n"
            "📢 **トピックを変更** — 今話している内容を設定\n"
            "👥 **人数制限** — 参加できる最大人数を設定（0で無制限）\n"
            "🔒 **公開/非公開** — ステージの公開状態を切り替え\n"
            "🔐 **パスワード** — 合言葉を設定して招待パネルを発行\n"
            "🗑️ **ステージ終了** — ステージを終了・削除（ログ保存も可）"
        ),
        inline=False
    )

    embed.add_field(
        name="👥 メンバー管理",
        value=(
            "👤 **メンバーを招待** — パネル下部のメニューから直接招待\n"
            "🚫 **ブロック＆キック** — メンバーを退出させ再入室を禁止\n"
            "👑 **サブ管理者指名** — 共同ホストとしてパネル操作権限を付与"
        ),
        inline=False
    )

    embed.add_field(
        name="🎟️ 招待方法いろいろ",
        value=(
            "🔐 **パスワード招待** — 合言葉を知っている人だけ入室可能\n"
            "　→ `/stage-join` または招待パネルのボタンでパスワード入力\n"
            "🎟️ **チケット招待** — 1回限りの使い捨てコードを発行\n"
            "　→ 相手は `/stage-ticket` でコードを入力して入室"
        ),
        inline=False
    )

    embed.add_field(
        name="📌 便利な機能",
        value=(
            "📌 **マイ・プリセット** — ステージ名・トピック・人数制限をセット保存（最大5件）\n"
            "📲 **簡易パネル** — ボタン4つに絞ったモバイル向けシンプルモード\n"
            "📺 **配信告知** — YouTube/TwitchなどのURLをEmbedカードで告知\n"
            "🔔 **挙手通知** — 参加者がスピーカーをリクエストするとチャットに通知"
        ),
        inline=False
    )

    embed.add_field(
        name="⌨️ スラッシュコマンド一覧",
        value=(
            "`/stage-panel` — どこからでもコントロールパネルを呼び出す\n"
            "`/stage-reaction` — どこからでもリアクションピッカーを呼び出す\n"
            "`/stage-join` — パスワード付きステージへ参加\n"
            "`/stage-ticket` — チケットコードで入室\n"
            "`/stage-help` — このヘルプを表示\n"
            "`/stage-botstatus` — Botの稼働状況を確認（サーバー管理権限が必要）\n"
            "`/stage-recent-errors` — 直近のエラーログを確認（サーバー管理権限が必要）"
        ),
        inline=False
    )

    embed.set_footer(text="ステージ作成者のみパネルを操作できます。楽しいステージを！🎤")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stage-botstatus", description="Botの稼働状況を確認します（サーバー管理権限が必要）。")
@app_commands.checks.has_permissions(manage_guild=True)
async def stage_botstatus(interaction: discord.Interaction):
    uptime_seconds = int(time.time() - START_TIME)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    active_stages_this_guild = sum(
        1 for data in temp_channels.values() if data.get("guild_id") == interaction.guild_id
    )

    embed = discord.Embed(title="🩺 ステージBot稼働状況", color=discord.Color.blurple())
    embed.add_field(name="Discord APIレイテンシ", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="起動からの経過時間", value=f"{hours}時間{minutes}分{seconds}秒", inline=True)
    embed.add_field(name="このサーバーの稼働中ステージ数", value=str(active_stages_this_guild), inline=True)

    for path, label in [(GUILD_CONFIG_PATH, "guild_config.json"), (TEMP_CHANNELS_PATH, "temp_channels.json"), (PRESETS_PATH, "presets.json")]:
        try:
            load_json(path, {})
            status = "✅ 読み込み可能"
        except Exception as e:
            status = f"❌ エラー：{e}"
        embed.add_field(name=label, value=status, inline=True)

    embed.set_footer(text="直近のエラーは /stage-recent-errors で確認できます。")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stage-recent-errors", description="直近のエラーログを表示します（サーバー管理権限が必要）。")
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(lines="表示する行数（デフォルト：40）")
async def stage_recent_errors(interaction: discord.Interaction, lines: int = 40):
    if not ERROR_LOG_PATH.exists():
        await interaction.response.send_message("エラーログはまだありません（記録されたエラーがありません）。", ephemeral=True)
        return

    with ERROR_LOG_PATH.open("r", encoding="utf-8") as f:
        all_lines = f.readlines()

    if not all_lines:
        await interaction.response.send_message("エラーログはまだありません（記録されたエラーがありません）。", ephemeral=True)
        return

    recent = "".join(all_lines[-lines:])
    preview = recent[-1900:] if len(recent) > 1900 else recent

    await interaction.response.send_message(
        content=f"直近{min(lines, len(all_lines))}行：\n```\n{preview}\n```",
        file=discord.File(fp=io.BytesIO(recent.encode("utf-8")), filename="stage_bot_errors.log"),
        ephemeral=True
    )


