"""TL の収集・日報ガチャ・投稿。本文は永続保存せず、配信状態のみ保存する。"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import re
import random
from urllib.parse import urlparse

import discord

from storage import atomic_write_json, load_hidden_data, INDEX_CHANNEL_NAME

SETTINGS_FILE = Path(__file__).with_name('timeline_settings.json')
STATE_FILE = Path('data/timeline_state.json')
LOCKS = {}
HISTORY_SLOTS = asyncio.Semaphore(4)


def load_settings():
    with SETTINGS_FILE.open(encoding='utf-8') as stream:
        config = json.load(stream)
    for key in ('category_name', 'channel_name'):
        if not isinstance(config.get(key), str) or not 1 <= len(config[key].strip()) <= 100:
            raise ValueError(f'timeline_settings.json: {key} の名前が不正です')
    for key in ('audience_role_names', 'source_category_names'):
        value = config.get(key)
        if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
            raise ValueError(f'timeline_settings.json: {key} は名前の配列が必要です')
        if not value:
            raise ValueError(f'timeline_settings.json: {key} は空にできません')
    config.setdefault('source_mode', 'listed')
    config.setdefault('timeline_lookback_hours', 24)
    config.setdefault('repeat_cooldown_hours', 168)
    config.setdefault('notification_mode', 'off')
    if config['notification_mode'] not in ('off', 'once'):
        raise ValueError('notification_mode は off または once を指定してください')
    if type(config['repeat_cooldown_hours']) is not int or not 1 <= config['repeat_cooldown_hours'] <= 8760:
        raise ValueError('repeat_cooldown_hours は1～8760時間を指定してください')
    if config['source_mode'] not in ('all', 'listed'):
        raise ValueError('source_mode は all または listed を指定してください')
    if type(config['timeline_lookback_hours']) is not int or not 1 <= config['timeline_lookback_hours'] <= 720:
        raise ValueError('timeline_lookback_hours は1～720時間を指定してください')
    for key, high in (('interval_hours', 720), ('lookback_hours', 720),
                      ('messages_per_channel', 1000), ('pickup_count', 10)):
        if type(config.get(key)) is not int or not 1 <= config[key] <= high:
            raise ValueError(f'timeline_settings.json: {key} は 1～{high} の整数が必要です')
    return config


def load_state():
    if not STATE_FILE.exists():
        return {}
    with STATE_FILE.open(encoding='utf-8') as stream:
        state = json.load(stream)
    if not isinstance(state, dict):
        raise ValueError('TL の配信状態が破損しています')
    return state


def save_state(state):
    atomic_write_json(STATE_FILE, state, prefix='.timeline-', ensure_ascii=False, indent=2)


def save_entry(guild_id, entry):
    state = load_state()
    state[str(guild_id)] = entry
    save_state(state)


def active_pick_history(entry, config, *, now=None):
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=config['repeat_cooldown_hours'])
    return {mid: timestamp for mid, timestamp in entry.get('recent_picks', {}).items()
            if datetime.fromisoformat(timestamp) > cutoff}


def cited_post_ids(embed):
    # 以前のカード・ガチャ表示のリンクにも対応する。
    values = [getattr(embed, 'url', None) or '', getattr(embed, 'description', None) or '']
    values.extend(field.value for field in getattr(embed, 'fields', []))
    return set(re.findall(r'https://discord\.com/channels/\d+/\d+/(\d+)', ' '.join(values)))


def remember_delivery(history, previous, bot_id, *, now=None):
    if previous.author.id != bot_id:
        return
    timestamp = getattr(previous, 'created_at', None) or now or datetime.now(timezone.utc)
    for embed in previous.embeds:
        if 'ccm-tl:' not in (embed.footer.text or ''):
            continue
        for mid in cited_post_ids(embed):
            stored = history.get(mid)
            if stored is None or datetime.fromisoformat(stored) < timestamp:
                history[mid] = timestamp.isoformat()


async def pickup_history(guild, entry, config, destination=None):
    history = active_pick_history(entry, config)
    # 更新直後は既存のTLから直近500配信分を取り込み、直前の投稿を再紹介しない。
    if 'recent_picks' not in entry and entry.get('channel_id'):
        destination = destination or guild.get_channel(entry['channel_id'])
        if isinstance(destination, discord.TextChannel):
            after = datetime.now(timezone.utc) - timedelta(hours=config['repeat_cooldown_hours'])
            async for previous in destination.history(limit=500, after=after):
                remember_delivery(history, previous, guild.me.id)
    return history


def unpicked(messages, history):
    return [message for message in messages if str(message.id) not in history]


def record_pick(guild_id, entry, history, message):
    # 配信が成功した投稿だけを記録し、途中失敗後も成功分を再紹介しない。
    history[str(message.id)] = datetime.now(timezone.utc).isoformat()
    entry['recent_picks'] = history
    save_entry(guild_id, entry)


def lock_for(guild_id):
    return LOCKS.setdefault(guild_id, asyncio.Lock())


def emoji_key(value):
    """カスタム絵文字は名前変更後もIDで一致させる。"""
    match = re.fullmatch(r'<a?:[^:]+:(\d+)>', str(value))
    return match.group(1) if match else str(value).replace('\ufe0f', '')


def reaction_score(message, emojis=()):
    wanted = {emoji_key(e) for e in emojis}
    return sum(max(0, r.count - int(r.me)) for r in message.reactions
               if not wanted or emoji_key(r.emoji) in wanted)


def can_read(channel, member):
    permissions = channel.permissions_for(member)
    return permissions.view_channel and permissions.read_message_history


def shared_audience(source, destination):
    """TLを見られるロール・メンバー全員が元チャンネルを読める場合だけ転載する。"""
    guild = source.guild
    roles = [guild.default_role, *guild.roles]
    if any(destination.permissions_for(role).view_channel and not can_read(source, role)
           for role in roles):
        return False
    return all(not destination.permissions_for(member).view_channel or can_read(source, member)
               for member in guild.members)


class TimelineBoundaryError(ValueError):
    """境界を安全に特定できない状態。Discordへ設定手順を返せる例外。"""


def resolve_timeline_category(guild, config, destination=None):
    """カテゴリはIDで特定し、名前での自動選択は一意な場合だけ許可する。"""
    categories = guild.categories
    by_id = {category.id: category for category in categories}
    entry = load_state().get(str(guild.id), {})
    pinned = entry.get('boundary_category_id')
    if pinned is not None:
        try:
            anchor = by_id.get(int(pinned))
        except (TypeError, ValueError):
            anchor = None
        if anchor is None:
            raise TimelineBoundaryError('設定されたTL境界カテゴリが見つかりません。/tl_boundary category:対象カテゴリ で指定し直してください。')
        return anchor
    anchor = getattr(destination, 'category', None)
    if anchor is None and entry.get('channel_id'):
        try:
            saved = guild.get_channel(int(entry['channel_id']))
        except (TypeError, ValueError):
            saved = None
        anchor = getattr(saved, 'category', None)
    if anchor is not None:
        if anchor.id in by_id:
            return by_id[anchor.id]
        raise TimelineBoundaryError('TLのカテゴリ情報が一致しません。/tl_boundary category:対象カテゴリ で境界を設定してください。')
    matches = [category for category in categories if category.name == config['category_name']]
    if len(matches) == 1:
        return matches[0]
    reason = '同名のTLカテゴリが複数あります。' if matches else 'TL境界カテゴリを特定できません。'
    raise TimelineBoundaryError(reason + '/tl_boundary category:対象カテゴリ で境界を設定してください。')


def category_boundary(guild, config, destination=None):
    anchor = resolve_timeline_category(guild, config, destination)
    categories = guild.categories
    for index, category in enumerate(categories):
        if category.id == anchor.id:
            return {lower.id for lower in categories[index + 1:]}
    raise TimelineBoundaryError('TL境界カテゴリの並び順を確認できません。/tl_boundary で設定を確認してください。')


def notification_mode(config, entry):
    return entry.get('notification_mode', config['notification_mode'])


def source_channels(guild, config, *, user=None, destination=None, channel=None):
    hidden = load_hidden_data()
    below = category_boundary(guild, config, destination)
    for source in guild.text_channels:
        if channel is not None and source.id != channel.id:
            continue
        category = source.category
        if (not category or source.name == INDEX_CHANNEL_NAME or str(source.id) in hidden
                or (destination and source.id == destination.id)):
            continue
        if category.id not in below:
            continue
        if config.get('source_mode', 'listed') == 'listed' and not any(
                category.name == base or category.name.startswith(base + '-')
                for base in config['source_category_names']):
            continue
        if not can_read(source, guild.me):
            continue
        if user is not None and not can_read(source, user):
            continue
        if destination is not None and not shared_audience(source, destination):
            continue
        yield source


async def collect(guild, config, *, days=None, user=None, destination=None, channel=None):
    after = datetime.now(timezone.utc) - timedelta(hours=config['lookback_hours'] if days is None else days * 24)
    sources = list(source_channels(guild, config, user=user, destination=destination, channel=channel))

    async def read(source):
        # 最新投稿すら対象期間より古いchはDiscordへの履歴リクエストを省く。
        last_id = getattr(source, 'last_message_id', None)
        if isinstance(last_id, int) and discord.utils.snowflake_time(last_id) < after:
            return [], 0
        async with HISTORY_SLOTS:
            found = []
            try:
                async for message in source.history(limit=config['messages_per_channel'], after=after):
                    if not message.author.bot and (message.content.strip() or message.attachments):
                        found.append(message)
            except (discord.Forbidden, discord.NotFound):
                return [], 1
            return found, 0

    # タスクも4件ずつ生成し、大規模サーバーでも待機タスクを増やさない。
    messages, skipped = [], 0
    for start in range(0, len(sources), 4):
        results = await asyncio.gather(*(read(source) for source in sources[start:start + 4]), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
            found, unavailable = result
            messages.extend(found)
            skipped += unavailable
    return messages, skipped


def ranked(messages, emojis=(), minimum=1):
    return sorted((m for m in messages if reaction_score(m, emojis) >= minimum),
                  key=lambda m: (reaction_score(m, emojis), m.created_at, m.id), reverse=True)


def excerpt(message):
    text = message.content.strip() or '（添付ファイルの投稿）'
    return discord.utils.escape_markdown(discord.utils.escape_mentions(' '.join(text.split())))[:220]


def draw_diaries(messages, count):
    """リアクション数に関係なく、収集した日報から重複なしで抽選する。"""
    return random.sample(messages, k=min(count, len(messages)))


def post_image(message):
    def valid(url):
        return isinstance(url, str) and urlparse(url).scheme == 'https'
    for attachment in message.attachments:
        content_type = getattr(attachment, 'content_type', '') or ''
        filename = getattr(attachment, 'filename', '').lower()
        if (content_type.startswith('image/') or filename.endswith(('.png', '.jpg', '.jpeg', '.webp', '.gif'))):
            if valid(attachment.url) and not (getattr(attachment, 'is_spoiler', lambda: False)()):
                return attachment.url
    # 投稿内リンクのDiscordプレビュー画像も引用できる。画像をダウンロードしない。
    for original in getattr(message, 'embeds', []):
        for media in (original.image, original.thumbnail):
            if valid(media.url):
                return media.url
    return None


def diary_embed(message, *, title='🎲 日報ガチャ', note=''):
    author = message.author
    name = discord.utils.escape_mentions(getattr(author, 'display_name', '投稿者'))[:256]
    body = discord.utils.escape_mentions(message.content.strip() or '（画像・添付ファイルの投稿）')
    # 本文の改行を保ち、短い抜粋だけだった旧表示から情報量を増やす。
    if len(body) > 1800:
        body = body[:1800] + '…\n（続きは元の日報へ）'
    embed = discord.Embed(title=title[:256], url=message.jump_url, color=0x2BBFA3,
                          description=f'{body}\n\n**[日報を読む →]({message.jump_url})**',
                          timestamp=message.created_at)
    embed.set_author(name=name, url=message.jump_url,
                     icon_url=str(author.display_avatar.url) if hasattr(author, 'display_avatar') else None)
    image = post_image(message)
    if image:
        embed.set_image(url=image)
    category = getattr(message.channel, 'category', None)
    category_name = getattr(category, 'name', '日報')
    channel_name = getattr(message.channel, 'name', '投稿チャンネル')
    embed.set_footer(text=f'{category_name} / #{channel_name}'[:300])
    if note:
        embed.add_field(name='今回のピックアップ', value=note[:1024], inline=False)
    return embed


def pick_per_category(messages):
    categories = {}
    for message in messages:
        category = message.channel.category
        if category:
            categories.setdefault(category.id, []).append(message)
    return {cid: random.choice(posts) for cid, posts in sorted(categories.items())}


def interval_minutes(config, entry):
    return entry.get('interval_minutes', config['interval_hours'] * 60)


async def ensure_timeline(guild, config):
    roles = [discord.utils.get(guild.roles, name=name) for name in config['audience_role_names']]
    if any(role is None for role in roles):
        raise ValueError('timeline_settings.json の audience_role_names にあるロールが見つかりません')
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                  guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                        read_message_history=True)}
    for role in roles:
        overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=False,
                                                      read_message_history=True)
    category = discord.utils.get(guild.categories, name=config['category_name'])
    if category is None:
        category = await guild.create_category(config['category_name'], overwrites=overwrites)
    # 既存カテゴリの権限を変更せず、TLチャンネルに専用の権限を設定する。
    channel = discord.utils.get(category.text_channels, name=config['channel_name'])
    if channel is None:
        channel = await category.create_text_channel(config['channel_name'], overwrites=overwrites)
    else:
        await channel.edit(overwrites=overwrites)
    return channel


async def publish(guild, *, force=False):
    async with lock_for(guild.id):
        config = load_settings()
        state = load_state()
        entry = state.get(str(guild.id))
        if not entry or not entry.get('enabled'):
            raise ValueError('先に /tl_setup を実行してください')
        now = datetime.now(timezone.utc)
        interval = timedelta(minutes=interval_minutes(config, entry))
        if not force and now - datetime.fromisoformat(entry['last_post']) < interval:
            return False
        destination = guild.get_channel(entry['channel_id'])
        if not isinstance(destination, discord.TextChannel):
            raise ValueError('TLチャンネルが見つかりません。/tl_setup を再実行してください')
        if not guild.chunked:
            await guild.chunk(cache=True)
        bucket = int(now.timestamp() // interval.total_seconds())
        marker = f'ccm-tl:{guild.id}:{bucket}'
        # カテゴリ単位で重複抑止し、途中送信失敗時も未送信カテゴリから再開する。
        sent = set()
        history = await pickup_history(guild, entry, config, destination)
        async for previous in destination.history(after=now - interval, limit=None):
            remember_delivery(history, previous, guild.me.id)
            if previous.author.id == guild.me.id:
                for original in previous.embeds:
                    footer = original.footer.text or ''
                    match = re.search(r'(ccm-tl:\d+:\d+(?::\d+)?)$', footer)
                    if match:
                        sent.add(match.group(1))
        history = active_pick_history({'recent_picks': history}, config)
        entry['recent_picks'] = history
        if marker in sent:  # 旧版の配信済みマーカーも引き継ぐ。
            entry['last_post'] = now.isoformat()
            save_entry(guild.id, entry)
            return False
        recent_hours = entry.get('timeline_lookback_hours', config['timeline_lookback_hours'])
        recent_config = dict(config, lookback_hours=recent_hours)
        messages, skipped = await collect(guild, recent_config, destination=destination)
        picks = pick_per_category(unpicked(messages, history))
        posted = False
        notify = notification_mode(config, entry) == 'once'
        already_notified = entry.get('notified_marker') == marker or any(
            old.startswith(marker + ':') for old in sent)
        for category_id, message in picks.items():
            category_marker = f'{marker}:{category_id}'
            if category_marker in sent:
                continue
            allowed = {c.id for c in source_channels(guild, config, destination=destination, channel=message.channel)}
            if message.channel.id not in allowed:
                continue
            # 履歴収集後の削除・編集・添付URLの変更を反映する。
            try:
                message = await message.channel.fetch_message(message.id)
            except (discord.NotFound, discord.Forbidden):
                continue
            allowed = {c.id for c in source_channels(guild, config, destination=destination, channel=message.channel)}
            if message.channel.id not in allowed or message.author.bot:
                continue
            embed = diary_embed(message, title=f'📰 {message.channel.category.name} の日報',
                                note=f'直近{recent_hours}時間から1件を抽選')
            embed.set_footer(text=f'{embed.footer.text} | {category_marker}')
            announce = notify and not already_notified
            if announce and not destination.permissions_for(guild.me).mention_everyone:
                raise ValueError('全体通知にはBotの「@everyone、@here、すべてのロールにメンション」権限が必要です。/tl_notify mode:off で通知を停止できます。')
            mentions = discord.AllowedMentions(everyone=True, users=False, roles=False, replied_user=False) if announce else discord.AllowedMentions.none()
            await destination.send(content='@everyone タイムラインを更新しました。' if announce else None,
                                   embed=embed, allowed_mentions=mentions)
            if announce:
                already_notified = True
                entry['notified_marker'] = marker
            record_pick(guild.id, entry, history, message)
            posted = True
        entry['last_post'] = now.isoformat()
        save_entry(guild.id, entry)
        return posted
