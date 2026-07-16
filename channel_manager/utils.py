"""
チャンネル操作まわりの共通処理: 個人チャンネルの検索・作成・権限復元、
お気に入り機能、チャンネル一覧indexの更新、embed生成ヘルパーなど。
"""
from datetime import datetime, timezone

import discord

from storage import (
    load_data,
    save_data,
    load_hidden_data,
    save_hidden_data,
    GRADE_CATEGORIES,
    INDEX_CHANNEL_NAME,
    MAX_CHANNELS_PER_CATEGORY,
    HIDDEN_RETENTION_DAYS,
)
from bot_core import ADMIN_ID


def get_channel_owner_id(channel_id: int) -> int | None:
    """channel_data.json から、このチャンネルの所有者ユーザーIDを逆引きする"""
    data = load_data()
    for user_id_str, cid_str in data.items():
        if cid_str == str(channel_id):
            return int(user_id_str)
    return None

async def rebuild_favorites_index(guild: discord.Guild, member: discord.Member, entry: dict):
    """本人専用のお気に入り一覧チャンネルの内容を、現在の登録状況に合わせて書き直す"""
    index_channel = guild.get_channel(int(entry["index_channel_id"])) if entry.get("index_channel_id") else None
    if index_channel is None:
        return

    lines = []
    for cid in entry.get("channels", []):
        channel = guild.get_channel(int(cid))
        if channel:
            lines.append(f"・{channel.mention}")
        else:
            lines.append(f"・（削除済みチャンネル: {cid}）")

    content = "⭐ **あなたのお気に入りチャンネル一覧**\n\n" + ("\n".join(lines) if lines else "まだ登録がありません。")

    async for msg in index_channel.history(limit=10):
        if msg.author == guild.me:
            try:
                await msg.delete()
            except Exception:
                pass
    await index_channel.send(content)

async def get_or_create_favorites_category(guild: discord.Guild, member: discord.Member, data: dict) -> dict:
    """本人にだけ見えるお気に入りカテゴリ＋一覧チャンネルを、無ければ作成して entry を返す"""
    user_id = str(member.id)
    entry = data.get(user_id, {"channels": [], "category_id": None, "index_channel_id": None})

    category = guild.get_channel(int(entry["category_id"])) if entry.get("category_id") else None
    if category is None:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            member: discord.PermissionOverwrite(read_messages=True, send_messages=False),
        }
        category = await guild.create_category(f"⭐ {member.display_name}のお気に入り", overwrites=overwrites)
        entry["category_id"] = str(category.id)
        entry["index_channel_id"] = None  # カテゴリを作り直した場合は一覧チャンネルも作り直す

    index_channel = guild.get_channel(int(entry["index_channel_id"])) if entry.get("index_channel_id") else None
    if index_channel is None:
        index_channel = await guild.create_text_channel("一覧", category=category)
        entry["index_channel_id"] = str(index_channel.id)

    data[user_id] = entry
    return entry

async def delete_favorites_category_if_empty(guild: discord.Guild, entry: dict):
    """お気に入りが0件になったら、放置されたカテゴリを残さないよう削除する"""
    if entry.get("channels"):
        return
    index_channel = guild.get_channel(int(entry["index_channel_id"])) if entry.get("index_channel_id") else None
    category = guild.get_channel(int(entry["category_id"])) if entry.get("category_id") else None
    if index_channel:
        try:
            await index_channel.delete()
        except Exception:
            pass
    if category:
        try:
            await category.delete()
        except Exception:
            pass
    entry["category_id"] = None
    entry["index_channel_id"] = None

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

def make_embed(step: int, title: str, description: str, color: int = 0x5865F2) -> discord.Embed:
    steps = ["ルール確認", "参加区分選択", "学年選択"]
    step_str = " → ".join(
        f"**✅ {s}**" if i + 1 < step else f"**👉 {s}**" if i + 1 == step else s
        for i, s in enumerate(steps)
    )
    embed = discord.Embed(title=title, description=description, color=color)
    embed.set_footer(text=f"STEP {step}/3　{step_str}")
    return embed

def is_personal_channel_category(category_name, base_names):
    """カテゴリ名が個人チャンネル用のベース名、または細分化された「-2」「-3」等かを判定"""
    for base in base_names:
        if category_name == base or category_name.startswith(base + "-"):
            return True
    return False

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

def make_self_panel_embed() -> discord.Embed:
    """チャンネル設定パネル用のembed。ウェルカムメッセージ内、および /setup_selfpanel で共通利用する。"""
    embed = discord.Embed(
        title="🛠️ チャンネル設定パネル",
        description="このチャンネルをよく訪れる人は、下のボタンから自分だけのお気に入りに追加できます。",
        color=0x5865F2
    )
    return embed

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

    # チャンネル設定パネルを同時に設置(お気に入り登録などをボタンで完結できるようにする)
    await channel.send(embed=make_self_panel_embed(), view=ChannelSettingsView())

    # チャンネル一覧indexを更新
    await update_channel_index(guild, category)

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
