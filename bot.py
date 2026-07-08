"""
一時ステージチャンネル作成bot
----------------------------------
・ボイスチャンネルに参加すると、自動的に一時的なステージを作成。
・コントロールパネルから「パスワード」を設定可能。
・パスワード設定時、好きなテキストチャンネルへ「招待用パネル」を自動送信。
・参加者はチャットのボタンを押してパスワードを入力するだけで入室可能！
・公開/非公開の切り替え、メニューからの直接招待機能付き。
・ステージ終了時にチャットログをDMへダウンロード可能。
・ブロック＆キック機能：メンバーをキックして再入室を禁止可能。
・マイ・プリセット機能：ステージ設定（名前/トピック/人数制限）をユーザーごとに保存・適用。
・サブ管理者（共同ホスト）指名機能：指名されたメンバーもコントロールパネルを操作可能。
・モバイル向け簡易パネル：ボタンを4つに絞ったシンプルモードに切り替え可能。
・エンドツーエンド招待：使い捨てトークンで特定の人だけを招待できる。
・同時配信告知連携：YouTube/Twitchなどの配信URLをEmbedカードで告知できる。
"""

import io
import json
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN が .env に設定されていません。")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
GUILD_CONFIG_PATH = DATA_DIR / "guild_config.json"
TEMP_CHANNELS_PATH = DATA_DIR / "temp_channels.json"
PRESETS_PATH = DATA_DIR / "presets.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("stage-bot")

DEFAULT_NAME_TEMPLATE = "🎤・{username} のステージ"
DEFAULT_TOPIC_TEMPLATE = "{username} の雑談ステージ"


def load_json(path: Path, default):
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            log.warning("%s の読み込みに失敗しました。初期値を使用します。", path)
    return default


def save_json(path: Path, data) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


guild_config: dict = load_json(GUILD_CONFIG_PATH, {})

# 旧バージョンのデータと互換性を持たせる処理
raw_temp_channels = load_json(TEMP_CHANNELS_PATH, {})
temp_channels: dict = {}
for k, v in raw_temp_channels.items():
    if isinstance(v, int):
        temp_channels[k] = {"guild_id": v, "password": None, "invite_msg_ids": [], "sub_admins": [], "blocked_users": [], "tickets": {}}
    else:
        if "invite_msg_ids" not in v:
            v["invite_msg_ids"] = []
        if "sub_admins" not in v:
            v["sub_admins"] = []
        if "blocked_users" not in v:
            v["blocked_users"] = []
        if "tickets" not in v:
            v["tickets"] = {}
        if "reaction_thread_id" not in v:
            v["reaction_thread_id"] = None
        temp_channels[k] = v

# ユーザーごとのプリセットデータ: {user_id_str: [{name, topic, limit, label}, ...]}
user_presets: dict = load_json(PRESETS_PATH, {})


def save_guild_config() -> None:
    save_json(GUILD_CONFIG_PATH, guild_config)


def save_temp_channels() -> None:
    save_json(TEMP_CHANNELS_PATH, temp_channels)


def save_presets() -> None:
    save_json(PRESETS_PATH, user_presets)


intents = discord.Intents.default()
intents.voice_states = True
intents.message_content = True

bot = commands.Bot(command_prefix="stagebot!", intents=intents)


# ==========================================
# ログファイル生成処理
# ==========================================
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


# ==========================================
# パスワード用の招待パネルUI
# ==========================================
class JoinPasswordModal(discord.ui.Modal, title="パスワード入力"):
    def __init__(self, target_channel: discord.StageChannel):
        super().__init__()
        self.target_channel = target_channel
        self.pwd_field = discord.ui.TextInput(
            label=f"パスワード",
            style=discord.TextStyle.short,
            placeholder="パスワードを入力してください",
            required=True
        )
        self.add_item(self.pwd_field)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        channel_id_str = str(self.target_channel.id)
        
        if channel_id_str not in temp_channels:
            await interaction.followup.send("❌ このステージは既に終了しているか、無効です。", ephemeral=True)
            return
            
        correct_pwd = temp_channels[channel_id_str].get("password")
        if not correct_pwd:
            await interaction.followup.send("ℹ️ このステージには現在パスワードが設定されていません。", ephemeral=True)
            return
            
        if self.pwd_field.value.strip() == correct_pwd:
            overwrite = self.target_channel.overwrites_for(interaction.user)
            overwrite.view_channel = True
            overwrite.connect = True
            overwrite.stream = True
            try:
                await self.target_channel.set_permissions(interaction.user, overwrite=overwrite, reason="パスワード認証成功")
                await interaction.followup.send(f"✅ パスワードが一致しました！\n{self.target_channel.mention} をクリックしてご入室ください。", ephemeral=True)
            except discord.HTTPException:
                await interaction.followup.send("❌ 権限の付与に失敗しました。", ephemeral=True)
        else:
            await interaction.followup.send("❌ パスワードが間違っています。", ephemeral=True)


