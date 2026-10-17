"""TL の収集・日報ガチャ・投稿。本文は永続保存せず、配信状態のみ保存する。"""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import random
import tempfile

import discord

from storage import load_hidden_data, INDEX_CHANNEL_NAME

SETTINGS_FILE = Path(__file__).with_name('timeline_settings.json')
STATE_FILE = Path('data/timeline_state.json')
LOCKS = {}


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
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=STATE_FILE.parent, prefix='.timeline-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
        os.replace(name, STATE_FILE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def save_entry(guild_id, entry):
    state = load_state()
    state[str(guild_id)] = entry
    save_state(state)


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


def source_channels(guild, config, *, user=None, destination=None):
    hidden = load_hidden_data()
    for channel in guild.text_channels:
        category = channel.category
        if (not category or channel.name == INDEX_CHANNEL_NAME or str(channel.id) in hidden
                or (destination and channel.id == destination.id)):
            continue
        if not any(category.name == base or category.name.startswith(base + '-')
                   for base in config['source_category_names']):
            continue
        if not can_read(channel, guild.me):
            continue
        if user is not None and not can_read(channel, user):
            continue
        if destination is not None and not shared_audience(channel, destination):
            continue
        yield channel


async def collect(guild, config, *, days=None, user=None, destination=None, channel=None):
    after = datetime.now(timezone.utc) - timedelta(hours=config['lookback_hours'] if days is None else days * 24)
    messages, skipped = [], 0
    for source in source_channels(guild, config, user=user, destination=destination):
        if channel is not None and source.id != channel.id:
            continue
        try:
            async for message in source.history(limit=config['messages_per_channel'], after=after):
                if not message.author.bot and (message.content.strip() or message.attachments):
                    messages.append(message)
        except (discord.Forbidden, discord.NotFound):
            skipped += 1
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


def diary_embed(messages, *, title='🎲 日報ガチャ', note=''):
    embed = discord.Embed(title=title, color=0x5865F2,
                          description=note or 'ランダムに選ばれた日報を読んでみよう！')
    for index, message in enumerate(messages, 1):
        embed.add_field(name=f'🎉 {index}件目の日報',
                        value=f'{excerpt(message)}\n[日報を読む]({message.jump_url})', inline=False)
    return embed


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
        interval = timedelta(hours=config['interval_hours'])
        if not force and now - datetime.fromisoformat(entry['last_post']) < interval:
            return False
        destination = guild.get_channel(entry['channel_id'])
        if not isinstance(destination, discord.TextChannel):
            raise ValueError('TLチャンネルが見つかりません。/tl_setup を再実行してください')
        if not guild.chunked:
            await guild.chunk(cache=True)
        bucket = int(now.timestamp() // interval.total_seconds())
        marker = f'ccm-tl:{guild.id}:{bucket}'
        # 再起動直前の送信成功・状態保存失敗でも、同じ期間の再投稿を抑える。
        async for previous in destination.history(limit=30):
            if previous.author.id == guild.me.id and any(e.footer.text == marker for e in previous.embeds):
                entry['last_post'] = now.isoformat()
                save_entry(guild.id, entry)
                return False
        messages, skipped = await collect(guild, config, destination=destination)
        if not messages:
            entry['last_post'] = now.isoformat()
            save_entry(guild.id, entry)
            return False
        picks = draw_diaries(messages, config['pickup_count'])
        allowed = {c.id for c in source_channels(guild, config, destination=destination)}
        if any(m.channel.id not in allowed for m in picks):
            raise ValueError('収集中に閲覧権限が変更されたため、TL投稿を中止しました')
        embed = diary_embed(picks, note=(
            f'直近{config["lookback_hours"]}時間の収集{len(messages)}件からランダムに紹介！\n'
            f'各ch最新{config["messages_per_channel"]}件まで・取得不可{skipped}ch'))
        embed.timestamp = now
        embed.set_footer(text=marker)
        await destination.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        entry['last_post'] = now.isoformat()
        save_entry(guild.id, entry)
        return True
