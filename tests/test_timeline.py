"""Discordへ接続せず、転載権限・配信状態・検索条件を検証。"""
import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import discord
import pytest
from test_regressions import modules, http_error  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)]


async def items(values):
    for value in values:
        yield value


def post(pid=1, content='開発が進みました', count=3, emoji='⭐'):
    message = NS(id=pid, content=content, attachments=[], embeds=[],
                 author=NS(id=7, bot=False, display_name='日報の投稿者',
                           display_avatar=NS(url='https://cdn.discordapp.com/avatars/7/avatar.png')),
                 reactions=[NS(emoji=emoji, count=count, me=True)],
                 created_at=datetime.now(timezone.utc),
                 channel=NS(id=10, name='diary', category=NS(id=1, name='日報_A26')),
                 jump_url=f'https://discord.com/channels/1/10/{pid}')
    message.channel.fetch_message = AsyncMock(return_value=message)
    return message



async def test_scores_custom_emoji_and_threshold(modules):
    t = importlib.import_module('timeline')
    message = post(emoji='<:star:123>')
    assert t.reaction_score(message, ['<:renamed:123>']) == 2
    assert t.ranked([message], ['👍']) == []
    assert t.ranked([message], ['<:star:123>'], 3) == []
    assert t.emoji_key('⭐️') == t.emoji_key('⭐')


async def test_search_excludes_private_hidden_and_outside_categories(modules, monkeypatch):
    t = importlib.import_module('timeline')
    guild = NS(id=1, me=NS(id=99))
    def channel(cid, category, readable=True):
        c = NS(id=cid, name='personal', category=NS(id=cid, name=category), guild=guild)
        c.permissions_for = lambda who: NS(view_channel=readable, read_message_history=True)
        c.history = Mock(return_value=items([post(cid)]))
        return c
    allowed = channel(10, '日報_A26-2')
    hidden = channel(11, '日報_A26')
    private = channel(12, '日報_A26', False)
    outside = channel(13, '管理者')
    guild.text_channels = [allowed, hidden, private, outside]
    guild.categories = [NS(id=5, name='タイムライン'), *(c.category for c in guild.text_channels)]
    monkeypatch.setattr(t, 'load_hidden_data', lambda: {'11': {}})
    config = t.load_settings(); config['source_mode'] = 'listed'
    results, skipped = await t.collect(guild, config, user=NS(id=7))
    assert [m.id for m in results] == [10]
    assert skipped == 0
    assert allowed.history.call_args.kwargs['limit'] == 200
    for c in (hidden, private, outside):
        c.history.assert_not_called()


async def test_audience_rejects_member_specific_denial(modules):
    t = importlib.import_module('timeline')
    role, member = NS(id=1), NS(id=7)
    guild = NS(default_role=role, roles=[], members=[member])
    destination = NS(permissions_for=lambda _: NS(view_channel=True))
    source = NS(guild=guild, permissions_for=lambda who: NS(view_channel=who.id != 7, read_message_history=True))
    assert not t.shared_audience(source, destination)


