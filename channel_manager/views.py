"""
discord.ui のモーダル・ビュー定義まとめ。
オンボーディング（ルール確認→参加区分→学年選択）、招待承認、
チャンネル名変更、退出時の確認、リセット確認、旧パネルの廃止案内など。
"""
from onboarding import finish_onboarding

import discord

from storage import (
    GRADE_CATEGORIES,
    HIDDEN_RETENTION_DAYS,
    RULES_TEXT,
    load_data,
    save_data,
    load_hidden_data,
    save_hidden_data,
)
from bot_core import bot, ADMIN_ID, logger
from storage import load_invite_requests
from invitations import (
    InviteError, new_request, transition, decide, request_embed,
    issue_invite, deliver_invite, send_dm,
)
from utils import (
    create_personal_channel,
    get_existing_channel,
    hide_channel_from_others,
    make_embed,
    make_privacy_embed,
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
        await interaction.response.defer(ephemeral=True)
        existing = await get_existing_channel(interaction.guild, interaction.user)
        role = discord.utils.get(interaction.guild.roles, name="member")
        if role:
            await interaction.user.add_roles(role)
        if existing:
            await restore_channel_permissions(existing, interaction.guild, interaction.user)
            await finish_onboarding(interaction, existing, restored=True)
            return
        embed = make_embed(step=3, title="📅 学年を選んでください", description="あなたの学年を選択してください。")
        await interaction.followup.send(embed=embed, view=GradeSelectView(), ephemeral=True)

    @discord.ui.button(label="👤 外部参加", style=discord.ButtonStyle.secondary, row=0)
    async def ex_member_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        existing = await get_existing_channel(interaction.guild, interaction.user)
        role = discord.utils.get(interaction.guild.roles, name="ex_member")
        if role:
            await interaction.user.add_roles(role)
        if existing:
            await restore_channel_permissions(existing, interaction.guild, interaction.user)
            personal_channel = existing
        else:
            personal_channel = await create_personal_channel(interaction.guild, interaction.user, "日報_外部参加")
        await finish_onboarding(interaction, personal_channel, restored=existing is not None)

    @discord.ui.button(label="🔍 すでにチャンネルを持っている", style=discord.ButtonStyle.success, row=1)
    async def already_have_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        existing = await get_existing_channel(interaction.guild, interaction.user)
        if not existing:
            await interaction.followup.send(
                "個人チャンネルが見つかりませんでした。参加区分を選んでチャンネルを作成してください。", ephemeral=True
            )
            return
        external = existing.category and (
            existing.category.name == "日報_外部参加" or existing.category.name.startswith("日報_外部参加-")
        )
        role = discord.utils.get(interaction.guild.roles, name="ex_member" if external else "member")
        if role:
            await interaction.user.add_roles(role)
        await restore_channel_permissions(existing, interaction.guild, interaction.user)
        await finish_onboarding(interaction, existing, restored=True)

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
        await interaction.response.defer(ephemeral=True)
        existing = await get_existing_channel(interaction.guild, interaction.user)
        if existing:
            await restore_channel_permissions(existing, interaction.guild, interaction.user)
            personal_channel = existing
        else:
            personal_channel = await create_personal_channel(interaction.guild, interaction.user, self.category_name)
        await finish_onboarding(interaction, personal_channel, restored=existing is not None)


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
    name = discord.ui.TextInput(label="招待したい人の名前", placeholder="例：山田太郎（表示名でも構いません）", max_length=100, required=True)
    reason = discord.ui.TextInput(
        label="どんな人物か・あなたとの関係", style=discord.TextStyle.paragraph,
        placeholder="例：同じ大学の友人で、○○の活動を一緒にしています", max_length=500, required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        request_id = None
        try:
            if interaction.guild is None:
                raise InviteError("サーバー内で実行してください。")
            name, relationship = self.name.value.strip(), self.reason.value.strip()
            if not name or not relationship:
                raise InviteError("相手の名前と人物・関係性の説明は必須です。")
            request_id, record = new_request(interaction.guild.id, interaction.user.id, name, relationship)
            admin = await bot.fetch_user(ADMIN_ID)
            # 保存成功前は承認ボタンを公開しない。
            message = await admin.send(embed=request_embed(request_id, record), allowed_mentions=discord.AllowedMentions.none())
            record = transition(request_id, {"submitting"}, "pending", message_id=message.id)
            await message.edit(embed=request_embed(request_id, record), view=ApprovalView(request_id))
            await interaction.followup.send(
                f"申請を送信しました。承認後、相手に送る招待メッセージをあなたへDMします。\n申請ID: `{request_id}`\n"
                "届いたメッセージを相手へ転送してください。`/invite_status` で状態と承認済みリンクを確認できます。", ephemeral=True,
            )
        except InviteError as error:
            await interaction.followup.send(str(error), ephemeral=True)
        except Exception as error:
            logger.error("招待申請 %s で %s", request_id, type(error).__name__)
            await interaction.followup.send(
                "申請送信を完了できませんでした。招待は発行していません。`/invite_status` を確認し、管理者へ連絡してください。", ephemeral=True,
            )


class ApprovalView(discord.ui.View):
    def __init__(self, request_id):
        super().__init__(timeout=None)
        self.request_id = request_id
        self.approve.custom_id = f"ccm:invite:{request_id}:approve"
        self.reject.custom_id = f"ccm:invite:{request_id}:reject"

    async def decide_request(self, interaction, approved):
        if interaction.user.id != ADMIN_ID:
            await interaction.response.send_message("管理者のみ操作できます。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            record = decide(self.request_id, interaction.user.id, interaction.message.id, approved)
        except InviteError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except Exception as error:
            logger.error("招待審査 %s の保存失敗: %s", self.request_id, type(error).__name__)
            await interaction.followup.send("申請記録を確認できないため停止しました。招待は発行していません。", ephemeral=True)
            return

        try:
            embed = request_embed(self.request_id, record)
            embed.add_field(name="審査者", value=f"<@{record['reviewer_id']}>", inline=False)
            await interaction.message.edit(
                content="✅ 承認済み・発行処理中" if approved else "❌ 却下しました", embed=embed, view=None,
            )
        except Exception:
            logger.warning("招待 %s の審査画面更新失敗（判断結果は保存済み）", self.request_id)

        if approved:
            try:
                record = await issue_invite(self.request_id, record)
            except Exception as error:
                detail = str(error) if isinstance(error, InviteError) else type(error).__name__
                logger.error("招待発行 %s は停止: %s", self.request_id, detail)
                await interaction.followup.send(
                    f"申請 `{self.request_id}` の招待発行に失敗しました。リンクは配布せず、再発行を禁止しました。記録とDiscordの招待一覧を確認してください。", ephemeral=True,
                )
                return
            try:
                notified = await deliver_invite(self.request_id, record)
                result = "✅ 承認済み：申請者へ転送用の招待メッセージをDMしました"
                if not notified:
                    result = "✅ 承認・発行済み。申請者へのDMに失敗しました。/invite_status から同じリンクを取得できます"
            except Exception as error:
                logger.error("招待配送 %s は停止: %s", self.request_id, type(error).__name__)
                result = "✅ 承認・発行済み。配送を完了できませんでした。/invite_status で確認してください"
        else:
            result = "❌ 却下しました"
            try:
                await send_dm(record["applicant_id"], f"❌ 招待申請 `{self.request_id}`（{discord.utils.escape_markdown(record.get('invitee_name', record.get('username', '不明')))}）は却下されました。")
            except Exception:
                result += "（申請者への通知失敗）"
        # 元の説明と承認対象を残し、判断者・結果を追跡できるようにする。
        try:
            record = load_invite_requests()[self.request_id]
            embed = request_embed(self.request_id, record)
            embed.add_field(name="審査者", value=f"<@{record['reviewer_id']}>", inline=False)
            await interaction.message.edit(content=result, embed=embed, view=None)
        except Exception:
            logger.warning("招待 %s の審査画面更新失敗（判断結果は保存済み）", self.request_id)
        await interaction.followup.send(result, ephemeral=True)

    @discord.ui.button(label="✅ 承認", style=discord.ButtonStyle.success, custom_id="ccm:invite:approve")
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.decide_request(interaction, True)

    @discord.ui.button(label="❌ 却下", style=discord.ButtonStyle.danger, custom_id="ccm:invite:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.decide_request(interaction, False)

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

        await interaction.response.defer()
        category = self.channel.category
        try:
            await self.channel.delete(reason="本人による自己データ削除")
        except discord.NotFound:
            pass  # 既に削除されていれば登録データの削除を続行する
        except discord.HTTPException:
            await interaction.edit_original_response(
                content="❌ チャンネルを削除できませんでした。登録データは保持しています。時間を置くかBotの権限を確認して再試行してください。",
                view=None,
            )
            return

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

        if category:
            try:
                await update_channel_index(guild, category)
            except discord.HTTPException:
                pass  # 一覧更新の失敗を、削除の失敗として扱わない

        member_role = discord.utils.get(guild.roles, name="member")
        ex_member_role = discord.utils.get(guild.roles, name="ex_member")
        if member_role in member.roles:
            await member.remove_roles(member_role)
        if ex_member_role in member.roles:
            await member.remove_roles(ex_member_role)

        await interaction.edit_original_response(
            content="✅ あなたのチャンネルと登録データを完全に削除しました。サーバー自体は継続して利用できます。",
            view=None
        )

    @discord.ui.button(label="❌ キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="削除をキャンセルしました。", view=None)

class RetiredFavoritesView(discord.ui.View):
    """既存メッセージの旧ボタンだけを受け付ける。登録・保存処理は行わない。"""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="廃止された機能", custom_id="star_toggle")
    async def retired(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message("お気に入り登録機能は廃止されました。", ephemeral=True)
        try:
            await interaction.message.edit(content="お気に入り登録機能は廃止されました。", embed=None, view=None)
        except discord.HTTPException:
            logger.warning("旧お気に入りパネルを更新できませんでした。")
