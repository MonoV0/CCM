"""実際のMember.move_to/editを使い、入室イベントとHTTP操作の順序を検証する。"""
import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
import pytest
import pytest_asyncio

from test_regressions import modules, http_error  # noqa: F401
from test_stage_participants import guild_and_channel, member, apply_overwrites


@pytest_asyncio.fixture
async def creation(modules, monkeypatch):
    m = modules
    guild, channel = guild_and_channel(m, role_names=('ex_member',))
    guild._add_channel(channel)
    trigger = discord.VoiceChannel(state=guild._state, guild=guild, data={
        'id': '20', 'name': 'trigger', 'type': 2, 'position': 0,
        'bitrate': 64000, 'user_limit': 0, 'permission_overwrites': [],
    })
    guild._add_channel(trigger)
    creator = member(guild, 7, discord.utils.get(guild.roles, name='ex_member'))
    guild._add_member(creator)
    voice_data = {'user_id': '7', 'session_id': 'test', 'deaf': False, 'mute': False,
                  'self_deaf': False, 'self_mute': False, 'suppress': True}
    guild._update_voice_state(voice_data, trigger.id)
    await m.events.bot._async_setup_hook()
    # 入室ハンドラによるウェルカム再送はこのテストの対象外。wait_forの実装は実物を使う。
    monkeypatch.setattr(m.events.bot, 'on_voice_state_update', AsyncMock())
    monkeypatch.setattr(m.events, 'STAGE_JOIN_TIMEOUT', 0.1)
    create = AsyncMock(return_value=channel)
    monkeypatch.setattr(discord.Guild, 'create_stage_channel', create)
    moved_payload = {'user': {'id': '7', 'username': 'creator', 'discriminator': '0', 'avatar': None},
                     'roles': [str(creator.roles[-1].id)], 'flags': 0}
    move = AsyncMock(return_value=moved_payload)
    monkeypatch.setattr(discord.http.HTTPClient, 'edit_member', move)
    promotion = AsyncMock()
    monkeypatch.setattr(discord.http.HTTPClient, 'edit_voice_state', promotion)
    start, send = AsyncMock(), AsyncMock()
    monkeypatch.setattr(discord.StageChannel, 'create_instance', start)
    monkeypatch.setattr(discord.StageChannel, 'send', send)

    def transition(destination):
        _, before, after = guild._update_voice_state(voice_data, destination.id if destination else None)
        m.events.bot.dispatch('voice_state_update', creator, before, after)

    yield NS(m=m, guild=guild, channel=channel, trigger=trigger, creator=creator, create=create,
             move=move, promotion=promotion, start=start, send=send, transition=transition,
             moved_payload=moved_payload)
    await asyncio.sleep(0)  # dispatchが作ったモックイベントタスクを完了させる。


