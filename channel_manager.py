import discord
from discord.ext import commands, tasks
import os
import sys
import asyncio
import io
import time
import logging
import traceback
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

DATA_FILE = Path("channel_data.json")
HIDDEN_DATA_FILE = Path("hidden_channels.json")
ERROR_LOG_FILE = Path("bot_errors.log")

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

# 非表示チャンネル（/leave で「残して非表示」を選んだもの）を
# 自動削除するまでの保持日数。ここを変えるだけで運用ポリシーを調整できる。
HIDDEN_RETENTION_DAYS = 30

def load_data() -> dict:
    if not DATA_FILE.exists():
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_hidden_data() -> dict:
    """{channel_id(str): {"user_id": str, "hidden_at": iso8601}} を保持する"""
    if not HIDDEN_DATA_FILE.exists():
        return {}
    with open(HIDDEN_DATA_FILE, "r") as f:
        return json.load(f)

def save_hidden_data(data: dict):
    with open(HIDDEN_DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

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

GRADE_CATEGORIES = {
    "A21": "日報_A21",
    "A22": "日報_A22",
    "A23": "日報_A23",
    "A24": "日報_A24",
    "A25": "日報_A25",
    "A26": "日報_A26",
}

INDEX_CHANNEL_NAME = "📌チャンネル一覧"
# Discordの1カテゴリあたりのチャンネル上限は50。index分を1つ確保して49を上限とする。
MAX_CHANNELS_PER_CATEGORY = 49

RULES_TEXT = """・日報などは自分のチャンネルでお願いします　訪問・コメントは自由です。
・他人に見られてる意識をもち、不快になるような発言・表現は避けてください。
・自分以外のメンバーのチャンネルへの荒らし行為、またはそれに準ずるものは禁止です。
・このサーバーをbotなどの研究環境としての利用は禁止です。
・このサーバー内で起きた個人間のトラブルや問題行為に対して、管理者は責任を取らないものとします。
・ルールに反するような行為が見られる場合、対応処置をとります。
・下記【データの取り扱いについて】も含めて、メンバーロールを取得したことで同意したものとみなします。
・なにかあれば <@421526096048291840> に連絡、もしくはhttps://discord.com/channels/1438017912492392501/1438497518735593524 をご利用ください。"""


def make_privacy_embed() -> discord.Embed:
    """データの取り扱いについての説明embed。オンボーディング時と /privacy コマンドの両方で使う。"""
    embed = discord.Embed(title="🔒 データの取り扱いについて", color=0x57F287)
    embed.add_field(
        name="📌 保存する情報",
        value=(
            "・あなたのDiscordユーザーID\n"
            "・作成された個人チャンネルのID\n"
            "（メッセージ内容そのものはBot側のデータベースには保存されません。Discord上のチャンネルに残るのみです）"
        ),
        inline=False
    )
    embed.add_field(
        name="⏳ 保持期間",
        value=(
            "・通常利用中は上記の紐付けのみを保持します。\n"
            f"・`/leave` で「非表示にする」を選んだ場合、非表示化から**{HIDDEN_RETENTION_DAYS}日後**にチャンネルごと自動的に完全削除されます。\n"
            f"・`/leave` を使わない退出・キック・除名の場合も同じルールが適用されます（管理者のみ閲覧可能な状態で保持され、{HIDDEN_RETENTION_DAYS}日後に自動削除）。"
        ),
        inline=False
    )
    embed.add_field(
        name="👀 アクセスできる人",
        value="・通常時：本人／member・ex_memberロールを持つメンバー\n・非表示化後：管理者のみ",
        inline=False
    )
    embed.add_field(
        name="🧰 本人が使えるセルフサービスコマンド",
        value=(
            "`/mydata` — 自分の登録状況を確認\n"
            "`/export_my_channel` — 自分のチャンネルの投稿内容をファイルで受け取る\n"
            "`/delete_my_data` — 在籍したまま自分のチャンネルと登録データを削除\n"
            "`/privacy` — この説明をいつでも再表示"
        ),
        inline=False
    )
    embed.add_field(
        name="🛠️ 管理者による操作について",
        value="・`/reset` が管理者によって実行された場合、事前と事後にDMで通知が届きます。",
        inline=False
    )
    embed.set_footer(text="ご不明点があれば管理者までお気軽にご連絡ください。")
    return embed


# ---- Embed生成ヘルパー ----

def make_embed(step: int, title: str, description: str, color: int = 0x5865F2) -> discord.Embed:
    steps = ["ルール確認", "参加区分選択", "学年選択"]
    step_str = " → ".join(
        f"**✅ {s}**" if i + 1 < step else f"**👉 {s}**" if i + 1 == step else s
        for i, s in enumerate(steps)
    )
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=f"STEP {step}/3　{step_str}")
    return embed


# ---- STEP 1: ルール確認 ----

class RulesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ 規約とデータの取り扱いに同意します", style=discord.ButtonStyle.success)
    async def rules_confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = make_embed(
            step=2,
            title="👥 参加区分を選んでください",
            description="あなたの参加区分を選択してください。\n\n🎓 **在学生・既卒生**\nデジタルハリウッド大学の在学生または既卒生\n\n👤 **外部参加**\nそれ以外の方"
        )
        await interaction.response.send_message(embed=embed, view=RoleSelectView(), ephemeral=True)


# ---- STEP 2: 参加区分選択 ----

class RoleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🎓 在学生・既卒生", style=discord.ButtonStyle.primary, row=0)
    async def member_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        existing = await get_existing_channel(interaction.guild, interaction.user)
        if existing:
            # 権限復元などの処理は3秒制限に引っかかるリスクがあるため、先に応答を確定させる
            await interaction.response.send_message(
                embed=make_embed(
                    step=2,
                    title="✅ チャンネルが見つかりました！",
                    description=f"以前作成した個人チャンネル {existing.mention} を確認してください。\n\nこのチャンネルは3秒後に自動削除されます。",
                    color=0x57F287
                ),
                ephemeral=True
            )

            role = discord.utils.get(interaction.guild.roles, name="member")
            if role:
                await interaction.user.add_roles(role)

            await restore_channel_permissions(existing, interaction.guild, interaction.user)
            await existing.send(f"{interaction.user.mention} おかえりなさい！あなたのチャンネルはここです👋")
            await asyncio.sleep(3)
            await interaction.channel.delete()
            return

        role = discord.utils.get(interaction.guild.roles, name="member")
        if role:
            await interaction.user.add_roles(role)

        embed = make_embed(
            step=3,
            title="📅 学年を選んでください",
            description="あなたの学年を選択してください。"
        )
        await interaction.response.send_message(embed=embed, view=GradeSelectView(), ephemeral=True)

    @discord.ui.button(label="👤 外部参加", style=discord.ButtonStyle.secondary, row=0)
    async def ex_member_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # 1. まず最初に defer() を呼び出して3秒制限を回避する
        await interaction.response.defer(ephemeral=True)

        existing = await get_existing_channel(interaction.guild, interaction.user)
        if existing:
            role = discord.utils.get(interaction.guild.roles, name="ex_member")
            if role:
                await interaction.user.add_roles(role)

            await restore_channel_permissions(existing, interaction.guild, interaction.user)
            await existing.send(f"{interaction.user.mention} おかえりなさい！あなたのチャンネルはここです👋")
            
            # 2. defer した後は interaction.response.send_message ではなく interaction.followup.send を使う
            await interaction.followup.send(
                embed=make_embed(
                    step=2,
                    title="✅ チャンネルが見つかりました！",
                    description=f"以前作成した個人チャンネル {existing.mention} を確認してください。\n\nこのチャンネルは3秒後に自動削除されます。",
                    color=0x57F287
                ),
                ephemeral=True
            )
            await asyncio.sleep(3)
            await interaction.channel.delete()
            return

        role = discord.utils.get(interaction.guild.roles, name="ex_member")
        if role:
            await interaction.user.add_roles(role)

        # 時間のかかるチャンネル作成処理
        await create_personal_channel(interaction.guild, interaction.user, "日報_外部参加")
        personal_channel = await get_existing_channel(interaction.guild, interaction.user)

        embed = make_embed(
            step=3,
            title="✅ 完了！",
            description=f"個人チャンネルを作成しました！\n{personal_channel.mention} から始めましょう🎉\n\nこのチャンネルは3秒後に自動削除されます。",
            color=0x57F287
        )
        
        # 3. ここも interaction.followup.send に変更
        await interaction.followup.send(embed=embed, ephemeral=True)
        await asyncio.sleep(3)
        await interaction.channel.delete()

    @discord.ui.button(label="🔍 すでにチャンネルを持っている", style=discord.ButtonStyle.success, row=1)
    async def already_have_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        existing = await get_existing_channel(interaction.guild, interaction.user)
        if existing:
            member_role = discord.utils.get(interaction.guild.roles, name="member")
            ex_member_role = discord.utils.get(interaction.guild.roles, name="ex_member")

            await interaction.response.send_message(
                embed=make_embed(
                    step=2,
                    title="✅ チャンネルが見つかりました！",
                    description=f"以前作成した個人チャンネル {existing.mention} を確認してください。\n\nこのチャンネルは3秒後に自動削除されます。",
                    color=0x57F287
                ),
                ephemeral=True
            )

            if existing.category and existing.category.name == "日報_外部参加":
                if ex_member_role:
                    await interaction.user.add_roles(ex_member_role)
            else:
                if member_role:
                    await interaction.user.add_roles(member_role)

            await restore_channel_permissions(existing, interaction.guild, interaction.user)
            await existing.send(f"{interaction.user.mention} おかえりなさい！あなたのチャンネルはここです👋")
            await asyncio.sleep(3)
            await interaction.channel.delete()
        else:
            await interaction.response.send_message(
                "個人チャンネルが見つかりませんでした。参加区分を選んでチャンネルを作成してください。",
                ephemeral=True
            )

    @discord.ui.button(label="← 戻る", style=discord.ButtonStyle.danger, row=2)
    async def back_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        member_role = discord.utils.get(interaction.guild.roles, name="member")
        ex_member_role = discord.utils.get(interaction.guild.roles, name="ex_member")
        if member_role in interaction.user.roles:
            await interaction.user.remove_roles(member_role)
        if ex_member_role in interaction.user.roles:
            await interaction.user.remove_roles(ex_member_role)

        embed = make_embed(
            step=1,
            title="📋 利用規約",
            description=RULES_TEXT,
            color=0x5865F2
        )
        await interaction.response.send_message(embeds=[embed, make_privacy_embed()], view=RulesView(), ephemeral=True)


# ---- STEP 3: 学年選択 ----

class GradeSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        grades_row0 = ["A21", "A22", "A23"]
        grades_row1 = ["A24", "A25", "A26"]
        for grade in grades_row0:
            self.add_item(GradeButton(grade=grade, category_name=GRADE_CATEGORIES[grade], row=0))
        for grade in grades_row1:
            self.add_item(GradeButton(grade=grade, category_name=GRADE_CATEGORIES[grade], row=1))
        self.add_item(BackToRoleButton())


class GradeButton(discord.ui.Button):
    def __init__(self, grade, category_name, row):
        super().__init__(label=grade, style=discord.ButtonStyle.primary, row=row)
        self.category_name = category_name

    async def callback(self, interaction: discord.Interaction):
        existing = await get_existing_channel(interaction.guild, interaction.user)
        if existing:
            await interaction.response.send_message(
                f"すでに個人チャンネルが作成されています：{existing.mention}",
                ephemeral=True
            )
            return

        # create_personal_channel は複数のDiscord API呼び出しを含み3秒を超えることがあるため、
        # 先に defer() でインタラクションを確定させてからタイムアウトを回避する
        await interaction.response.defer()

        await create_personal_channel(interaction.guild, interaction.user, self.category_name)
        personal_channel = await get_existing_channel(interaction.guild, interaction.user)

        embed = make_embed(
            step=3,
            title="✅ 完了！",
            description=f"個人チャンネルを作成しました！\n{personal_channel.mention} から始めましょう🎉\n\nこのチャンネルは3秒後に自動削除されます。",
            color=0x57F287
        )
        await interaction.edit_original_response(embed=embed, view=None)
        await asyncio.sleep(3)
        await interaction.channel.delete()


class BackToRoleButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="← 戻る", style=discord.ButtonStyle.danger, row=2)

    async def callback(self, interaction: discord.Interaction):
        member_role = discord.utils.get(interaction.guild.roles, name="member")
        if member_role in interaction.user.roles:
            await interaction.user.remove_roles(member_role)

        embed = make_embed(
            step=2,
            title="👥 参加区分を選んでください",
            description="あなたの参加区分を選択してください。\n\n🎓 **在学生・既卒生**\nデジタルハリウッド大学の在学生または既卒生\n\n👤 **外部参加**\nそれ以外の方"
        )
        await interaction.response.edit_message(embed=embed, view=RoleSelectView())


# ---- サーバー参加時に一時チャンネル生成 ----

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


