"""
discord.ui のモーダル・ビュー定義まとめ。
オンボーディング（ルール確認→参加区分→学年選択）、招待承認、
チャンネル名変更、退出時の確認、リセット確認、設定パネルなど。
"""
import asyncio

import discord

from storage import (
    GRADE_CATEGORIES,
    HIDDEN_RETENTION_DAYS,
    RULES_TEXT,
    load_data,
    save_data,
    load_hidden_data,
    save_hidden_data,
    load_starred_data,
    save_starred_data,
)
from bot_core import bot, ADMIN_ID
from utils import (
    create_personal_channel,
    delete_favorites_category_if_empty,
    get_existing_channel,
    get_or_create_favorites_category,
    hide_channel_from_others,
    make_embed,
    make_privacy_embed,
    rebuild_favorites_index,
    restore_channel_permissions,
    update_channel_index,
)


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

class ChannelSettingsView(discord.ui.View):
    """全チャンネル共通のテンプレートView。
    on_ready で一度だけ bot.add_view() すれば、Bot再起動後も全パネルのボタンが機能し続ける。
    お気に入りは「押した本人のための個人ブックマーク」であり、チャンネルの所有者かどうかは無関係。
    そのため toggle_star は所有者チェックを行わず、押した本人の starred_channels に記録する。"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="⭐ このチャンネルをお気に入りに追加/解除", style=discord.ButtonStyle.secondary, custom_id="star_toggle")
    async def toggle_star(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_starred_data()
        user_id = str(interaction.user.id)
        channel_id = str(interaction.channel.id)
        entry = data.get(user_id, {"channels": [], "category_id": None, "index_channel_id": None})
        starred = entry.get("channels", [])

        if channel_id in starred:
            starred.remove(channel_id)
            entry["channels"] = starred
            data[user_id] = entry
            await delete_favorites_category_if_empty(interaction.guild, entry)
            if entry.get("index_channel_id"):
                await rebuild_favorites_index(interaction.guild, interaction.user, entry)
            save_starred_data(data)
            await interaction.response.send_message("⭐ お気に入りから解除しました。", ephemeral=True)
        else:
            starred.append(channel_id)
            entry["channels"] = starred
            entry = await get_or_create_favorites_category(interaction.guild, interaction.user, data)
            entry["channels"] = starred
            data[user_id] = entry
            await rebuild_favorites_index(interaction.guild, interaction.user, entry)
            save_starred_data(data)
            await interaction.response.send_message("⭐ お気に入りに登録しました。", ephemeral=True)
