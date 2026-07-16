"""
スラッシュコマンド定義まとめ。
"""
import io
import time

import discord

from bot_core import bot, ADMIN_ID, START_TIME
from storage import (
    GRADE_CATEGORIES,
    INDEX_CHANNEL_NAME,
    HIDDEN_RETENTION_DAYS,
    ERROR_LOG_FILE,
    load_data,
    save_data,
    load_hidden_data,
)
from utils import (
    get_existing_channel,
    restore_channel_permissions,
    update_channel_index,
    is_personal_channel_category,
    make_privacy_embed,
    make_self_panel_embed,
)
from views import (
    ResetConfirmView,
    InviteModal,
    RenameModal,
    LeaveConfirmView,
    DeleteMyDataConfirmView,
    ChannelSettingsView,
)
from events import cleanup_expired_hidden_channels


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

@bot.tree.command(name="privacy", description="Botが保存するデータの取り扱いについて説明します")
async def privacy(interaction: discord.Interaction):
    await interaction.response.send_message(embed=make_privacy_embed(), ephemeral=True)

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

@bot.tree.command(name="invite", description="招待申請フォームを開きます")
async def invite(interaction: discord.Interaction):
    await interaction.response.send_modal(InviteModal())

@bot.tree.command(name="rename", description="自分の個人チャンネル名を変更します")
async def rename(interaction: discord.Interaction):
    await interaction.response.send_modal(RenameModal())

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

@bot.tree.command(name="setup_selfpanel", description="自分の個人チャンネルに設定パネルを設置します")
async def setup_selfpanel(interaction: discord.Interaction):
    data = load_data()
    channel_id_str = data.get(str(interaction.user.id))

    if channel_id_str is None:
        await interaction.response.send_message(
            "あなたに紐付けられた個人チャンネルが見つかりませんでした。", ephemeral=True
        )
        return

    channel = interaction.guild.get_channel(int(channel_id_str))
    if channel is None:
        await interaction.response.send_message(
            "登録されているチャンネルが見つかりませんでした（削除済みの可能性があります）。", ephemeral=True
        )
        return

    await channel.send(embed=make_self_panel_embed(), view=ChannelSettingsView())
    await interaction.response.send_message(f"✅ {channel.mention} に設定パネルを設置しました。", ephemeral=True)

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