# ---- /reset コマンド（管理者用） ----

class ResetConfirmView(discord.ui.View):
    def __init__(self, target_member, existing_channel):
        super().__init__(timeout=30)
        self.target_member = target_member
        self.existing_channel = existing_channel

    @discord.ui.button(label="✅ 実行する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="🔄 リセット処理を開始します…", view=None)

        # 実行前に本人へ事前通知（届かなくても処理は続行する）
        try:
            channel_note = f"（対象チャンネル：{self.existing_channel.name}）" if self.existing_channel else ""
            await self.target_member.send(
                f"⚠️ 管理者によってあなたのチャンネルとロールがリセットされます{channel_note}。\n"
                "内容を保存したい場合は、通常はチャンネル削除前に `/export_my_channel` で受け取れますが、"
                "今回は管理者操作のため、必要であれば管理者に直接ご相談ください。"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass

        await perform_reset(interaction, self.target_member, self.existing_channel)

    @discord.ui.button(label="❌ キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="リセットをキャンセルしました。", view=None)


async def perform_reset(interaction: discord.Interaction, member: discord.Member, existing):
    guild = interaction.guild

    member_role = discord.utils.get(guild.roles, name="member")
    ex_member_role = discord.utils.get(guild.roles, name="ex_member")
    if member_role in member.roles:
        await member.remove_roles(member_role)
    if ex_member_role in member.roles:
        await member.remove_roles(ex_member_role)

    if existing:
        category = existing.category
        await existing.delete(reason="管理者によるリセット")
        if category:
            try:
                await update_channel_index(guild, category)
            except Exception:
                pass

    # JSONの紐付け情報も忘れずに削除（残っていると次回誤検出の原因になる）
    data = load_data()
    if str(member.id) in data:
        del data[str(member.id)]
        save_data(data)

    welcome_category = discord.utils.get(guild.categories, name="ようこそ")
    if not welcome_category:
        welcome_category = await guild.create_category("ようこそ")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
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

    # 実行後に本人へ完了通知（届かなくても処理は続行する）
    try:
        await member.send(
            "✅ 管理者によるリセットが完了しました。チャンネルとロールが初期化されています。\n"
            "サーバー内に案内された手順に沿って進めれば、また個人チャンネルを作成できます。"
        )
    except (discord.Forbidden, discord.HTTPException):
        pass

    try:
        await interaction.followup.send(f"✅ {member.display_name} のリセットが完了しました。", ephemeral=True)
    except discord.NotFound:
        pass


@bot.tree.command(name="reset", description="メンバーのロールと個人チャンネルをリセットします（管理者用）")
async def reset(interaction: discord.Interaction, member: discord.Member):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return

    existing = await get_existing_channel(interaction.guild, member)

    if existing:
        content = (
            f"⚠️ **{member.display_name}** のリセットを実行します。\n"
            f"削除対象チャンネル：{existing.mention}\n\n"
            "このチャンネルは削除され、内容は復元できません。本当に実行しますか？"
        )
    else:
        content = (
            f"⚠️ **{member.display_name}** のリセットを実行します。\n"
            "（紐付けられたチャンネルは見つかりませんでした）\n\n"
            "本当に実行しますか？"
        )

    await interaction.response.send_message(
        content,
        view=ResetConfirmView(target_member=member, existing_channel=existing),
        ephemeral=True
    )


# ---- /sync_channels コマンド（管理者用） ----

@bot.tree.command(name="sync_channels", description="既存メンバーの個人チャンネルを一括で紐付けます（管理者用）")
@discord.app_commands.describe(notify="対象チャンネルに同期完了の一言通知を投稿するか（デフォルト：しない）")
async def sync_channels(interaction: discord.Interaction, notify: bool = False):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    matched = []
    unmatched = []

    async for member in guild.fetch_members(limit=None):
        if member.bot:
            continue
        channel = await get_existing_channel(guild, member)
        if channel:
            await restore_channel_permissions(channel, guild, member)
            matched.append(f"{member.display_name} → {channel.mention}")
            if notify:
                try:
                    await channel.send("🔄 権限情報が管理者操作により同期されました。")
                except Exception:
                    pass
        else:
            unmatched.append(member.display_name)

    result = f"✅ 紐付け成功（管理権限も付与済み）：{len(matched)}人\n❌ 未検出：{len(unmatched)}人\n"
    if unmatched:
        result += "\n**未検出のメンバー：**\n" + "\n".join(unmatched[:30])
        if len(unmatched) > 30:
            result += f"\n...ほか{len(unmatched) - 30}人"

    # Discordのメッセージ長制限対策
    if len(result) > 1900:
        result = result[:1900] + "\n...(省略)"

    await interaction.followup.send(result, ephemeral=True)


# ---- /link_channel コマンド（管理者用） ----

@bot.tree.command(name="link_channel", description="メンバーと個人チャンネルを手動で紐付けます（管理者用）")
async def link_channel(interaction: discord.Interaction, member: discord.Member, channel: discord.TextChannel):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return

    data = load_data()
    data[str(member.id)] = str(channel.id)
    save_data(data)

    await restore_channel_permissions(channel, interaction.guild, member)

    await interaction.response.send_message(
        f"✅ {member.display_name} と {channel.mention} を紐付けました（管理権限も付与済みです）。",
        ephemeral=True
    )


# ---- /check_channel コマンド（管理者用） ----

@bot.tree.command(name="check_channel", description="指定したメンバーの個人チャンネルの紐付け状況を確認します（管理者用）")
async def check_channel(interaction: discord.Interaction, member: discord.Member):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    data = load_data()
    user_id = str(member.id)

    # JSONに登録済みかどうかを直接チェック（フォールバック検索による自動保存を挟まず確認）
    if user_id in data:
        channel = interaction.guild.get_channel(int(data[user_id]))
        if channel:
            await interaction.followup.send(
                f"✅ {member.display_name} は {channel.mention} に紐付け済みです（登録済み）。",
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"⚠️ {member.display_name} はJSON上に記録がありますが、該当チャンネルが見つかりません（削除済みの可能性）。",
                ephemeral=True
            )
        return

    # JSONになければフォールバック検索を試す（実行すると見つかった場合はJSONに保存される）
    channel = await get_existing_channel(interaction.guild, member)
    if channel:
        await interaction.followup.send(
            f"🔍 {member.display_name} はJSON未登録でしたが、{channel.mention} が見つかったため紐付けました。",
            ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"❌ {member.display_name} の個人チャンネルは見つかりませんでした。",
            ephemeral=True
        )


# ---- /help コマンド ----

@bot.tree.command(name="help", description="このBotで使えるコマンド一覧を表示します")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 チャンネル管理Bot ヘルプ",
        description="このBotは、メンバーごとの個人チャンネル作成・管理をサポートします。",
        color=0x5865F2
    )
    embed.add_field(
        name="👤 メンバー用コマンド",
        value=(
            "`/rename` — 自分の個人チャンネル名を変更します\n"
            "`/invite` — 知り合いを招待するための申請フォームを開きます\n"
            "`/leave` — 個人チャンネルの扱いを選んでからサーバーを退出します\n"
            "`/find` — 名前の一部からメンバーの個人チャンネルを検索します\n"
            "`/mydata` — Botに登録されている自分のデータを確認します\n"
            "`/export_my_channel` — 自分のチャンネルの投稿内容をDMで受け取ります\n"
            "`/delete_my_data` — 在籍したまま自分のチャンネルと登録データを削除します\n"
            "`/privacy` — データの取り扱いについての説明を表示します\n"
            "`/help` — このヘルプを表示します"
        ),
        inline=False
    )

    if interaction.user.id == ADMIN_ID:
        embed.add_field(
            name="🛠️ 管理者用コマンド",
            value=(
                "`/reset` — メンバーのロールと個人チャンネルをリセットします\n"
                "`/sync_channels` — 既存メンバーの個人チャンネルを一括で紐付けます\n"
                "`/link_channel` — メンバーと個人チャンネルを手動で紐付けます\n"
                "`/check_channel` — 指定メンバーの紐付け状況を確認します\n"
                "`/update_index` — 全カテゴリのチャンネル一覧indexを再生成します\n"
                "`/cleanup_ghost_permissions` — 退出済みメンバーの権限が残っているチャンネルを検出・削除します\n"
                "`/audit_data` — 記録データと実際の状態の不整合を検出します\n"
                "`/botstatus` — Botの稼働状況（レイテンシ・タスク・データファイル）を確認します\n"
                "`/recent_errors` — 直近のエラーログを確認します"
            ),
            inline=False
        )

    embed.set_footer(text="サーバー参加時に案内される手順に沿って進めると、自動で個人チャンネルが作成されます。")

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---- /privacy コマンド ----

@bot.tree.command(name="privacy", description="Botが保存するデータの取り扱いについて説明します")
async def privacy(interaction: discord.Interaction):
    await interaction.response.send_message(embed=make_privacy_embed(), ephemeral=True)




# ---- /find コマンド ----

@bot.tree.command(name="find", description="名前の一部からメンバーの個人チャンネルを検索します")
async def find(interaction: discord.Interaction, name: str):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    data = load_data()
    query = name.lower()

    matches = []
    for user_id, channel_id in data.items():
        member = guild.get_member(int(user_id))
        if not member:
            continue
        if query in member.display_name.lower() or query in member.name.lower():
            channel = guild.get_channel(int(channel_id))
            if channel:
                matches.append(f"{member.mention} → {channel.mention}")

    if not matches:
        await interaction.followup.send(f"「{name}」に一致するメンバーは見つかりませんでした。", ephemeral=True)
        return

    result = "\n".join(matches[:20])
    if len(matches) > 20:
        result += f"\n...ほか{len(matches) - 20}件"

    await interaction.followup.send(f"🔍 検索結果：\n{result}", ephemeral=True)


# ---- /update_index コマンド（管理者用） ----

@bot.tree.command(name="update_index", description="全カテゴリのチャンネル一覧indexを再生成します（管理者用）")
async def update_index(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    base_names = list(GRADE_CATEGORIES.values()) + ["日報_外部参加"]
    updated = []
    for category in guild.categories:
        if is_personal_channel_category(category.name, base_names):
            await update_channel_index(guild, category)
            updated.append(category.name)

    await interaction.followup.send(
        f"✅ 以下のカテゴリのチャンネル一覧を更新しました：\n" + "\n".join(updated),
        ephemeral=True
    )


# ---- /cleanup_ghost_permissions コマンド（管理者用） ----

@bot.tree.command(
    name="cleanup_ghost_permissions",
    description="退出済みメンバーの権限設定が残っているチャンネルを検出・削除します（管理者用）"
)
@discord.app_commands.describe(dry_run="実際には削除せず対象一覧だけ確認する（デフォルト：確認のみ）")
async def cleanup_ghost_permissions(interaction: discord.Interaction, dry_run: bool = True):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    base_names = list(GRADE_CATEGORIES.values()) + ["日報_外部参加"]
    found = []

    for category in guild.categories:
        if not is_personal_channel_category(category.name, base_names):
            continue
        for channel in category.channels:
            if not isinstance(channel, discord.TextChannel):
                continue
            for target, _overwrite in list(channel.overwrites.items()):
                # discord.Object になっているのは、対応する Member/Role が解決できなかった
                # ケース（退出済みメンバー、または削除済みロール）。
                # ロールはこのBotではmember/ex_memberの2つしか使わないため、ここに出てくるのは
                # 実質的にほぼ「退出済みメンバーの個人権限（旧オーナー権限など）」のゴースト。
                if isinstance(target, discord.Object):
                    found.append((channel, target.id))
                    if not dry_run:
                        try:
                            await channel.set_permissions(target, overwrite=None)
                        except Exception:
                            pass

    if not found:
        await interaction.followup.send("✅ ゴースト権限は見つかりませんでした。", ephemeral=True)
        return

    lines = [f"・{c.mention} （残っていたID: {gid}）" for c, gid in found[:30]]
    header = f"{'🔍 検出のみ（dry_run）' if dry_run else '🧹 削除しました'}：{len(found)}件\n\n"
    result = header + "\n".join(lines)
    if len(found) > 30:
        result += f"\n...ほか{len(found) - 30}件"
    if dry_run:
        result += "\n\n実際に削除するには `/cleanup_ghost_permissions dry_run:False` を実行してください。"

    if len(result) > 1900:
        result = result[:1900] + "\n...(省略)"

    await interaction.followup.send(result, ephemeral=True)


# ---- /audit_data コマンド（管理者用） ----

@bot.tree.command(
    name="audit_data",
    description="Botの記録データ（JSON）と実際のDiscordの状態を突き合わせて不整合を検出します（管理者用）"
)
async def audit_data(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    data = load_data()
    hidden_data = load_hidden_data()
    base_names = list(GRADE_CATEGORIES.values()) + ["日報_外部参加"]

    issues = []

    # 1. channel_data.json：存在しないチャンネル／退出済みユーザーの記録が残っていないか
    for user_id, channel_id in data.items():
        channel = guild.get_channel(int(channel_id))
        member = guild.get_member(int(user_id))
        if not channel:
            issues.append(f"⚠️ <@{user_id}> の記録先チャンネル(ID:{channel_id})が存在しません")
        if not member:
            issues.append(f"⚠️ チャンネルID:{channel_id} の所有者(ID:{user_id})はサーバーに在籍していません")

    # 2. hidden_channels.json：存在しないチャンネル／記録上は非表示なのに実際は@everyoneに見える設定のもの
    for channel_id, info in hidden_data.items():
        channel = guild.get_channel(int(channel_id))
        if not channel:
            issues.append(f"⚠️ 非表示記録があるチャンネルID:{channel_id} が実際には存在しません（記録だけ残留）")
            continue
        everyone_ow = channel.overwrites_for(guild.default_role)
        if everyone_ow.read_messages is not False:
            issues.append(f"⚠️ {channel.mention} は非表示記録がありますが、実際は@everyoneから見える設定のままです")

    # 3. 実際に存在する個人チャンネルのうち、channel_data.jsonに未登録のもの
    linked_channel_ids = set(data.values())
    for category in guild.categories:
        if not is_personal_channel_category(category.name, base_names):
            continue
        for channel in category.channels:
            if channel.name == INDEX_CHANNEL_NAME or not isinstance(channel, discord.TextChannel):
                continue
            if str(channel.id) not in linked_channel_ids:
                issues.append(f"⚠️ {channel.mention} は channel_data.json に未登録です（`/check_channel` で確認できます）")

    if not issues:
        await interaction.followup.send("✅ 不整合は見つかりませんでした。", ephemeral=True)
        return

    result = f"🔍 {len(issues)}件の不整合を検出しました：\n\n" + "\n".join(issues[:30])
    if len(issues) > 30:
        result += f"\n...ほか{len(issues) - 30}件"
    if len(result) > 1900:
        result = result[:1900] + "\n...(省略)"

    await interaction.followup.send(result, ephemeral=True)


# ---- /invite コマンド ----

class InviteModal(discord.ui.Modal, title="招待申請"):
    name = discord.ui.TextInput(label="招待したい人の名前", placeholder="例：山田太郎")
    reason = discord.ui.TextInput(
        label="どんな人か教えてください",
        style=discord.TextStyle.paragraph,
        placeholder="例：同じゼミの友人で、デジハリに興味があります"
    )

    async def on_submit(self, interaction: discord.Interaction):
        admin = await bot.fetch_user(ADMIN_ID)
        embed = discord.Embed(title="📨 招待申請が届きました", color=0x5865F2)
        embed.add_field(name="申請者", value=interaction.user.mention, inline=False)
        embed.add_field(name="招待したい人", value=self.name.value, inline=False)
        embed.add_field(name="説明", value=self.reason.value, inline=False)

        view = ApprovalView(
            guild_id=interaction.guild.id,
            applicant_id=interaction.user.id,
            invitee_name=self.name.value
        )
        await admin.send(embed=embed, view=view)
        await interaction.response.send_message("申請を送信しました！承認されたら招待リンクをDMでお送りします。", ephemeral=True)


class ApprovalView(discord.ui.View):
    def __init__(self, guild_id, applicant_id, invitee_name):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.applicant_id = applicant_id
        self.invitee_name = invitee_name

    @discord.ui.button(label="✅ 承認", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = bot.get_guild(self.guild_id)
        channel = guild.text_channels[0]
        invite = await channel.create_invite(max_uses=1, max_age=86400, unique=True)

        applicant = await bot.fetch_user(self.applicant_id)
        await applicant.send(
            f"✅ 招待申請が承認されました！\n**{self.invitee_name}** さんに以下のリンクを送ってください。\n\n{invite.url}\n\n※このリンクは1回限り・24時間有効です。"
        )
        await interaction.response.edit_message(content=f"✅ 承認しました（{self.invitee_name}）", view=None)

    @discord.ui.button(label="❌ 却下", style=discord.ButtonStyle.danger)
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        applicant = await bot.fetch_user(self.applicant_id)
        await applicant.send(f"❌ 招待申請が却下されました。（{self.invitee_name}）")
        await interaction.response.edit_message(content=f"❌ 却下しました（{self.invitee_name}）", view=None)


@bot.tree.command(name="invite", description="招待申請フォームを開きます")
async def invite(interaction: discord.Interaction):
    await interaction.response.send_modal(InviteModal())

# ---- /rename コマンド ----

class RenameModal(discord.ui.Modal, title="チャンネル名変更"):
    new_name = discord.ui.TextInput(
        label="新しいチャンネル名",
        placeholder="例：my-diary",
        min_length=1,
        max_length=100
    )

    async def on_submit(self, interaction: discord.Interaction):
        existing = await get_existing_channel(interaction.guild, interaction.user)
        if not existing:
            await interaction.response.send_message(
                "個人チャンネルが見つかりませんでした。",
                ephemeral=True
            )
            return

        new_name = self.new_name.value.lower().replace(" ", "-")
        await existing.edit(name=new_name)
        if existing.category:
            await update_channel_index(interaction.guild, existing.category)
        await interaction.response.send_message(
            f"チャンネル名を **{new_name}** に変更しました！",
            ephemeral=True
        )


@bot.tree.command(name="rename", description="自分の個人チャンネル名を変更します")
async def rename(interaction: discord.Interaction):
    await interaction.response.send_modal(RenameModal())


# ---- /leave コマンド ----

class LeaveConfirmView(discord.ui.View):
    def __init__(self, channel):
        super().__init__(timeout=60)
        self.channel = channel

    @discord.ui.button(label="🗑️ 削除してから退出", style=discord.ButtonStyle.danger)
    async def delete_and_leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="10秒後に個人チャンネルを削除してサーバーからキックします。\nキャンセルする場合は下のボタンを押してください。",
            view=KickCancelView(member=interaction.user, guild=interaction.guild, channel=self.channel)
        )

    @discord.ui.button(label="🚪 チャンネルを残して退出", style=discord.ButtonStyle.secondary)
    async def leave_only(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=f"{self.channel.mention} を、退出後に管理者以外の全員から非表示にしますか？",
            view=HideConfirmView(member=interaction.user, guild=interaction.guild, channel=self.channel)
        )


class HideConfirmView(discord.ui.View):
    def __init__(self, member, guild, channel):
        super().__init__(timeout=30)
        self.member = member
        self.guild = guild
        self.channel = channel

    @discord.ui.button(label="🙈 非表示にする", style=discord.ButtonStyle.danger)
    async def hide_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=(
                f"10秒後にチャンネルを非表示（管理者のみ閲覧可）にしてサーバーからキックします。\n"
                f"非表示にしてから{HIDDEN_RETENTION_DAYS}日後に、内容を含めて完全に自動削除されます。\n"
                "キャンセルする場合は下のボタンを押してください。"
            ),
            view=KickCancelView(member=self.member, guild=self.guild, channel=None, hide_channel=self.channel)
        )

    @discord.ui.button(label="👀 そのまま残す", style=discord.ButtonStyle.secondary)
    async def hide_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="10秒後にサーバーからキックします。\nキャンセルする場合は下のボタンを押してください。",
            view=KickCancelView(member=self.member, guild=self.guild, channel=None, hide_channel=None)
        )


class KickCancelView(discord.ui.View):
    def __init__(self, member, guild, channel, hide_channel=None):
        super().__init__(timeout=10)
        self.member = member
        self.guild = guild
        self.channel = channel
        self.hide_channel = hide_channel
        self.cancelled = False

    @discord.ui.button(label="❌ キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.cancelled = True
        self.stop()
        await interaction.response.edit_message(
            content="退出をキャンセルしました。",
            view=None
        )

    async def on_timeout(self):
        if not self.cancelled:
            try:
                if self.channel:
                    category = self.channel.category
                    await self.channel.delete(reason="退出に伴う個人チャンネル削除")
                    if category:
                        await update_channel_index(self.guild, category)
                elif self.hide_channel:
                    await hide_channel_from_others(self.hide_channel, self.guild, self.member.id)
                await self.guild.kick(self.member, reason="退出申請によるキック")
            except Exception:
                pass


async def hide_channel_from_others(channel, guild, user_id):
    """管理者（ADMIN_ID）以外の全ユーザー・ロールからチャンネルを見えなくし、
    非表示にした日時を記録する（HIDDEN_RETENTION_DAYS後に自動削除するため）"""
    # 既存の権限上書き（member/ex_memberロールや本人など）を全て削除
    for target in list(channel.overwrites.keys()):
        try:
            await channel.set_permissions(target, overwrite=None)
        except Exception:
            pass

    # @everyone を非表示に
    await channel.set_permissions(guild.default_role, read_messages=False, send_messages=False)

    # 管理者だけ見えるようにする
    admin_member = guild.get_member(ADMIN_ID)
    if admin_member:
        await channel.set_permissions(admin_member, read_messages=True, send_messages=True)

    # 保持期限管理用に非表示化した日時を記録
    hidden_data = load_hidden_data()
    hidden_data[str(channel.id)] = {
        "user_id": str(user_id),
        "hidden_at": datetime.now(timezone.utc).isoformat(),
    }
    save_hidden_data(hidden_data)

    # 非表示化により通常メンバーからは見えなくなるため、一覧indexからも外す
    if channel.category:
        try:
            await update_channel_index(guild, channel.category)
        except Exception:
            pass


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

@bot.tree.command(name="leave", description="サーバーを退出する前にチャンネルの処理を選択します")
async def leave(interaction: discord.Interaction):
    existing = await get_existing_channel(interaction.guild, interaction.user)

    if not existing:
        await interaction.response.send_message(
            "個人チャンネルは見つかりませんでした。そのままサーバーを退出してください。",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"あなたの個人チャンネル {existing.mention} をどうしますか？",
        view=LeaveConfirmView(channel=existing),
        ephemeral=True
    )


# ---- /mydata コマンド（本人用：自分のデータ確認） ----

@bot.tree.command(name="mydata", description="Botに登録されている自分のデータを確認します")
async def mydata(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    member = interaction.user
    user_id = str(member.id)

    data = load_data()
    hidden_data = load_hidden_data()

    embed = discord.Embed(title="🗂️ あなたのデータ", color=0x5865F2)

    if user_id in data:
        channel = guild.get_channel(int(data[user_id]))
        if channel:
            embed.add_field(name="個人チャンネル", value=channel.mention, inline=False)

            hidden_info = hidden_data.get(str(channel.id))
            if hidden_info:
                hidden_at = datetime.fromisoformat(hidden_info["hidden_at"])
                delete_at = hidden_at + timedelta(days=HIDDEN_RETENTION_DAYS)
                embed.add_field(
                    name="状態",
                    value=(
                        f"🙈 非表示中（管理者のみ閲覧可）\n"
                        f"非表示化日時：{hidden_at.strftime('%Y-%m-%d %H:%M')} UTC\n"
                        f"自動削除予定日：{delete_at.strftime('%Y-%m-%d %H:%M')} UTC"
                    ),
                    inline=False
                )
            else:
                embed.add_field(name="状態", value="✅ 通常公開中（あなた・member/ex_memberロールが閲覧可能）", inline=False)
        else:
            embed.add_field(name="個人チャンネル", value="記録はありますが、チャンネル自体は既に削除されています。", inline=False)
    else:
        embed.add_field(name="個人チャンネル", value="登録されているチャンネルは見つかりませんでした。", inline=False)

    embed.add_field(
        name="保存されている情報",
        value="Botが保持しているのは、あなたのDiscordユーザーIDと個人チャンネルIDの紐付けのみです。",
        inline=False
    )
    embed.set_footer(text="自分のチャンネル内容の受け取りは /export_my_channel、記録の削除は /delete_my_data から行えます。")

    await interaction.followup.send(embed=embed, ephemeral=True)


# ---- /export_my_channel コマンド（本人用：自分のデータのエクスポート） ----

@bot.tree.command(name="export_my_channel", description="自分の個人チャンネルの投稿内容をDMでファイル受け取りします")
async def export_my_channel(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    existing = await get_existing_channel(interaction.guild, interaction.user)
    if not existing:
        await interaction.followup.send("個人チャンネルが見つかりませんでした。", ephemeral=True)
        return

    lines = []
    async for msg in existing.history(limit=None, oldest_first=True):
        timestamp = msg.created_at.strftime("%Y-%m-%d %H:%M:%S")
        author = msg.author.display_name
        content = msg.content or "(添付ファイルまたは埋め込みのみ)"
        lines.append(f"[{timestamp}] {author}: {content}")
        for attachment in msg.attachments:
            lines.append(f"    添付: {attachment.url}")

    if not lines:
        await interaction.followup.send("チャンネルにメッセージが見つかりませんでした。", ephemeral=True)
        return

    text_data = "\n".join(lines)
    buffer = io.BytesIO(text_data.encode("utf-8"))
    filename = f"{existing.name}_export.txt"

    try:
        await interaction.user.send(
            content=f"📦 {existing.mention} のエクスポートです。",
            file=discord.File(fp=buffer, filename=filename)
        )
        await interaction.followup.send("DMにエクスポートを送信しました。", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send(
            "DMを送信できませんでした。DMを受け取れるよう設定を確認してから、もう一度お試しください。",
            ephemeral=True
        )


# ---- /delete_my_data コマンド（本人用：在籍したまま自分のデータを削除） ----

class DeleteMyDataConfirmView(discord.ui.View):
    def __init__(self, channel):
        super().__init__(timeout=30)
        self.channel = channel

    @discord.ui.button(label="🗑️ 完全に削除する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        member = interaction.user

        category = self.channel.category
        try:
            await self.channel.delete(reason="本人による自己データ削除")
            if category:
                await update_channel_index(guild, category)
        except Exception:
            pass

        data = load_data()
        user_id = str(member.id)
        if user_id in data:
            del data[user_id]
            save_data(data)

        hidden_data = load_hidden_data()
        channel_id = str(self.channel.id)
        if channel_id in hidden_data:
            del hidden_data[channel_id]
            save_hidden_data(hidden_data)

        member_role = discord.utils.get(guild.roles, name="member")
        ex_member_role = discord.utils.get(guild.roles, name="ex_member")
        if member_role in member.roles:
            await member.remove_roles(member_role)
        if ex_member_role in member.roles:
            await member.remove_roles(ex_member_role)

        await interaction.response.edit_message(
            content="✅ あなたのチャンネルと登録データを完全に削除しました。サーバー自体は継続して利用できます。",
            view=None
        )

    @discord.ui.button(label="❌ キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="削除をキャンセルしました。", view=None)


@bot.tree.command(name="delete_my_data", description="サーバーに残ったまま、自分の個人チャンネルと登録データを削除します")
async def delete_my_data(interaction: discord.Interaction):
    existing = await get_existing_channel(interaction.guild, interaction.user)
    if not existing:
        await interaction.response.send_message("削除対象のデータが見つかりませんでした。", ephemeral=True)
        return

    await interaction.response.send_message(
        (
            f"⚠️ {existing.mention} と、それに紐づく登録データを完全に削除します。\n"
            "この操作は取り消せません。事前に内容が必要な場合は `/export_my_channel` を先にお使いください。\n\n"
            "本当に削除しますか？"
        ),
        view=DeleteMyDataConfirmView(channel=existing),
        ephemeral=True
    )


# ---- ユーティリティ ----

async def get_existing_channel(guild, member):
    data = load_data()
    user_id = str(member.id)

    # jsonにチャンネルIDが記録されていればそれを優先
    if user_id in data:
        channel = guild.get_channel(int(data[user_id]))
        if channel:
            return channel
        else:
            # チャンネルが削除済みならjsonからも削除
            del data[user_id]
            save_data(data)

    # フォールバック検索（細分化カテゴリ「-2」「-3」なども対象）
    all_category_names = list(GRADE_CATEGORIES.values()) + ["日報_外部参加"]
    channel_name = member.name.lower().replace(" ", "-")

    target_categories = [
        c for c in guild.categories if is_personal_channel_category(c.name, all_category_names)
    ]

    # 1st pass: 「manage_channels権限を持っている」＝本当の所有者だけが持つ権限で判定する。
    # 単に channel.overwrites に含まれているだけ（例：管理者が/leaveの非表示機能で
    # 閲覧権限だけ付与されているケースなど）は所有者とは判定しない。
    for category in target_categories:
        for channel in category.channels:
            if channel.name == INDEX_CHANNEL_NAME:
                continue
            if channel.overwrites_for(member).manage_channels is True:
                data[user_id] = str(channel.id)
                save_data(data)
                return channel

    # 2nd pass: 上記で見つからなければ、チャンネル名の一致のみで判定（弱い手がかりなので最終手段）
    for category in target_categories:
        for channel in category.channels:
            if channel.name == INDEX_CHANNEL_NAME:
                continue
            if channel.name == channel_name:
                data[user_id] = str(channel.id)
                save_data(data)
                return channel

    return None


def is_personal_channel_category(category_name, base_names):
    """カテゴリ名が個人チャンネル用のベース名、または細分化された「-2」「-3」等かを判定"""
    for base in base_names:
        if category_name == base or category_name.startswith(base + "-"):
            return True
    return False


async def restore_channel_permissions(channel, guild, member):
    """非表示状態（/leaveで非表示にした後）から復帰したユーザーの個人チャンネルに、
    通常時の閲覧権限（@everyone非表示・member/ex_memberロール閲覧可・本人フル権限）を再設定する"""
    member_role = discord.utils.get(guild.roles, name="member")
    ex_member_role = discord.utils.get(guild.roles, name="ex_member")

    await channel.set_permissions(guild.default_role, read_messages=False, send_messages=False)
    if member_role:
        await channel.set_permissions(member_role, read_messages=True, send_messages=True)
    if ex_member_role:
        await channel.set_permissions(ex_member_role, read_messages=True, send_messages=True)

    await channel.set_permissions(
        member,
        read_messages=True,
        send_messages=True,
        manage_messages=True,
        manage_channels=True,
    )

    # 非表示（自動削除待ち）だったチャンネルが復帰した場合は、
    # 保持期限の記録を解除して誤って自動削除されないようにする
    hidden_data = load_hidden_data()
    if str(channel.id) in hidden_data:
        del hidden_data[str(channel.id)]
        save_hidden_data(hidden_data)
        # 非表示中はindexの一覧から除外されているため、復帰時に再掲載する
        if channel.category:
            try:
                await update_channel_index(guild, channel.category)
            except Exception:
                pass


async def get_or_create_available_category(guild, base_name):
    """base_nameのカテゴリに空きがあればそれを、上限（49ch）に達していれば
    「base_name-2」「base_name-3」...という細分化カテゴリを自動的に作成/使用する"""
    suffix = 1
    while True:
        name = base_name if suffix == 1 else f"{base_name}-{suffix}"
        category = discord.utils.get(guild.categories, name=name)
        if not category:
            category_overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
            }
            category = await guild.create_category(name, overwrites=category_overwrites)
            return category
        if len(category.channels) < MAX_CHANNELS_PER_CATEGORY:
            return category
        suffix += 1


async def update_channel_index(guild, category):
    """カテゴリ内の個人チャンネル一覧をindexチャンネルに書き出す（ピン留め・先頭固定）"""
    member_role = discord.utils.get(guild.roles, name="member")
    ex_member_role = discord.utils.get(guild.roles, name="ex_member")

    index_channel = discord.utils.get(category.channels, name=INDEX_CHANNEL_NAME)
    if not index_channel:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False, send_messages=False),
        }
        if member_role:
            overwrites[member_role] = discord.PermissionOverwrite(read_messages=True, send_messages=False)
        if ex_member_role:
            overwrites[ex_member_role] = discord.PermissionOverwrite(read_messages=True, send_messages=False)
        index_channel = await category.create_text_channel(INDEX_CHANNEL_NAME, overwrites=overwrites)

    try:
        await index_channel.edit(position=0)
    except Exception:
        pass

    hidden_data = load_hidden_data()
    data = load_data()
    # channel_id -> user_id の逆引きを作る（誰のチャンネルかを一覧に併記するため）
    owner_by_channel = {v: k for k, v in data.items()}

    candidate_channels = [
        c for c in category.channels
        if c.id != index_channel.id
        and isinstance(c, discord.TextChannel)
        and str(c.id) not in hidden_data
    ]

    def sort_key(c):
        user_id = owner_by_channel.get(str(c.id))
        member = guild.get_member(int(user_id)) if user_id else None
        # 所有者名が分かるものは表示名でソートし、不明なものは最後にまとめる
        return (0, member.display_name.lower()) if member else (1, c.name.lower())

    channels = sorted(candidate_channels, key=sort_key)

    lines = []
    for c in channels:
        user_id = owner_by_channel.get(str(c.id))
        member = guild.get_member(int(user_id)) if user_id else None
        if member:
            lines.append(f"・{c.mention} — **{member.display_name}**")
        else:
            lines.append(f"・{c.mention} — （所有者不明。チャンネル名が変更されている場合は `/find` や `/check_channel` で確認できます）")

    header = f"📌 **{category.name} のチャンネル一覧**（{len(channels)}件）\n\n"

    chunks = []
    current = header
    for line in lines:
        if len(current) + len(line) + 1 > 1900:
            chunks.append(current)
            current = ""
        current += line + "\n"
    chunks.append(current)

    # 既存のBot投稿メッセージを削除してから最新版を投稿し直す
    async for msg in index_channel.history(limit=50):
        if msg.author == guild.me:
            try:
                await msg.delete()
            except Exception:
                pass

    for chunk in chunks:
        sent = await index_channel.send(chunk)
        try:
            await sent.pin()
        except Exception:
            pass


async def create_personal_channel(guild, member, category_name):
    category = await get_or_create_available_category(guild, category_name)

    member_role = discord.utils.get(guild.roles, name="member")
    ex_member_role = discord.utils.get(guild.roles, name="ex_member")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        member: discord.PermissionOverwrite(
            read_messages=True,
            send_messages=True,
        ),
    }
    if member_role:
        overwrites[member_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    if ex_member_role:
        overwrites[ex_member_role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)

    channel_name = member.name.lower().replace(" ", "-")
    channel = await guild.create_text_channel(channel_name, category=category, overwrites=overwrites)

    await channel.set_permissions(member,
        read_messages=True,
        send_messages=True,
        manage_messages=True,
        manage_channels=True,
    )

    # jsonに保存
    data = load_data()
    data[str(member.id)] = str(channel.id)
    save_data(data)

    # 新規作成のお知らせ
    welcome_embed = discord.Embed(
        title="🎉 あなたの個人チャンネルができました！",
        description=(
            f"{member.mention} ここがあなた専用のチャンネルです👋\n"
            "日報などは自由にここに投稿してください。\n\n"
            "💡 チャンネル名の変更は `/rename`\n"
            "💡 このサーバー・Botの使い方は `/help` で確認できます。"
        ),
        color=0x57F287
    )
    await channel.send(content=member.mention, embed=welcome_embed)

    # チャンネル一覧indexを更新
    await update_channel_index(guild, category)


# ---- 起動 ----

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


# ---- /botstatus コマンド（管理者用） ----

@bot.tree.command(name="botstatus", description="Botの稼働状況を確認します（管理者用）")
async def botstatus(interaction: discord.Interaction):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return

    uptime_seconds = int(time.time() - START_TIME)
    hours, remainder = divmod(uptime_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    embed = discord.Embed(title="🩺 Bot稼働状況", color=0x5865F2)
    embed.add_field(name="Discord APIレイテンシ", value=f"{round(bot.latency * 1000)}ms", inline=True)
    embed.add_field(name="起動からの経過時間", value=f"{hours}時間{minutes}分{seconds}秒", inline=True)
    embed.add_field(
        name="非表示チャンネル自動削除タスク",
        value="✅ 稼働中" if cleanup_expired_hidden_channels.is_running() else "❌ 停止中（要確認）",
        inline=False
    )

    try:
        load_data()
        data_status = "✅ 読み込み可能"
    except Exception as e:
        data_status = f"❌ エラー：{e}"
    try:
        load_hidden_data()
        hidden_status = "✅ 読み込み可能"
    except Exception as e:
        hidden_status = f"❌ エラー：{e}"

    embed.add_field(name="channel_data.json", value=data_status, inline=True)
    embed.add_field(name="hidden_channels.json", value=hidden_status, inline=True)
    embed.set_footer(text="直近のエラーは /recent_errors で確認できます。")

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---- /recent_errors コマンド（管理者用） ----

@bot.tree.command(name="recent_errors", description="直近のエラーログを表示します（管理者用）")
@discord.app_commands.describe(lines="表示する行数（デフォルト：40）")
async def recent_errors(interaction: discord.Interaction, lines: int = 40):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message("管理者のみ使用できます。", ephemeral=True)
        return

    if not ERROR_LOG_FILE.exists():
        await interaction.response.send_message("エラーログはまだありません（記録されたエラーがありません）。", ephemeral=True)
        return

    with open(ERROR_LOG_FILE, "r", encoding="utf-8") as f:
        all_lines = f.readlines()

    if not all_lines:
        await interaction.response.send_message("エラーログはまだありません（記録されたエラーがありません）。", ephemeral=True)
        return

    recent = "".join(all_lines[-lines:])
    preview = recent[-1900:] if len(recent) > 1900 else recent

    await interaction.response.send_message(
        content=f"直近{min(lines, len(all_lines))}行：\n```\n{preview}\n```",
        file=discord.File(fp=io.BytesIO(recent.encode("utf-8")), filename="recent_errors.log"),
        ephemeral=True
    )


bot.run(TOKEN)