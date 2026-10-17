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
    return NS(id=pid, content=content, attachments=[], author=NS(bot=False),
              reactions=[NS(emoji=emoji, count=count, me=True)],
              created_at=datetime.now(timezone.utc), channel=NS(id=10),
              jump_url=f'https://discord.com/channels/1/10/{pid}')


async def test_scores_custom_emoji_and_threshold(modules):
    t = importlib.import_module('timeline')
    message = post(emoji='<:star:123>')
    assert t.reaction_score(message, ['<:renamed:123>']) == 2
    assert t.ranked([message], ['👍']) == []
    assert t.ranked([message], ['<:star:123>'], 3) == []
    assert t.emoji_key('⭐️') == t.emoji_key('⭐')


async def test_search_excludes_private_hidden_and_outside_categories(modules, monkeypatch):
    t = importlib.import_module('timeline')
    guild = NS(me=NS(id=99))
    def channel(cid, category, readable=True):
        c = NS(id=cid, name='personal', category=NS(name=category), guild=guild)
        c.permissions_for = lambda who: NS(view_channel=readable, read_message_history=True)
        c.history = Mock(return_value=items([post(cid)]))
        return c
    allowed = channel(10, '日報_A26-2')
    hidden = channel(11, '日報_A26')
    private = channel(12, '日報_A26', False)
    outside = channel(13, '管理者')
    guild.text_channels = [allowed, hidden, private, outside]
    monkeypatch.setattr(t, 'load_hidden_data', lambda: {'11': {}})
    results, skipped = await t.collect(guild, t.load_settings(), user=NS(id=7))
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
    assert '[日報を読む]' in embed.fields[0].value
    assert embed.title == '🎲 日報ガチャ'
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
    with pytest.raises(ValueError, match='閲覧権限'):
        await t.publish(guild)
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
    assert '[日報を読む]' in inter.followup.send.call_args.kwargs['embed'].fields[0].value


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