class JoinPromptView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="パスワードを入力して参加", style=discord.ButtonStyle.primary, emoji="🔑", custom_id="join_stage_prompt_btn")
    async def btn_join(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg_id = interaction.message.id
        target_stage_id = None
        
        for ch_id_str, data in temp_channels.items():
            if msg_id in data.get("invite_msg_ids", []):
                target_stage_id = int(ch_id_str)
                break
                
        if target_stage_id:
            channel = interaction.guild.get_channel(target_stage_id)
            if channel and isinstance(channel, discord.StageChannel):
                if not temp_channels[str(target_stage_id)].get("password"):
                    await interaction.response.send_message("ℹ️ このステージのパスワードロックは現在解除されています。そのままご参加ください！", ephemeral=True)
                    return
                await interaction.response.send_modal(JoinPasswordModal(channel))
            else:
                await interaction.response.send_message("❌ このステージは既に終了しました。", ephemeral=True)
        else:
            await interaction.response.send_message("❌ この招待パネルは無効です。", ephemeral=True)


class SendInvitePanelView(discord.ui.View):
    def __init__(self, stage_id_str: str):
        super().__init__(timeout=300)
        self.stage_id_str = stage_id_str

    @discord.ui.select(cls=discord.ui.ChannelSelect, channel_types=[discord.ChannelType.text], placeholder="招待パネルを送信するチャンネルを選択...")
    async def select_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        # 先に応答を遅らせることで、処理落ち（インタラクション失敗）を確実に防ぎます
        await interaction.response.defer(ephemeral=True)
        
        # 選択されたチャンネルのIDから、Discordのチャンネル情報を再取得する（エラー対策）
        target_channel_id = select.values[0].id
        target_channel = interaction.guild.get_channel(target_channel_id)
        stage_channel = interaction.guild.get_channel(int(self.stage_id_str))

        if not target_channel:
            await interaction.followup.send("❌ 送信先のチャンネルが見つかりませんでした。", ephemeral=True)
            return

        if not stage_channel:
            await interaction.followup.send("❌ ステージが見つかりません。", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"🔒 プライベートステージ: {stage_channel.name}",
            description=(
                f"{interaction.user.mention} さんがパスワード付きのステージを作成しました。\n"
                "参加するには、下のボタンを押してパスワードを入力してください。"
            ),
            color=discord.Color.red()
        )
        
        try:
            msg = await target_channel.send(embed=embed, view=JoinPromptView())
            
            if self.stage_id_str in temp_channels:
                temp_channels[self.stage_id_str]["invite_msg_ids"].append(msg.id)
                save_temp_channels()

            # セレクトメニューを消して完了メッセージに変更
            await interaction.edit_original_response(content=f"✅ {target_channel.mention} に招待パネルを送信しました！", view=None)
        except discord.Forbidden:
            await interaction.followup.send("❌ 指定されたチャンネルにメッセージを送信する権限がありません。", ephemeral=True)
        except Exception as e:
            log.exception("招待パネル送信エラー")
            await interaction.followup.send("❌ パネルの送信中に予期せぬエラーが発生しました。", ephemeral=True)


# ==========================================
# コンソール用 UI (Modal)
# ==========================================
class StageEditModal(discord.ui.Modal):
    def __init__(self, edit_type: str):
        title = "チャンネル名の変更" if edit_type == "name" else "トピックの変更"
        super().__init__(title=title)
        self.edit_type = edit_type
        self.input_field = discord.ui.TextInput(
            label="新しい名前" if edit_type == "name" else "新しいトピック",
            style=discord.TextStyle.short,
            required=True,
            max_length=100 if edit_type == "name" else 120
        )
        self.add_item(self.input_field)

    async def on_submit(self, interaction: discord.Interaction):
        channel = interaction.channel
        new_val = self.input_field.value.strip()
        await interaction.response.defer(ephemeral=True)
        try:
            if self.edit_type == "name":
                is_private = channel.overwrites_for(channel.guild.default_role).view_channel is False
                if is_private and not new_val.startswith("🔒"):
                    new_val = f"🔒{new_val}"
                elif not is_private and new_val.startswith("🔒"):
                    new_val = new_val.replace("🔒", "", 1).strip()
                
                await channel.edit(name=new_val[:100], reason=f"{interaction.user} が変更")
                await interaction.followup.send(f"✅ チャンネル名を「{new_val}」に変更しました。", ephemeral=True)
            else:
                instance = channel.instance
                if instance:
                    await instance.edit(topic=new_val)
                else:
                    await channel.create_instance(topic=new_val)
                await interaction.followup.send(f"✅ トピックを「{new_val}」に変更しました。", ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send("❌ 変更に失敗しました。※Discordの制限により名前の変更は10分間に2回までです。", ephemeral=True)


class StageLimitModal(discord.ui.Modal, title="参加人数の制限"):
    input_field = discord.ui.TextInput(
        label="最大人数 (0 で無制限)", style=discord.TextStyle.short, required=True, max_length=5
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            limit = int(self.input_field.value.strip())
            if limit < 0 or limit > 10000: raise ValueError
        except ValueError:
            await interaction.response.send_message("❌ 0から10000までの半角数字で入力してください。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.channel.edit(user_limit=limit)
            msg = f"✅ 参加人数制限を「{limit}人」に設定しました。" if limit > 0 else "✅ 参加人数制限を「無制限」にしました。"
            await interaction.followup.send(msg, ephemeral=True)
        except Exception:
            await interaction.followup.send("❌ エラーが発生しました。", ephemeral=True)


class StagePasswordModal(discord.ui.Modal, title="パスワードの設定"):
    pwd_field = discord.ui.TextInput(
        label="パスワード (空欄でロック解除)",
        style=discord.TextStyle.short,
        placeholder="例: 1234, secret",
        required=False,
        max_length=20
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        new_pwd = self.pwd_field.value.strip()
        channel_id_str = str(channel.id)
        
        if not new_pwd:
            # パスワード解除
            temp_channels[channel_id_str]["password"] = None
            save_temp_channels()
            await interaction.followup.send("🔓 パスワードロックを解除しました。（※ステージ自体は現在の公開/非公開状態を維持します）", ephemeral=True)
            return
            
        # パスワード設定＆自動で非公開にする
        temp_channels[channel_id_str]["password"] = new_pwd
        save_temp_channels()
        
        guild = interaction.guild
        default_role = guild.default_role
        overwrite = channel.overwrites_for(default_role)
        
        if overwrite.view_channel is not False:
            overwrite.view_channel = False
            new_name = channel.name
            if not new_name.startswith("🔒"):
                new_name = f"🔒{new_name}"
            try:
                await channel.edit(name=new_name[:100])
                await channel.set_permissions(default_role, overwrite=overwrite, reason="パスワード設定により非公開化")
            except discord.HTTPException:
                pass

        view = SendInvitePanelView(channel_id_str)
        await interaction.followup.send(
            f"🔐 パスワードを「{new_pwd}」に設定し、非公開にしました！\n\n"
            "他のメンバーが簡単に参加できるようにするための「招待パネル」を送信します。\n"
            "送信先のテキストチャンネルを下から選んでください。",
            view=view,
            ephemeral=True
        )


# ==========================================
# コマンドからの参加用 UI（フォールバック）
# ==========================================
class JoinStageSelect(discord.ui.Select):
    def __init__(self, stage_options):
        super().__init__(placeholder="参加したいステージを選択...", min_values=1, max_values=1, options=stage_options)

    async def callback(self, interaction: discord.Interaction):
        channel_id = int(self.values[0])
        channel = interaction.guild.get_channel(channel_id)
        if isinstance(channel, discord.StageChannel):
            await interaction.response.send_modal(JoinPasswordModal(channel))
        else:
            await interaction.response.send_message("❌ ステージが見つかりません。", ephemeral=True)


class JoinStageView(discord.ui.View):
    def __init__(self, stage_options):
        super().__init__(timeout=120)
        self.add_item(JoinStageSelect(stage_options))


# ==========================================
# 終了確認 View
# ==========================================
class DeleteConfirmView(discord.ui.View):
    def __init__(self, target_channel: discord.StageChannel):
        super().__init__(timeout=60)
        self.target_channel = target_channel

    @discord.ui.button(label="ログをダウンロードして終了", style=discord.ButtonStyle.primary, emoji="📥")
    async def btn_download_and_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        log_file = await generate_log_file(self.target_channel)
        
        if log_file:
            try:
                await interaction.user.send(f"ステージ「{self.target_channel.name}」のチャットログです。", file=log_file)
                await interaction.followup.send("✅ DMにチャットログを送信しました。ステージを終了します...", ephemeral=True)
            except discord.Forbidden:
                await interaction.followup.send("❌ DMがブロックされているため送信できませんでした。そのままステージを終了します...", ephemeral=True)
        else:
            await interaction.followup.send("ℹ️ 保存するチャット履歴がありませんでした。ステージを終了します...", ephemeral=True)
            
        await maybe_delete_temp_channel(self.target_channel, force=True)

    @discord.ui.button(label="そのまま終了", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def btn_delete_only(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send("✅ ステージを終了します...", ephemeral=True)
        await maybe_delete_temp_channel(self.target_channel, force=True)


# ==========================================
# メインのコントロールパネル（隠しメニュー）
# ==========================================
class StageConsoleView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        channel = interaction.channel
        if not isinstance(channel, discord.StageChannel): return False
        channel_id_str = str(channel.id)
        is_owner = channel.permissions_for(interaction.user).manage_channels
        is_sub_admin = interaction.user.id in temp_channels.get(channel_id_str, {}).get("sub_admins", [])
        if not (is_owner or is_sub_admin):
            await interaction.response.send_message("⚠️ この操作を行えるのはステージ作成者またはサブ管理者のみです。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="名前を変更", style=discord.ButtonStyle.primary, custom_id="console_btn_name", emoji="📝", row=0)
    async def btn_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StageEditModal("name"))

    @discord.ui.button(label="トピックを変更", style=discord.ButtonStyle.success, custom_id="console_btn_topic", emoji="📢", row=0)
    async def btn_topic(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StageEditModal("topic"))

    @discord.ui.button(label="人数制限", style=discord.ButtonStyle.secondary, custom_id="console_btn_limit", emoji="👥", row=0)
    async def btn_limit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StageLimitModal())

    @discord.ui.button(label="公開/非公開", style=discord.ButtonStyle.secondary, custom_id="console_btn_private", emoji="🔒", row=1)
    async def btn_private(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        guild = interaction.guild
        default_role = guild.default_role

        overwrite = channel.overwrites_for(default_role)
        is_private = overwrite.view_channel is False

        if is_private:
            overwrite.view_channel = True
            overwrite.connect = True
            overwrite.stream = True
            new_name = channel.name
            if new_name.startswith("🔒"):
                new_name = new_name.replace("🔒", "", 1).strip()
            try:
                await channel.edit(name=new_name[:100])
                await channel.set_permissions(default_role, overwrite=overwrite)
                await interaction.followup.send("🔓 ステージを**公開**に変更しました。", ephemeral=True)
            except discord.HTTPException:
                await interaction.followup.send("❌ 変更に失敗しました。(10分制限の可能性)", ephemeral=True)
        else:
            overwrite.view_channel = False
            new_name = channel.name
            if not new_name.startswith("🔒"):
                new_name = f"🔒{new_name}"
            try:
                await channel.edit(name=new_name[:100])
                await channel.set_permissions(default_role, overwrite=overwrite)
                await interaction.followup.send("🔒 ステージを**非公開**に変更しました。", ephemeral=True)
            except discord.HTTPException:
                await interaction.followup.send("❌ 変更に失敗しました。(10分制限の可能性)", ephemeral=True)

    @discord.ui.button(label="パスワード", style=discord.ButtonStyle.secondary, custom_id="console_btn_password", emoji="🔐", row=1)
    async def btn_password(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StagePasswordModal())

    @discord.ui.button(label="ステージ終了", style=discord.ButtonStyle.danger, custom_id="console_btn_delete", emoji="🗑️", row=1)
    async def btn_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="⚠️ ステージの終了確認",
            description="本当にステージを終了して削除しますか？\n終了する前にチャット履歴をダウンロードできます。",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, view=DeleteConfirmView(interaction.channel), ephemeral=True)

    @discord.ui.button(label="ブロック＆キック", style=discord.ButtonStyle.danger, custom_id="console_btn_block", emoji="🚫", row=2)
    async def btn_block(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "## 🚫 ブロック＆キック管理\nキックしたメンバーはステージから退出させられ、再入室が禁止されます。",
            view=BlockMenuView(interaction.channel), ephemeral=True
        )

    @discord.ui.button(label="マイ・プリセット", style=discord.ButtonStyle.primary, custom_id="console_btn_preset", emoji="📌", row=2)
    async def btn_preset(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id_str = str(interaction.user.id)
        presets = user_presets.get(user_id_str, [])
        count_text = f"（現在 {len(presets)}/{MAX_PRESETS} 件保存中）"
        await interaction.response.send_message(
            f"## 📌 マイ・プリセット {count_text}\nステージ名・トピック・人数制限のセットを保存して、ワンクリックで適用できます。",
            view=PresetMenuView(user_id_str), ephemeral=True
        )

    @discord.ui.button(label="サブ管理者", style=discord.ButtonStyle.secondary, custom_id="console_btn_subadmin", emoji="👑", row=2)
    async def btn_subadmin(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id_str = str(interaction.channel.id)
        sub_admins = temp_channels.get(channel_id_str, {}).get("sub_admins", [])
        guild = interaction.guild
        mentions = []
        for uid in sub_admins:
            m = guild.get_member(uid)
            mentions.append(m.mention if m else f"ID:{uid}")
        current = f"\n現在のサブ管理者: {', '.join(mentions)}" if mentions else "\n現在サブ管理者はいません。"
        await interaction.response.send_message(
            f"## 👑 サブ管理者（共同ホスト）管理{current}\nサブ管理者はコントロールパネルの全機能を使用できます。",
            view=SubAdminView(interaction.channel), ephemeral=True
        )

    @discord.ui.button(label="簡易パネル", style=discord.ButtonStyle.secondary, custom_id="console_btn_simple", emoji="📲", row=3)
    async def btn_simple(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="📲 簡易コントロールパネル",
            description="よく使う操作だけをまとめたシンプルモードです。\n*(※このパネルはあなたにしか見えていません)*",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, view=SimplePanelView(), ephemeral=True)

    @discord.ui.button(label="チケット招待", style=discord.ButtonStyle.primary, custom_id="console_btn_ticket", emoji="🎟️", row=3)
    async def btn_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id_str = str(interaction.channel.id)
        ticket_count = len(temp_channels.get(channel_id_str, {}).get("tickets", {}))
        await interaction.response.send_message(
            f"## 🎟️ 使い捨てチケット招待\n"
            f"特定の人だけを招待する「使い捨てチケット」を発行できます。\n"
            f"相手は `/stage-ticket` コマンドでコードを入力するだけで入室できます。\n"
            f"現在の発行枚数: **{ticket_count}/20**",
            view=TicketIssueView(interaction.channel), ephemeral=True
        )

    @discord.ui.button(label="配信告知", style=discord.ButtonStyle.success, custom_id="console_btn_stream", emoji="📺", row=3)
    async def btn_stream(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StreamAnnounceModal())

    @discord.ui.button(label="リアクションパネル再送", style=discord.ButtonStyle.secondary, custom_id="console_btn_reaction_resend", emoji="🎉", row=3)
    async def btn_reaction_resend(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.channel.send(
                "🎉 **リアクションパネル**\n好きなタイミングでボタンを押してください！",
                view=SummonAndReactionView()
            )
            await interaction.followup.send("✅ リアクションパネルを再送しました。", ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send("❌ 再送に失敗しました。", ephemeral=True)

    @discord.ui.select(
        cls=discord.ui.UserSelect, placeholder="👤 ここから招待するメンバーを選択 (複数可)",
        min_values=1, max_values=25, row=4, custom_id="console_select_invite"
    )
    async def select_invite(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        channel = interaction.channel
        await interaction.response.defer(ephemeral=True)
        added_users = []
        for user in select.values:
            if isinstance(user, discord.Member):
                overwrite = channel.overwrites_for(user)
                overwrite.view_channel = True
                overwrite.connect = True
                overwrite.stream = True
                try:
                    await channel.set_permissions(user, overwrite=overwrite)
                    added_users.append(user.mention)
                except discord.HTTPException:
                    pass

        if added_users:
            await interaction.followup.send(f"✅ 以下のメンバーを招待しました！\n{', '.join(added_users)}", ephemeral=True)
            try:
                await channel.send(f"🔔 {', '.join(added_users)} さん！\n{interaction.user.mention} さんからステージに招待されました！")
            except discord.HTTPException:
                pass
        else:
            await interaction.followup.send("❌ 招待に失敗しました。", ephemeral=True)


# ==========================================
# ブロック＆キック UI
# ==========================================
class BlockKickView(discord.ui.View):
    def __init__(self, channel: discord.StageChannel):
        super().__init__(timeout=120)
        self.channel = channel

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="🚫 キック＆ブロックするメンバーを選択...",
        min_values=1, max_values=10
    )
    async def select_block(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        channel_id_str = str(self.channel.id)
        blocked = temp_channels.get(channel_id_str, {}).get("blocked_users", [])

        kicked = []
        already_blocked = []
        failed = []

        for user in select.values:
            if not isinstance(user, discord.Member):
                continue
            # ステージ作成者（manage_channels持ち）とBotはブロック不可
            if user.bot or self.channel.permissions_for(user).manage_channels:
                failed.append(user.mention)
                continue
            if user.id in blocked:
                already_blocked.append(user.mention)
                continue

            blocked.append(user.id)
            # チャンネルへのアクセスを禁止
            overwrite = self.channel.overwrites_for(user)
            overwrite.view_channel = False
            overwrite.connect = False
            try:
                await self.channel.set_permissions(user, overwrite=overwrite, reason="ブロック＆キック")
                # ボイスから退出させる
                if user.voice and user.voice.channel == self.channel:
                    await user.move_to(None, reason="ブロックによるキック")
                kicked.append(user.mention)
            except discord.HTTPException:
                failed.append(user.mention)
                blocked.remove(user.id)

        temp_channels[channel_id_str]["blocked_users"] = blocked
        save_temp_channels()

        lines = []
        if kicked:
            lines.append(f"🚫 キック＆ブロック完了: {', '.join(kicked)}")
        if already_blocked:
            lines.append(f"⚠️ 既にブロック済み: {', '.join(already_blocked)}")
        if failed:
            lines.append(f"❌ 処理できませんでした: {', '.join(failed)}")

        await interaction.followup.send("\n".join(lines) or "❌ 対象メンバーがいません。", ephemeral=True)


class UnblockView(discord.ui.View):
    def __init__(self, channel: discord.StageChannel):
        super().__init__(timeout=120)
        self.channel = channel

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="🔓 ブロックを解除するメンバーを選択...",
        min_values=1, max_values=10
    )
    async def select_unblock(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        channel_id_str = str(self.channel.id)
        blocked = temp_channels.get(channel_id_str, {}).get("blocked_users", [])

        unblocked = []
        not_blocked = []

        for user in select.values:
            if not isinstance(user, discord.Member):
                continue
            if user.id not in blocked:
                not_blocked.append(user.mention)
                continue
            blocked.remove(user.id)
            # 権限を元に戻す（ロールのデフォルトに委ねる）
            try:
                await self.channel.set_permissions(user, overwrite=None, reason="ブロック解除")
                unblocked.append(user.mention)
            except discord.HTTPException:
                blocked.append(user.id)

        temp_channels[channel_id_str]["blocked_users"] = blocked
        save_temp_channels()

        lines = []
        if unblocked:
            lines.append(f"🔓 ブロック解除: {', '.join(unblocked)}")
        if not_blocked:
            lines.append(f"ℹ️ ブロックされていません: {', '.join(not_blocked)}")

        await interaction.followup.send("\n".join(lines) or "❌ 対象メンバーがいません。", ephemeral=True)


class BlockMenuView(discord.ui.View):
    """ブロック操作の選択メニュー"""
    def __init__(self, channel: discord.StageChannel):
        super().__init__(timeout=120)
        self.channel = channel

    @discord.ui.button(label="キック＆ブロック", style=discord.ButtonStyle.danger, emoji="🚫")
    async def btn_block(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "ブロックするメンバーを選択してください。\n（選択したメンバーはステージから退出させられ、再入室が禁止されます）",
            view=BlockKickView(self.channel), ephemeral=True
        )

    @discord.ui.button(label="ブロック解除", style=discord.ButtonStyle.secondary, emoji="🔓")
    async def btn_unblock(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id_str = str(self.channel.id)
        blocked_ids = temp_channels.get(channel_id_str, {}).get("blocked_users", [])
        if not blocked_ids:
            await interaction.response.send_message("ℹ️ 現在ブロックしているメンバーはいません。", ephemeral=True)
            return
        await interaction.response.send_message(
            "ブロックを解除するメンバーを選択してください。",
            view=UnblockView(self.channel), ephemeral=True
        )


# ==========================================
# マイ・プリセット UI
# ==========================================
MAX_PRESETS = 5

class PresetSaveModal(discord.ui.Modal, title="プリセットを保存"):
    label_field = discord.ui.TextInput(
        label="プリセット名（例: 雑談ステージ）",
        style=discord.TextStyle.short, required=True, max_length=30
    )
    name_field = discord.ui.TextInput(
        label="ステージ名（空欄で現在の名前を使用）",
        style=discord.TextStyle.short, required=False, max_length=100
    )
    topic_field = discord.ui.TextInput(
        label="トピック（空欄でスキップ）",
        style=discord.TextStyle.short, required=False, max_length=120
    )
    limit_field = discord.ui.TextInput(
        label="人数制限（0=無制限、空欄でスキップ）",
        style=discord.TextStyle.short, required=False, max_length=5
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        user_id_str = str(interaction.user.id)
        channel = interaction.channel

        presets = user_presets.get(user_id_str, [])
        if len(presets) >= MAX_PRESETS:
            await interaction.followup.send(
                f"❌ プリセットは最大{MAX_PRESETS}件まで保存できます。\n不要なプリセットを削除してから再度お試しください。",
                ephemeral=True
            )
            return

        # 人数制限のバリデーション
        limit = None
        if self.limit_field.value.strip():
            try:
                limit = int(self.limit_field.value.strip())
                if limit < 0 or limit > 10000:
                    raise ValueError
            except ValueError:
                await interaction.followup.send("❌ 人数制限は0〜10000の数字で入力してください。", ephemeral=True)
                return

        preset = {
            "label": self.label_field.value.strip(),
            "name": self.name_field.value.strip() or channel.name,
            "topic": self.topic_field.value.strip() or None,
            "limit": limit,
        }
        presets.append(preset)
        user_presets[user_id_str] = presets
        save_presets()

        summary = f"📌 **{preset['label']}** を保存しました！\n"
        summary += f"　ステージ名: {preset['name']}\n"
        if preset["topic"]:
            summary += f"　トピック: {preset['topic']}\n"
        if preset["limit"] is not None:
            summary += f"　人数制限: {preset['limit']}人\n"
        await interaction.followup.send(summary, ephemeral=True)


class PresetApplySelect(discord.ui.Select):
    def __init__(self, presets: list):
        options = [
            discord.SelectOption(
                label=p["label"],
                value=str(i),
                description=f"名前: {p['name'][:40]}" + (f" | {p['limit']}人" if p.get("limit") is not None else ""),
                emoji="📌"
            )
            for i, p in enumerate(presets)
        ]
        super().__init__(placeholder="適用するプリセットを選択...", min_values=1, max_values=1, options=options)
        self.presets = presets

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        idx = int(self.values[0])
        preset = self.presets[idx]
        channel = interaction.channel

        results = []
        try:
            new_name = preset["name"]
            is_private = channel.overwrites_for(channel.guild.default_role).view_channel is False
            if is_private and not new_name.startswith("🔒"):
                new_name = f"🔒{new_name}"
            await channel.edit(name=new_name[:100])
            results.append(f"✅ 名前を「{new_name}」に変更")
        except discord.HTTPException:
            results.append("⚠️ 名前の変更に失敗 (10分制限の可能性)")

        if preset.get("topic"):
            try:
                instance = channel.instance
                if instance:
                    await instance.edit(topic=preset["topic"])
                else:
                    await channel.create_instance(topic=preset["topic"])
                results.append(f"✅ トピックを「{preset['topic']}」に変更")
            except discord.HTTPException:
                results.append("⚠️ トピックの変更に失敗")

        if preset.get("limit") is not None:
            try:
                await channel.edit(user_limit=preset["limit"])
                label = f"{preset['limit']}人" if preset["limit"] > 0 else "無制限"
                results.append(f"✅ 人数制限を「{label}」に設定")
            except discord.HTTPException:
                results.append("⚠️ 人数制限の変更に失敗")

        await interaction.followup.send(
            f"📌 プリセット「{preset['label']}」を適用しました！\n" + "\n".join(results),
            ephemeral=True
        )


class PresetDeleteSelect(discord.ui.Select):
    def __init__(self, user_id_str: str, presets: list):
        self.user_id_str = user_id_str
        self.presets = presets
        options = [
            discord.SelectOption(label=p["label"], value=str(i), emoji="🗑️")
            for i, p in enumerate(presets)
        ]
        super().__init__(placeholder="削除するプリセットを選択...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        idx = int(self.values[0])
        removed = self.presets.pop(idx)
        user_presets[self.user_id_str] = self.presets
        save_presets()
        await interaction.followup.send(f"🗑️ プリセット「{removed['label']}」を削除しました。", ephemeral=True)


class PresetMenuView(discord.ui.View):
    """プリセット操作の選択メニュー"""
    def __init__(self, user_id_str: str):
        super().__init__(timeout=120)
        self.user_id_str = user_id_str

    @discord.ui.button(label="プリセットを保存", style=discord.ButtonStyle.primary, emoji="💾")
    async def btn_save(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(PresetSaveModal())

    @discord.ui.button(label="プリセットを適用", style=discord.ButtonStyle.success, emoji="▶️")
    async def btn_apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        presets = user_presets.get(self.user_id_str, [])
        if not presets:
            await interaction.response.send_message("ℹ️ 保存されているプリセットがありません。先に保存してください。", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(PresetApplySelect(presets))
        await interaction.response.send_message("適用するプリセットを選択してください：", view=view, ephemeral=True)

    @discord.ui.button(label="プリセットを削除", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def btn_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        presets = user_presets.get(self.user_id_str, [])
        if not presets:
            await interaction.response.send_message("ℹ️ 削除できるプリセットがありません。", ephemeral=True)
            return
        view = discord.ui.View(timeout=120)
        view.add_item(PresetDeleteSelect(self.user_id_str, presets[:]))
        await interaction.response.send_message("削除するプリセットを選択してください：", view=view, ephemeral=True)


# ==========================================
# サブ管理者（共同ホスト）指名 UI
# ==========================================
class SubAdminView(discord.ui.View):
    def __init__(self, channel: discord.StageChannel):
        super().__init__(timeout=120)
        self.channel = channel

    @discord.ui.button(label="サブ管理者を追加", style=discord.ButtonStyle.primary, emoji="👑")
    async def btn_add(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "サブ管理者として追加するメンバーを選択してください。\n（選択されたメンバーはコントロールパネルの操作が可能になります）",
            view=SubAdminAddView(self.channel), ephemeral=True
        )

    @discord.ui.button(label="サブ管理者を解除", style=discord.ButtonStyle.secondary, emoji="🔧")
    async def btn_remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id_str = str(self.channel.id)
        sub_admins = temp_channels.get(channel_id_str, {}).get("sub_admins", [])
        if not sub_admins:
            await interaction.response.send_message("ℹ️ 現在サブ管理者はいません。", ephemeral=True)
            return

        # 現在のサブ管理者一覧を表示して確認
        guild = interaction.guild
        mentions = []
        for uid in sub_admins:
            m = guild.get_member(uid)
            mentions.append(m.mention if m else f"ID:{uid}")

        await interaction.response.send_message(
            f"現在のサブ管理者: {', '.join(mentions)}\n解除するメンバーを選択してください。",
            view=SubAdminRemoveView(self.channel), ephemeral=True
        )


class SubAdminAddView(discord.ui.View):
    def __init__(self, channel: discord.StageChannel):
        super().__init__(timeout=120)
        self.channel = channel

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="👑 サブ管理者にするメンバーを選択...",
        min_values=1, max_values=10
    )
    async def select_add(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        channel_id_str = str(self.channel.id)
        sub_admins = temp_channels.get(channel_id_str, {}).get("sub_admins", [])

        added = []
        already = []
        failed = []

        for user in select.values:
            if not isinstance(user, discord.Member) or user.bot:
                continue
            # ステージオーナー（manage_channels持ち）はサブ管理者指名不要
            if self.channel.permissions_for(user).manage_channels:
                already.append(user.mention)
                continue
            if user.id in sub_admins:
                already.append(user.mention)
                continue
            sub_admins.append(user.id)
            # コントロールパネルを開くためのチャンネル閲覧・接続権限を付与
            overwrite = self.channel.overwrites_for(user)
            overwrite.manage_channels = True
            try:
                await self.channel.set_permissions(user, overwrite=overwrite, reason="サブ管理者指名")
                added.append(user.mention)
            except discord.HTTPException:
                sub_admins.remove(user.id)
                failed.append(user.mention)

        temp_channels[channel_id_str]["sub_admins"] = sub_admins
        save_temp_channels()

        lines = []
        if added:
            lines.append(f"👑 サブ管理者に追加しました: {', '.join(added)}")
            # チャンネルへ通知
            try:
                await self.channel.send(
                    f"👑 {', '.join(added)} さんがサブ管理者（共同ホスト）に指名されました！\n"
                    "「コントロールパネルを開く」ボタンからステージの設定が可能です。"
                )
            except discord.HTTPException:
                pass
        if already:
            lines.append(f"ℹ️ 既にサブ管理者またはオーナーです: {', '.join(already)}")
        if failed:
            lines.append(f"❌ 追加に失敗: {', '.join(failed)}")

        await interaction.followup.send("\n".join(lines) or "❌ 対象メンバーがいません。", ephemeral=True)


class SubAdminRemoveView(discord.ui.View):
    def __init__(self, channel: discord.StageChannel):
        super().__init__(timeout=120)
        self.channel = channel

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="🔧 解除するサブ管理者を選択...",
        min_values=1, max_values=10
    )
    async def select_remove(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        channel_id_str = str(self.channel.id)
        sub_admins = temp_channels.get(channel_id_str, {}).get("sub_admins", [])

        removed = []
        not_found = []

        for user in select.values:
            if not isinstance(user, discord.Member):
                continue
            if user.id not in sub_admins:
                not_found.append(user.mention)
                continue
            sub_admins.remove(user.id)
            # manage_channels 権限を取り消す
            overwrite = self.channel.overwrites_for(user)
            overwrite.manage_channels = None
            try:
                await self.channel.set_permissions(user, overwrite=overwrite, reason="サブ管理者解除")
                removed.append(user.mention)
            except discord.HTTPException:
                sub_admins.append(user.id)

        temp_channels[channel_id_str]["sub_admins"] = sub_admins
        save_temp_channels()

        lines = []
        if removed:
            lines.append(f"🔧 サブ管理者を解除しました: {', '.join(removed)}")
        if not_found:
            lines.append(f"ℹ️ サブ管理者ではありません: {', '.join(not_found)}")

        await interaction.followup.send("\n".join(lines) or "❌ 対象メンバーがいません。", ephemeral=True)


# ==========================================
# 📲 モバイル向け簡易パネル
# ==========================================
class SimplePanelView(discord.ui.View):
    """ボタン4つに絞ったシンプルモード。モバイルでも押しやすい。"""
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        channel = interaction.channel
        if not isinstance(channel, discord.StageChannel):
            return False
        channel_id_str = str(channel.id)
        is_owner = channel.permissions_for(interaction.user).manage_channels
        is_sub_admin = interaction.user.id in temp_channels.get(channel_id_str, {}).get("sub_admins", [])
        if not (is_owner or is_sub_admin):
            await interaction.response.send_message("⚠️ この操作を行えるのはステージ作成者またはサブ管理者のみです。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="名前を変更", style=discord.ButtonStyle.primary, custom_id="simple_btn_name", emoji="📝", row=0)
    async def btn_name(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(StageEditModal("name"))

    @discord.ui.button(label="公開 / 非公開", style=discord.ButtonStyle.secondary, custom_id="simple_btn_private", emoji="🔒", row=0)
    async def btn_private(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        channel = interaction.channel
        guild = interaction.guild
        default_role = guild.default_role
        overwrite = channel.overwrites_for(default_role)
        is_private = overwrite.view_channel is False
        if is_private:
            overwrite.view_channel = True
            overwrite.connect = True
            overwrite.stream = True
            new_name = channel.name.replace("🔒", "", 1).strip() if channel.name.startswith("🔒") else channel.name
            try:
                await channel.edit(name=new_name[:100])
                await channel.set_permissions(default_role, overwrite=overwrite)
                await interaction.followup.send("🔓 **公開**に変更しました。", ephemeral=True)
            except discord.HTTPException:
                await interaction.followup.send("❌ 変更に失敗しました。(10分制限の可能性)", ephemeral=True)
        else:
            overwrite.view_channel = False
            new_name = channel.name if channel.name.startswith("🔒") else f"🔒{channel.name}"
            try:
                await channel.edit(name=new_name[:100])
                await channel.set_permissions(default_role, overwrite=overwrite)
                await interaction.followup.send("🔒 **非公開**に変更しました。", ephemeral=True)
            except discord.HTTPException:
                await interaction.followup.send("❌ 変更に失敗しました。(10分制限の可能性)", ephemeral=True)

    @discord.ui.button(label="メンバーを招待", style=discord.ButtonStyle.success, custom_id="simple_btn_invite", emoji="👤", row=1)
    async def btn_invite(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "招待するメンバーを選んでください。",
            view=SimpleInviteView(interaction.channel), ephemeral=True
        )

    @discord.ui.button(label="ステージ終了", style=discord.ButtonStyle.danger, custom_id="simple_btn_delete", emoji="🗑️", row=1)
    async def btn_delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="⚠️ ステージの終了確認",
            description="本当にステージを終了して削除しますか？",
            color=discord.Color.red()
        )
        await interaction.response.send_message(embed=embed, view=DeleteConfirmView(interaction.channel), ephemeral=True)


class SimpleInviteView(discord.ui.View):
    """簡易パネル用の招待セレクト（別Viewに分離してシンプルパネルのrow制限を回避）"""
    def __init__(self, channel: discord.StageChannel):
        super().__init__(timeout=120)
        self.channel = channel

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="👤 招待するメンバーを選択...",
        min_values=1, max_values=25
    )
    async def select_invite(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        await interaction.response.defer(ephemeral=True)
        added_users = []
        for user in select.values:
            if isinstance(user, discord.Member):
                overwrite = self.channel.overwrites_for(user)
                overwrite.view_channel = True
                overwrite.connect = True
                overwrite.stream = True
                try:
                    await self.channel.set_permissions(user, overwrite=overwrite)
                    added_users.append(user.mention)
                except discord.HTTPException:
                    pass
        if added_users:
            await interaction.followup.send(f"✅ 招待しました: {', '.join(added_users)}", ephemeral=True)
            try:
                await self.channel.send(
                    f"🔔 {', '.join(added_users)} さん！\n{interaction.user.mention} さんからステージに招待されました！"
                )
            except discord.HTTPException:
                pass
        else:
            await interaction.followup.send("❌ 招待に失敗しました。", ephemeral=True)


# ==========================================
# 🔏 エンドツーエンド招待（使い捨てトークン）
# ==========================================
TICKET_LENGTH = 8  # トークンの文字数


def generate_ticket() -> str:
    """英数字8文字のランダムトークンを生成する"""
    return secrets.token_urlsafe(TICKET_LENGTH)[:TICKET_LENGTH].upper()


class TicketIssueView(discord.ui.View):
    """トークン発行＆管理メニュー"""
    def __init__(self, channel: discord.StageChannel):
        super().__init__(timeout=120)
        self.channel = channel

    @discord.ui.button(label="新しいチケットを発行", style=discord.ButtonStyle.primary, emoji="🎟️")
    async def btn_issue(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        channel_id_str = str(self.channel.id)
        tickets: dict = temp_channels.get(channel_id_str, {}).get("tickets", {})

        if len(tickets) >= 20:
            await interaction.followup.send("❌ チケットは最大20枚までです。不要なチケットを削除してください。", ephemeral=True)
            return

        token = generate_ticket()
        # 万が一重複したら再生成
        while token in tickets:
            token = generate_ticket()

        tickets[token] = {"used": False, "issued_by": interaction.user.id}
        temp_channels[channel_id_str]["tickets"] = tickets
        save_temp_channels()

        await interaction.followup.send(
            f"🎟️ **使い捨て招待チケットを発行しました！**\n\n"
            f"```\n{token}\n```\n"
            f"このコードを招待したい相手に渡してください。\n"
            f"相手は `/stage-ticket` コマンドで入力するだけで入室できます。\n"
            f"⚠️ このチケットは**1回限り**有効です。",
            ephemeral=True
        )

    @discord.ui.button(label="発行済みチケット一覧", style=discord.ButtonStyle.secondary, emoji="📋")
    async def btn_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        channel_id_str = str(self.channel.id)
        tickets: dict = temp_channels.get(channel_id_str, {}).get("tickets", {})

        if not tickets:
            await interaction.followup.send("ℹ️ 現在発行済みのチケットはありません。", ephemeral=True)
            return

        lines = ["**🎟️ 発行済みチケット一覧**\n"]
        for token, info in tickets.items():
            status = "✅ 使用済み" if info.get("used") else "🟡 未使用"
            lines.append(f"`{token}` — {status}")

        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @discord.ui.button(label="チケットを全削除", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def btn_clear(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        channel_id_str = str(self.channel.id)
        count = len(temp_channels.get(channel_id_str, {}).get("tickets", {}))
        temp_channels[channel_id_str]["tickets"] = {}
        save_temp_channels()
        await interaction.followup.send(f"🗑️ チケットを {count} 件すべて削除しました。", ephemeral=True)


class TicketJoinModal(discord.ui.Modal, title="チケットで入室"):
    token_field = discord.ui.TextInput(
        label="チケットコード（8文字）",
        style=discord.TextStyle.short,
        placeholder="例: AB3XY7KZ",
        required=True,
        max_length=10,
        min_length=6
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        token = self.token_field.value.strip().upper()
        guild = interaction.guild

        # このサーバーの一時ステージからトークンを探す
        matched_channel = None
        matched_ch_id_str = None
        for ch_id_str, data in temp_channels.items():
            if data.get("guild_id") != guild.id:
                continue
            tickets = data.get("tickets", {})
            if token in tickets:
                matched_ch_id_str = ch_id_str
                matched_channel = guild.get_channel(int(ch_id_str))
                ticket_info = tickets[token]
                break

        if not matched_channel or not isinstance(matched_channel, discord.StageChannel):
            await interaction.followup.send("❌ チケットが見つかりません。コードを確認してください。", ephemeral=True)
            return

        if ticket_info.get("used"):
            await interaction.followup.send("❌ このチケットは既に使用済みです。", ephemeral=True)
            return

        # 使用済みにマーク
        temp_channels[matched_ch_id_str]["tickets"][token]["used"] = True
        save_temp_channels()

        # 入室権限を付与
        overwrite = matched_channel.overwrites_for(interaction.user)
        overwrite.view_channel = True
        overwrite.connect = True
        overwrite.stream = True
        try:
            await matched_channel.set_permissions(interaction.user, overwrite=overwrite, reason="チケット認証成功")
            await interaction.followup.send(
                f"✅ チケット認証に成功しました！\n{matched_channel.mention} をクリックして入室してください。",
                ephemeral=True
            )
        except discord.HTTPException:
            await interaction.followup.send("❌ 権限の付与に失敗しました。", ephemeral=True)


# ==========================================
# 📺 同時配信告知連携
# ==========================================
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


class StreamAnnounceModal(discord.ui.Modal, title="同時配信の告知"):
    url_field = discord.ui.TextInput(
        label="配信URL",
        style=discord.TextStyle.short,
        placeholder="例: https://www.youtube.com/watch?v=xxxxx",
        required=True,
        max_length=200
    )
    comment_field = discord.ui.TextInput(
        label="コメント（空欄でも可）",
        style=discord.TextStyle.paragraph,
        placeholder="例: アーカイブも残します！チャット欄でも絡んでください🎉",
        required=False,
        max_length=300
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        url = self.url_field.value.strip()

        # URLの簡易バリデーション
        if not re.match(r"https?://", url):
            await interaction.followup.send("❌ URLは `https://` から始まる形式で入力してください。", ephemeral=True)
            return

        icon, platform_name = detect_platform(url)
        comment = self.comment_field.value.strip()
        channel = interaction.channel

        view = StreamAnnounceSendView(url=url, icon=icon, platform_name=platform_name,
                                      comment=comment, stage_channel=channel,
                                      host=interaction.user)
        preview_text = (
            f"**{icon} {platform_name} で同時配信中！**\n"
            f"URL: {url}\n"
            + (f"コメント: {comment}\n" if comment else "")
            + "\n送信先のチャンネルを選んでください👇"
        )
        await interaction.followup.send(preview_text, view=view, ephemeral=True)


class StreamAnnounceSendView(discord.ui.View):
    def __init__(self, url: str, icon: str, platform_name: str, comment: str,
                 stage_channel: discord.StageChannel, host: discord.Member):
        super().__init__(timeout=300)
        self.url = url
        self.icon = icon
        self.platform_name = platform_name
        self.comment = comment
        self.stage_channel = stage_channel
        self.host = host

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        channel_types=[discord.ChannelType.text],
        placeholder="📢 告知を送信するチャンネルを選択..."
    )
    async def select_channel(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        await interaction.response.defer(ephemeral=True)
        target_channel = interaction.guild.get_channel(select.values[0].id)
        if not target_channel:
            await interaction.followup.send("❌ チャンネルが見つかりません。", ephemeral=True)
            return

        embed = discord.Embed(
            title=f"{self.icon} {self.platform_name} で同時配信中！",
            url=self.url,
            description=(
                f"{self.host.mention} さんが **{self.stage_channel.name}** と同時配信しています！\n\n"
                + (f"💬 {self.comment}\n\n" if self.comment else "")
                + f"▶️ [配信はこちら]({self.url})"
            ),
            color=(
                discord.Color.red() if self.platform_name == "YouTube"
                else discord.Color.purple() if self.platform_name == "Twitch"
                else discord.Color.blurple()
            )
        )
        embed.set_author(name=self.host.display_name, icon_url=self.host.display_avatar.url)
        embed.set_footer(text=f"Discord ステージ: {self.stage_channel.name}")

        try:
            await target_channel.send(embed=embed)
            # ステージのチャットにも簡易通知
            try:
                await self.stage_channel.send(
                    f"📺 {self.icon} **{self.platform_name}** でも配信中です！ → {self.url}"
                )
            except discord.HTTPException:
                pass
            await interaction.edit_original_response(
                content=f"✅ {target_channel.mention} に配信告知を送信しました！", view=None
            )
        except discord.Forbidden:
            await interaction.followup.send("❌ そのチャンネルへの送信権限がありません。", ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send("❌ 送信中にエラーが発生しました。", ephemeral=True)


# ==========================================
# 🎉 リアクション送信（エフェメラル絵文字ピッカー）
# ==========================================

# クールダウン管理: {(channel_id, user_id): 最終送信時刻}
_reaction_cooldowns: dict = {}
REACTION_COOLDOWN_SECS = 30

REACTION_EMOJIS = [
    ("👋", "ハロー"),
    ("🎉", "いえーい"),
    ("👍", "いいね"),
    ("😂", "笑"),
    ("🔥", "熱い"),
    ("❤️", "ありがとう"),
    ("👏", "拍手"),
    ("😮", "おお"),
]


class ReactionPickerView(discord.ui.View):
    """自分だけに見えるエフェメラル絵文字ピッカー。ボタンを押すとメインチャットに投稿される。"""

    def __init__(self, stage_channel: discord.StageChannel):
        super().__init__(timeout=60)
        self.stage_channel = stage_channel
        for emoji, label in REACTION_EMOJIS:
            self.add_item(ReactionPickerButton(emoji=emoji, label=label, stage_channel=stage_channel))


class ReactionPickerButton(discord.ui.Button):
    def __init__(self, emoji: str, label: str, stage_channel: discord.StageChannel):
        super().__init__(
            style=discord.ButtonStyle.secondary,
            emoji=emoji,
            label=label,
        )
        self.stage_channel = stage_channel

    async def callback(self, interaction: discord.Interaction):
        import time
        await interaction.response.defer(ephemeral=True)

        # クールダウンチェック
        now = time.monotonic()
        key = (self.stage_channel.id, interaction.user.id)
        last = _reaction_cooldowns.get(key, 0)
        remaining = REACTION_COOLDOWN_SECS - (now - last)
        if remaining > 0:
            await interaction.followup.send(
                f"⏳ あと **{int(remaining)+1}秒** 待ってから送れます。",
                ephemeral=True
            )
            return

        _reaction_cooldowns[key] = now

        try:
            await self.stage_channel.send(
                f"# {self.emoji}\n-# {interaction.user.display_name} より"
            )
            await interaction.followup.send("✅ 送信しました！", ephemeral=True)
        except discord.HTTPException:
            await interaction.followup.send("❌ 送信に失敗しました。", ephemeral=True)


class ReactionTriggerView(discord.ui.View):
    """チャットに常駐する『リアクションを送る』ボタン1つだけのView。流れても再送できる。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="リアクションを送る",
        style=discord.ButtonStyle.success,
        emoji="🎉",
        custom_id="reaction_trigger_btn"
    )
    async def btn_reaction(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.StageChannel) or str(channel.id) not in temp_channels:
            await interaction.response.send_message("❌ このボタンは現在無効です。", ephemeral=True)
            return
        await interaction.response.send_message(
            "送りたい絵文字を選んでください👇\n*(この画面はあなたにしか見えていません)*",
            view=ReactionPickerView(channel),
            ephemeral=True
        )


# ==========================================
# パネル呼び出し用ボタン（チャットに常駐）
# ==========================================
class SummonPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="コントロールパネルを開く", style=discord.ButtonStyle.secondary, custom_id="summon_panel_btn", emoji="🎛️")
    async def btn_summon(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.StageChannel) or str(channel.id) not in temp_channels:
            await interaction.response.send_message("このボタンは現在無効です。", ephemeral=True)
            return
        channel_id_str = str(channel.id)
        is_owner = channel.permissions_for(interaction.user).manage_channels
        is_sub_admin = interaction.user.id in temp_channels.get(channel_id_str, {}).get("sub_admins", [])
        if not (is_owner or is_sub_admin):
            await interaction.response.send_message("⚠️ パネルを開けるのはステージ作成者またはサブ管理者のみです。", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎛️ ステージ・コントロールパネル",
            description="以下のボタンをクリックすると設定を変更できます。\n*(※このパネルはあなたにしか見えていません)*",
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed, view=StageConsoleView(), ephemeral=True)


class SummonAndReactionView(discord.ui.View):
    """ウェルカムメッセージに使う。コントロールパネルとリアクション送信を1つに統合。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="コントロールパネルを開く", style=discord.ButtonStyle.secondary, custom_id="summon_and_reaction_panel_btn", emoji="🎛️")
    async def btn_summon(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.StageChannel) or str(channel.id) not in temp_channels:
            await interaction.response.send_message("このボタンは現在無効です。", ephemeral=True)
            return
        channel_id_str = str(channel.id)
        is_owner = channel.permissions_for(interaction.user).manage_channels
        is_sub_admin = interaction.user.id in temp_channels.get(channel_id_str, {}).get("sub_admins", [])
        if not (is_owner or is_sub_admin):
            await interaction.response.send_message("⚠️ パネルを開けるのはステージ作成者またはサブ管理者のみです。", ephemeral=True)
            return
        embed = discord.Embed(
            title="🎛️ ステージ・コントロールパネル",
            description="以下のボタンをクリックすると設定を変更できます。\n*(※このパネルはあなたにしか見えていません)*",
            color=discord.Color.blurple()
        )
        await interaction.response.send_message(embed=embed, view=StageConsoleView(), ephemeral=True)

    @discord.ui.button(label="リアクションを送る", style=discord.ButtonStyle.success, custom_id="summon_and_reaction_react_btn", emoji="🎉")
    async def btn_reaction(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        if not isinstance(channel, discord.StageChannel) or str(channel.id) not in temp_channels:
            await interaction.response.send_message("❌ このボタンは現在無効です。", ephemeral=True)
            return
        await interaction.response.send_message(
            "送りたい絵文字を選んでください👇\n*(この画面はあなたにしか見えていません)*",
            view=ReactionPickerView(channel),
            ephemeral=True
        )


# ==========================================
# Bot イベント & コマンド
# ==========================================
async def setup_hook():
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
            try:
                await channel.delete(reason="再起動時クリーンアップ")
            except discord.HTTPException:
                pass
            temp_channels.pop(channel_id_str, None)
    save_temp_channels()


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
            "`/stage-help` — このヘルプを表示"
        ),
        inline=False
    )

    embed.set_footer(text="ステージ作成者のみパネルを操作できます。楽しいステージを！🎤")
    await interaction.response.send_message(embed=embed, ephemeral=True)


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
            view_channel=True, connect=True, manage_channels=True, move_members=True, mute_members=True, request_to_speak=True, stream=True
        ),
    }

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

    try:
        await member.move_to(stage_channel)
    except discord.HTTPException:
        pass

    try:
        await stage_channel.create_instance(topic=DEFAULT_TOPIC_TEMPLATE.format(username=member.display_name)[:120])
    except discord.HTTPException:
        pass

    try:
        await member.edit(suppress=False)
    except discord.HTTPException:
        pass

    try:
        await stage_channel.send(
            f"{member.mention} さん、一時ステージを作成しました！\n"
            "設定の変更やステージの終了は 🎛️ ボタンから、絵文字リアクションは 🎉 ボタンからどうぞ。",
            view=SummonAndReactionView()
        )
    except discord.HTTPException:
        pass

    return stage_channel


async def maybe_delete_temp_channel(channel: discord.abc.GuildChannel, force: bool = False) -> None:
    channel_id_str = str(channel.id)
    if channel_id_str not in temp_channels:
        return
    if not isinstance(channel, discord.StageChannel):
        return
    if not force and len(channel.members) > 0:
        return

    try:
        await channel.delete(reason="一時ステージの自動削除または終了ボタン")
    except discord.HTTPException:
        pass
    finally:
        temp_channels.pop(channel_id_str, None)
        save_temp_channels()


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


if __name__ == "__main__":
    bot.run(TOKEN)