def publishing(t, monkeypatch, tmp_path):
    monkeypatch.setattr(t, 'STATE_FILE', tmp_path / 'state.json')
    destination = Mock(spec=discord.TextChannel)
    destination.id = 20
    destination.history = lambda **kw: items([])
    destination.send = AsyncMock()
    guild = NS(id=1, get_channel=lambda _: destination, me=NS(id=99), chunked=True)
    t.save_state({'1': {'enabled': True, 'channel_id': 20,
                       'last_post': (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}})
    monkeypatch.setattr(t, 'collect', AsyncMock(return_value=([post()], 0)))
    monkeypatch.setattr(t, 'source_channels', lambda *a, **kw: [NS(id=10)])
    return guild, destination


async def test_publish_updates_only_after_success_and_survives_restart(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    guild, dest = publishing(t, monkeypatch, tmp_path)
    before = t.load_state()
    dest.send.side_effect = http_error()
    with pytest.raises(discord.HTTPException):
        await t.publish(guild)
    assert t.load_state() == before
    dest.send.side_effect = None
    assert await t.publish(guild)
    assert not await t.publish(guild)
    embed = dest.send.call_args.kwargs['embed']
    assert len(embed) <= 6000
    assert '[日報を読む' in embed.description
    assert '日報_A26' in embed.title
    assert dest.send.call_args.kwargs['allowed_mentions'].everyone is False


async def test_collection_failure_keeps_delivery_due(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    guild, dest = publishing(t, monkeypatch, tmp_path)
    before = t.load_state()
    t.collect.side_effect = RuntimeError('Discord unavailable')
    with pytest.raises(RuntimeError):
        await t.publish(guild)
    assert t.load_state() == before
    dest.send.assert_not_awaited()


async def test_permissions_change_during_collection_stops_delivery(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    guild, dest = publishing(t, monkeypatch, tmp_path)
    monkeypatch.setattr(t, 'source_channels', lambda *a, **kw: [])
    assert not await t.publish(guild)
    dest.send.assert_not_awaited()


async def test_already_sent_marker_prevents_duplicate_after_state_write_failure(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    guild, dest = publishing(t, monkeypatch, tmp_path)
    bucket = int(datetime.now(timezone.utc).timestamp() // (24 * 3600))
    previous = NS(author=NS(id=99), embeds=[NS(footer=NS(text=f'ccm-tl:1:{bucket}'))])
    dest.history = lambda **kw: items([previous])
    assert not await t.publish(guild)
    t.collect.assert_not_awaited()
    dest.send.assert_not_awaited()


async def test_gacha_includes_zero_reaction_posts_and_draws_without_duplicates(modules, monkeypatch):
    t = importlib.import_module('timeline')
    messages = [post(i, count=0) for i in range(5)]
    assert t.draw_diaries([], 1) == []
    drawn = t.draw_diaries(messages, 10)
    assert len({m.id for m in drawn}) == 5
    assert {m.id for m in drawn} == {m.id for m in messages}
    # 乱数抽選へ元の候補を全て渡し、リアクション等で重み付けしない。
    sampler = Mock(return_value=[messages[-1]])
    monkeypatch.setattr(t.random, 'sample', sampler)
    assert t.draw_diaries(messages, 1) == [messages[-1]]
    sampler.assert_called_once_with(messages, k=1)
    assert not hasattr(t, 'ai_summary')


async def test_setup_requires_roles_before_creating_anything(modules):
    t = importlib.import_module('timeline')
    guild = NS(roles=[], create_category=AsyncMock())
    with pytest.raises(ValueError, match='ロール'):
        await t.ensure_timeline(guild, t.load_settings())
    guild.create_category.assert_not_awaited()


async def test_commands_register_admin_checks_and_private_search(modules, monkeypatch):
    t = importlib.import_module('timeline')
    commands = importlib.import_module('timeline_commands')
    assert commands.tl_setup.checks
    assert commands.tl_stop.checks
    assert commands.tl_post.checks
    monkeypatch.setattr(t, 'collect', AsyncMock(return_value=([post()], 0)))
    inter = NS(guild=NS(id=1), user=NS(id=7), response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
    monkeypatch.setattr(t, 'source_channels', lambda *a, **kw: [NS(id=10)])
    await commands.reaction_search.callback(inter, emoji='⭐', limit=1)
    assert inter.followup.send.call_args.kwargs['ephemeral'] is True
    assert t.collect.call_args.kwargs['user'] is inter.user


async def test_largest_configured_digest_fits_discord_embed_limits(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    guild, dest = publishing(t, monkeypatch, tmp_path)
    config = t.load_settings(); config.update(pickup_count=10)
    monkeypatch.setattr(t, 'load_settings', lambda: config)
    messages = [post(i, content='長い投稿' * 400) for i in range(200)]
    for message in messages:
        message.jump_url = 'https://discord.com/channels/' + '/'.join(['9' * 20] * 3)
    t.collect.return_value = (messages, 0)
    assert await t.publish(guild)
    embed = dest.send.call_args.kwargs['embed']
    assert len(embed) <= 6000
    assert all(len(f.value) <= 1024 for f in embed.fields)


async def test_manual_gacha_is_private_and_uses_user_read_permissions(modules, monkeypatch):
    t = importlib.import_module('timeline')
    commands = importlib.import_module('timeline_commands')
    monkeypatch.setattr(t, 'collect', AsyncMock(return_value=([post()], 0)))
    monkeypatch.setattr(t, 'source_channels', lambda *a, **kw: [NS(id=10)])
    inter = NS(guild=NS(id=1), user=NS(id=7), response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
    await commands.diary_gacha.callback(inter, days=7)
    assert inter.followup.send.call_args.kwargs['ephemeral'] is True
    assert t.collect.call_args.kwargs['user'] is inter.user
    assert t.collect.call_args.kwargs['days'] == 7
    assert '[日報を読む' in inter.followup.send.call_args.kwargs['embed'].description


async def test_manual_gacha_handles_empty_and_newly_private_sources(modules, monkeypatch):
    t = importlib.import_module('timeline')
    commands = importlib.import_module('timeline_commands')
    monkeypatch.setattr(t, 'collect', AsyncMock(return_value=([post()], 0)))
    monkeypatch.setattr(t, 'source_channels', lambda *a, **kw: [])
    inter = NS(guild=NS(id=1), user=NS(id=7), response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
    await commands.diary_gacha.callback(inter)
    assert '対象の日報がありません' in inter.followup.send.call_args.args[0]
    assert inter.followup.send.call_args.kwargs['ephemeral'] is True


async def test_old_settings_with_unused_ai_keys_still_load(modules, monkeypatch, tmp_path):
    import json
    t = importlib.import_module('timeline')
    config = t.load_settings()
    config.update(summary_messages=100, recommendation_emojis=[], minimum_reactions=1)
    path = tmp_path / 'old-settings.json'
    path.write_text(json.dumps(config), encoding='utf-8')
    monkeypatch.setattr(t, 'SETTINGS_FILE', path)
    assert t.load_settings()['pickup_count'] == 1


async def test_rich_card_has_author_avatar_image_and_readable_body(modules):
    t = importlib.import_module('timeline')
    message = post(content='## 今日の進捗\n\n開発が進みました。\n次はテストします。')
    spoiler = NS(content_type='image/png', filename='secret.png', url='https://cdn.discordapp.com/secret.png', is_spoiler=lambda: True)
    image = NS(content_type='image/jpeg', filename='diary.jpg', url='https://cdn.discordapp.com/diary.jpg', is_spoiler=lambda: False)
    message.attachments = [spoiler, image]
    embed = t.diary_embed(message)
    assert embed.author.name == message.author.display_name
    assert embed.author.icon_url == message.author.display_avatar.url
    assert embed.image.url == image.url
    assert '\n\n' in embed.description
    assert '次はテストします。' in embed.description
    assert embed.timestamp == message.created_at
    message.attachments = []
    original = discord.Embed(); original.set_image(url='https://example.com/post-image.png')
    message.embeds = [original]
    assert t.post_image(message) == 'https://example.com/post-image.png'
    message.embeds = []
    assert t.post_image(message) is None


async def test_one_post_per_category_and_partial_delivery_retry(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    guild, dest = publishing(t, monkeypatch, tmp_path)
    messages = [post(1), post(2), post(3)]
    messages[2].channel.category = NS(id=2, name='制作')
    t.collect.return_value = (messages, 0)
    delivered = []
    async def send(**kwargs):
        if len(delivered) == 1:
            raise http_error()
        delivered.append(NS(author=NS(id=99), embeds=[kwargs['embed']]))
    dest.send.side_effect = send
    before = t.load_state()
    with pytest.raises(discord.HTTPException):
        await t.publish(guild)
    assert t.load_state()['1']['last_post'] == before['1']['last_post']
    assert len(t.load_state()['1']['recent_picks']) == 1
    assert len(delivered) == 1
    dest.history = lambda **kwargs: items(delivered)
    dest.send.side_effect = None
    assert await t.publish(guild)
    assert dest.send.await_count == 3  # category1 success, category2 failure, category2 retry
    assert '制作' in dest.send.call_args.kwargs['embed'].title
    assert t.collect.call_args.args[1]['lookback_hours'] == 24


async def test_configurable_interval_persists_and_setup_preserves_it(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    c = importlib.import_module('timeline_commands')
    monkeypatch.setattr(t, 'STATE_FILE', tmp_path / 'state.json')
    inter = NS(guild=NS(id=1), response=NS(send_message=AsyncMock(), defer=AsyncMock()), followup=NS(send=AsyncMock()))
    await c.tl_interval.callback(inter, minutes=5, recent_hours=6)
    assert t.load_state()['1']['interval_minutes'] == 5
    monkeypatch.setattr(t, 'ensure_timeline', AsyncMock(return_value=NS(id=20, mention='#tl')))
    await c.tl_setup.callback(inter)
    assert t.load_state()['1']['enabled'] is True
    assert t.load_state()['1']['interval_minutes'] == 5
    assert t.load_state()['1']['timeline_lookback_hours'] == 6
    assert t.interval_minutes(t.load_settings(), t.load_state()['1']) == 5
    assert c.tl_interval.checks


async def test_history_reads_are_bounded_parallel_and_skip_old_channels(modules, monkeypatch):
    import asyncio
    t = importlib.import_module('timeline')
    active = 0; peak = 0
    async def history(**kwargs):
        nonlocal active, peak
        active += 1; peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        yield post()
    channels = [NS(id=i, history=Mock(side_effect=history), last_message_id=None) for i in range(8)]
    old = NS(id=90, history=Mock(), last_message_id=discord.utils.time_snowflake(datetime.now(timezone.utc) - timedelta(days=90)))
    monkeypatch.setattr(t, 'source_channels', lambda *a, **kw: channels + [old])
    result, skipped = await t.collect(NS(), t.load_settings())
    assert len(result) == 8 and skipped == 0
    assert 1 < peak <= 4
    old.history.assert_not_called()


async def test_all_mode_uses_new_categories_but_never_the_timeline_category(modules, monkeypatch):
    t = importlib.import_module('timeline')
    guild = NS(id=1, me=NS())
    def channel(cid, name):
        return NS(id=cid, name='daily', category=NS(id=cid, name=name),
                  permissions_for=lambda _: NS(view_channel=True, read_message_history=True))
    guild.text_channels = [channel(1, '新しい学年'), channel(2, 'タイムライン'), channel(3, '作品')]
    guild.categories = [c.category for c in guild.text_channels]
    monkeypatch.setattr(t, 'load_hidden_data', lambda: {})
    assert [c.id for c in t.source_channels(guild, t.load_settings(), user=NS())] == [3]
    guild.categories = [guild.categories[1], guild.categories[0], guild.categories[2]]
    assert [c.id for c in t.source_channels(guild, t.load_settings(), user=NS())] == [1, 3]


async def test_repeated_only_category_is_skipped_while_another_gets_new_post(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    guild, dest = publishing(t, monkeypatch, tmp_path)
    assert await t.publish(guild, force=True)
    # 同じ投稿しかないカテゴリは、次の配信枠でも再利用しない。
    assert not await t.publish(guild, force=True)
    assert dest.send.await_count == 1
    alternate = post(2)
    alternate.channel.category = NS(id=2, name='新しい日報')
    t.collect.return_value = ([post(1), alternate], 0)
    assert await t.publish(guild, force=True)
    assert dest.send.await_count == 2
    assert dest.send.call_args.kwargs['embed'].url == alternate.jump_url
    # 永続化したIDを読み直しても両方が除外される。
    history = t.active_pick_history(t.load_state()['1'], t.load_settings())
    assert t.unpicked([post(1), alternate], history) == []


async def test_repeat_cooldown_expires_and_old_config_gets_default(modules):
    t = importlib.import_module('timeline')
    config = t.load_settings()
    now = datetime.now(timezone.utc)
    entry = {'recent_picks': {'1': now.isoformat(), '2': (now - timedelta(hours=169)).isoformat()}}
    history = t.active_pick_history(entry, config, now=now)
    assert list(history) == ['1']
    assert [m.id for m in t.unpicked([post(1), post(2)], history)] == [2]
    assert config['repeat_cooldown_hours'] == 168


async def test_existing_timeline_links_seed_history_after_update(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    guild, dest = publishing(t, monkeypatch, tmp_path)
    original = t.diary_embed(post(1)); original.set_footer(text='ccm-tl:1:100:1')
    previous = NS(author=NS(id=99), created_at=datetime.now(timezone.utc) - timedelta(hours=2), embeds=[original])
    dest.history = lambda **kwargs: items([previous])
    assert not await t.publish(guild, force=True)
    dest.send.assert_not_awaited()
    assert '1' in t.load_state()['1']['recent_picks']


async def test_manual_gacha_shares_recent_picks_and_does_not_fall_back_to_seen_post(modules, monkeypatch, tmp_path):
    import asyncio
    t = importlib.import_module('timeline')
    c = importlib.import_module('timeline_commands')
    monkeypatch.setattr(t, 'STATE_FILE', tmp_path / 'state.json')
    monkeypatch.setattr(t, 'collect', AsyncMock(return_value=([post(1)], 0)))
    monkeypatch.setattr(t, 'source_channels', lambda *a, **kw: [NS(id=10)])
    def inter():
        return NS(guild=NS(id=1), user=NS(id=7), response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
    first, second = inter(), inter()
    await asyncio.gather(c.diary_gacha.callback(first), c.diary_gacha.callback(second))
    results = [i.followup.send.call_args for i in (first, second)]
    assert sum('embed' in r.kwargs for r in results) == 1
    assert any(r.args and '最近紹介した投稿しかないカテゴリ' in r.args[0] for r in results)
    assert list(t.load_state()['1']['recent_picks']) == ['1']


async def test_category_boundary_prefers_saved_tl_even_after_rename_and_fails_closed(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    monkeypatch.setattr(t, 'STATE_FILE', tmp_path / 'state.json')
    upper, anchor, lower = NS(id=1, name='管理'), NS(id=2, name='TLを改名'), NS(id=3, name='日報')
    guild = NS(id=1, categories=[upper, anchor, lower], get_channel=lambda _: NS(category=anchor))
    t.save_state({'1': {'channel_id': 20}})
    assert t.category_boundary(guild, t.load_settings()) == {3}
    guild.categories = [lower, anchor, upper]
    assert t.category_boundary(guild, t.load_settings()) == {1}
    t.save_state({})
    with pytest.raises(t.TimelineBoundaryError, match='/tl_boundary'):
        t.category_boundary(guild, t.load_settings())


async def test_notifications_once_for_all_categories_and_default_off(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    guild, dest = publishing(t, monkeypatch, tmp_path)
    messages = [post(1), post(2)]
    messages[1].channel.category = NS(id=2, name='作品')
    t.collect.return_value = (messages, 0)
    entry = t.load_state()['1']; entry['notification_mode'] = 'once'; t.save_entry(1, entry)
    dest.permissions_for.return_value = NS(mention_everyone=True)
    assert await t.publish(guild, force=True)
    calls = dest.send.call_args_list
    assert calls[0].kwargs['content'].startswith('@everyone')
    assert calls[0].kwargs['allowed_mentions'].everyone is True
    assert calls[1].kwargs['content'] is None
    assert calls[1].kwargs['allowed_mentions'].everyone is False
    assert t.notification_mode(t.load_settings(), {}) == 'off'
    entry = t.load_state()['1']; entry['notification_mode'] = 'off'; t.save_entry(1, entry)
    t.collect.return_value = ([post(3)], 0)
    assert await t.publish(guild, force=True)
    assert dest.send.call_args.kwargs['content'] is None
    assert dest.send.call_args.kwargs['allowed_mentions'].everyone is False


async def test_notification_not_repeated_after_partial_failure_and_missing_state_marker(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    guild, dest = publishing(t, monkeypatch, tmp_path)
    messages = [post(1), post(2)]
    messages[1].channel.category = NS(id=2, name='作品')
    t.collect.return_value = (messages, 0)
    entry = t.load_state()['1']; entry['notification_mode'] = 'once'; t.save_entry(1, entry)
    dest.permissions_for.return_value = NS(mention_everyone=True)
    delivered = []
    async def send(**kw):
        if len(delivered) == 1:
            raise http_error()
        delivered.append(NS(author=NS(id=99), embeds=[kw['embed']]))
    dest.send.side_effect = send
    with pytest.raises(discord.HTTPException):
        await t.publish(guild, force=True)
    entry = t.load_state()['1']; entry.pop('notified_marker', None); t.save_entry(1, entry)
    dest.history = lambda **kwargs: items(delivered)
    dest.send.side_effect = None
    assert await t.publish(guild, force=True)
    assert dest.send.call_args.kwargs['content'] is None
    assert dest.send.call_args.kwargs['allowed_mentions'].everyone is False


async def test_notification_mode_command_persists_and_checks_permission(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline'); c = importlib.import_module('timeline_commands')
    monkeypatch.setattr(t, 'STATE_FILE', tmp_path / 'state.json')
    target = NS(permissions_for=lambda _: NS(mention_everyone=False))
    inter = NS(guild=NS(id=1, me=NS(), get_channel=lambda _: target),
               response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
    t.save_state({'1': {'channel_id': 20}})
    await c.tl_notify.callback(inter, mode='once')
    assert 'notification_mode' not in t.load_state()['1']
    target.permissions_for = lambda _: NS(mention_everyone=True)
    await c.tl_notify.callback(inter, mode='once')
    assert t.load_state()['1']['notification_mode'] == 'once'
    await c.tl_notify.callback(inter, mode='off')
    assert t.load_state()['1']['notification_mode'] == 'off'
    assert c.tl_notify.checks


async def test_empty_update_never_sends_everyone_notification(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    guild, dest = publishing(t, monkeypatch, tmp_path)
    entry = t.load_state()['1']; entry['notification_mode'] = 'once'; t.save_entry(1, entry)
    t.collect.return_value = ([], 0)
    assert not await t.publish(guild, force=True)
    dest.send.assert_not_awaited()


async def test_lower_duplicate_name_category_is_not_excluded(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    monkeypatch.setattr(t, 'STATE_FILE', tmp_path / 'state.json')
    anchor, duplicate = NS(id=1, name='タイムライン'), NS(id=2, name='タイムライン')
    guild = NS(id=1, me=NS(), categories=[anchor, duplicate])
    source = NS(id=30, name='diary', category=duplicate,
                permissions_for=lambda _: NS(view_channel=True, read_message_history=True))
    guild.text_channels = [source]
    t.save_state({'1': {'channel_id': 20}})
    guild.get_channel = lambda _: NS(category=anchor)
    monkeypatch.setattr(t, 'load_hidden_data', lambda: {})
    assert [c.id for c in t.source_channels(guild, t.load_settings(), user=NS())] == [30]


async def test_ambiguous_or_missing_boundary_returns_actionable_error(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    monkeypatch.setattr(t, 'STATE_FILE', tmp_path / 'state.json')
    guild = NS(id=1, categories=[NS(id=1, name='タイムライン'), NS(id=2, name='タイムライン')])
    with pytest.raises(ValueError, match='/tl_boundary'):
        t.category_boundary(guild, t.load_settings())
    guild.categories = []
    with pytest.raises(ValueError, match='/tl_boundary'):
        t.category_boundary(guild, t.load_settings())


async def test_explicit_boundary_stays_pinned_when_tl_channel_moves(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    monkeypatch.setattr(t, 'STATE_FILE', tmp_path / 'state.json')
    upper, anchor, lower = NS(id=1, name='管理'), NS(id=2, name='TL'), NS(id=3, name='日報')
    guild = NS(id=1, categories=[upper, anchor, lower], get_channel=lambda _: NS(category=upper))
    t.save_state({'1': {'channel_id': 20, 'boundary_category_id': 2}})
    assert t.category_boundary(guild, t.load_settings(), destination=NS(category=upper)) == {3}
    guild.categories.remove(anchor)
    with pytest.raises(t.TimelineBoundaryError, match='指定し直し'):
        t.category_boundary(guild, t.load_settings())


async def test_boundary_command_sets_id_and_reports_targets_without_touching_channels(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline'); c = importlib.import_module('timeline_commands')
    monkeypatch.setattr(t, 'STATE_FILE', tmp_path / 'state.json')
    anchor, lower = NS(id=2, name='TL'), NS(id=3, name='日報')
    guild = NS(id=1, categories=[anchor, lower])
    inter = NS(guild=guild, response=NS(defer=AsyncMock()), followup=NS(send=AsyncMock()))
    monkeypatch.setattr(t, 'source_channels', lambda *a, **kw: [NS(id=10)])
    await c.tl_boundary.callback(inter, category=anchor)
    assert t.load_state()['1']['boundary_category_id'] == 2
    assert '取得可能: 1チャンネル' in inter.followup.send.call_args.args[0]
    assert inter.followup.send.call_args.kwargs['ephemeral'] is True
    assert c.tl_boundary.checks
    await c.tl_boundary.callback(inter)
    assert 'ID: 2' in inter.followup.send.call_args.args[0]


async def test_missing_boundary_returns_help_instead_of_empty_gacha_or_generic_error(modules, monkeypatch, tmp_path):
    t = importlib.import_module('timeline')
    monkeypatch.setattr(t, 'STATE_FILE', tmp_path / 'state.json')
    guild = NS(id=1, categories=[], text_channels=[])
    with pytest.raises(t.TimelineBoundaryError) as caught:
        await t.collect(guild, t.load_settings(), user=NS())
    monkeypatch.setattr(modules.events, 'notify_admin_error', AsyncMock())
    inter = NS(response=NS(is_done=lambda: True), followup=NS(send=AsyncMock()))
    await modules.events.on_app_command_error(inter, NS(original=caught.value))
    assert '/tl_boundary' in inter.followup.send.call_args.args[0]
    modules.events.notify_admin_error.assert_not_awaited()