@pytest.mark.asyncio
@pytest.mark.parametrize('timing', ['during_move', 'during_start', 'delayed'])
@pytest.mark.parametrize('modules', ['stage_bot'], indirect=True)
async def test_creator_invite_targets_new_stage_after_early_or_delayed_arrival(creation, timing):
    c = creation

    async def move(*args, **kwargs):
        assert kwargs['channel_id'] == c.channel.id
        if timing == 'during_move':
            c.transition(c.channel)
        elif timing == 'delayed':
            asyncio.get_running_loop().call_later(0.01, c.transition, c.channel)
        return c.moved_payload

    async def start(*args, **kwargs):
        if timing == 'during_start':
            c.transition(c.channel)

    c.move.side_effect = move
    c.start.side_effect = start
    assert await c.m.events.create_temp_stage(c.creator, {}) is c.channel
    c.promotion.assert_awaited_once()
    guild_id, user_id, payload = c.promotion.call_args.args
    assert (guild_id, user_id) == (c.guild.id, c.creator.id)
    assert payload['channel_id'] == c.channel.id
    assert payload['suppress'] is False
    assert 'スピーカー招待を送信しました' in c.send.call_args.args[0]
    assert '⚠️' not in c.send.call_args.args[0]
    # ex_member単独の作成者にも、実際の権限計算でパネルとチャットを許可する。
    apply_overwrites(c.channel, c.create.call_args.kwargs['overwrites'])
    effective = c.channel.permissions_for(c.creator)
    assert effective.send_messages and effective.manage_channels and effective.request_to_speak
    inter = NS(channel=c.channel, guild=c.guild, user=c.creator,
               response=NS(send_message=AsyncMock()))
    assert await c.m.views.StageConsoleView().interaction_check(inter)


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['stage_bot'], indirect=True)
async def test_missing_arrival_times_out_without_sending_to_old_channel(creation, caplog):
    c = creation
    await c.m.events.create_temp_stage(c.creator, {})
    c.promotion.assert_not_awaited()
    assert 'タイムアウト' in caplog.text and 'channel_id=30 user_id=7' in caplog.text
    text = c.send.call_args.args[0]
    assert '自動のスピーカー設定を完了できませんでした' in text
    assert 'スピーカー招待を送信しました' not in text


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['stage_bot'], indirect=True)
async def test_wait_ignores_other_members_guilds_and_channels(creation):
    c = creation
    other = member(c.guild, 8)
    foreign = NS(id=c.creator.id, guild=NS(id=999))

    def send_unrelated_then_arrive():
        dispatch = c.m.events.bot.dispatch
        dispatch('voice_state_update', other, None, NS(channel=c.channel))
        dispatch('voice_state_update', foreign, None, NS(channel=c.channel))
        dispatch('voice_state_update', c.creator, None, NS(channel=c.trigger))
        # 正しい入室が来るまでは、発言招待を送っていない。
        c.promotion.assert_not_awaited()
        asyncio.get_running_loop().call_later(0.01, c.transition, c.channel)

    async def move(*args, **kwargs):
        asyncio.get_running_loop().call_soon(send_unrelated_then_arrive)
        return c.moved_payload

    c.move.side_effect = move
    await c.m.events.create_temp_stage(c.creator, {})
    c.promotion.assert_awaited_once()
    assert c.promotion.call_args.args[2]['channel_id'] == c.channel.id


@pytest.mark.asyncio
@pytest.mark.parametrize('destination', ['trigger', 'disconnect'])
@pytest.mark.parametrize('modules', ['stage_bot'], indirect=True)
async def test_creator_leaving_before_promotion_does_not_modify_another_channel(creation, destination, caplog):
    c = creation

    def arrive_then_leave():
        c.transition(c.channel)
        c.transition(c.trigger if destination == 'trigger' else None)

    async def move(*args, **kwargs):
        asyncio.get_running_loop().call_soon(arrive_then_leave)
        return c.moved_payload

    c.move.side_effect = move
    await c.m.events.create_temp_stage(c.creator, {})
    c.promotion.assert_not_awaited()
    assert '移動したため発言招待を中止' in caplog.text
    assert '自動のスピーカー設定を完了できませんでした' in c.send.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['move', 'start', 'promotion'])
@pytest.mark.parametrize('modules', ['stage_bot'], indirect=True)
async def test_creation_failures_are_logged_and_reported_without_false_invite_success(creation, failure, caplog):
    c = creation

    async def move(*args, **kwargs):
        c.transition(c.channel)
        return c.moved_payload

    c.move.side_effect = move
    if failure == 'move':
        c.move.side_effect = http_error(discord.Forbidden)
    elif failure == 'start':
        c.start.side_effect = http_error(discord.Forbidden)
    else:
        c.promotion.side_effect = http_error(discord.Forbidden)
    await c.m.events.create_temp_stage(c.creator, {})
    if failure == 'promotion':
        c.promotion.assert_awaited_once()
        assert c.promotion.call_args.args[2]['channel_id'] == c.channel.id
    else:
        c.promotion.assert_not_awaited()
    expected = {'move': '自動移動に失敗', 'start': 'ステージの開始に失敗',
                'promotion': '自動のスピーカー設定を完了できませんでした'}[failure]
    assert expected in c.send.call_args.args[0]
    assert 'スピーカー招待を送信しました' not in c.send.call_args.args[0]
    assert 'guild_id=9 channel_id=30 user_id=7' in caplog.text
