"""ロールに送信・挙手権限がない利用者と、非公開・ブロックの実効権限を検証する。"""
from unittest.mock import AsyncMock

import discord
import pytest

from test_regressions import modules, http_error  # noqa: F401


def guild_and_channel(m, *, role_names=('member', 'ex_member')):
    state = m.events.bot._connection
    roles = [{'id': '9', 'name': '@everyone', 'permissions': '0'}]
    roles += [{'id': str(i), 'name': name, 'permissions': '0', 'position': i - 9}
              for i, name in enumerate(role_names, 10)]
    guild = discord.Guild(data={'id': '9', 'owner_id': '99', 'roles': roles}, state=state)
    state.user = member(guild, 2)
    guild._add_member(state.user)
    channel = discord.StageChannel(state=state, guild=guild, data={
        'id': '30', 'name': 'stage', 'type': 13, 'position': 0, 'bitrate': 64000, 'user_limit': 0,
        'permission_overwrites': [],
    })
    return guild, channel


def member(guild, uid, role=None):
    return discord.Member(guild=guild, state=guild._state, data={
        'user': {'id': str(uid), 'username': f'user{uid}', 'discriminator': '0', 'avatar': None},
        'roles': [str(role.id)] if role else [], 'flags': 0,
    })


def apply_overwrites(channel, overwrites):
    payload = []
    for target, overwrite in overwrites.items():
        allow, deny = overwrite.pair()
        payload.append({'id': str(target.id), 'type': 0 if isinstance(target, discord.Role) else 1,
                        'allow': str(allow.value), 'deny': str(deny.value)})
    channel._fill_overwrites({'permission_overwrites': payload})


@pytest.mark.asyncio
@pytest.mark.parametrize('role_names', [('member', 'ex_member'), ('ex_member',), ()])
@pytest.mark.parametrize('modules', ['stage_bot'], indirect=True)
async def test_new_stage_grants_chat_and_hand_raise_without_management(modules, monkeypatch, role_names):
    m = modules
    guild, channel = guild_and_channel(m, role_names=role_names)
    creator = member(guild, 7)
    create = AsyncMock(return_value=channel)
    monkeypatch.setattr(discord.Guild, 'create_stage_channel', create)
    async def move_to(destination):
        guild._add_channel(destination)
        guild._update_voice_state({'user_id': str(creator.id), 'session_id': 'test', 'deaf': False,
                                  'mute': False, 'self_deaf': False, 'self_mute': False,
                                  'suppress': True}, destination.id)

    monkeypatch.setattr(discord.Member, 'move_to', AsyncMock(side_effect=move_to))
    edit = AsyncMock()
    monkeypatch.setattr(discord.Member, 'edit', edit)
    monkeypatch.setattr(discord.StageChannel, 'create_instance', AsyncMock())
    monkeypatch.setattr(discord.StageChannel, 'send', AsyncMock())

    assert await m.events.create_temp_stage(creator, {}) is channel
    apply_overwrites(channel, create.call_args.kwargs['overwrites'])
    owner_permissions = channel.permissions_for(creator)
    assert owner_permissions.send_messages and owner_permissions.read_message_history
    assert owner_permissions.manage_channels and owner_permissions.mute_members
    edit.assert_awaited_once_with(suppress=False)
    for role in guild.roles:
        if role.is_default():
            continue
        participant = member(guild, 8, role)
        permissions = channel.permissions_for(participant)
        assert permissions.view_channel and permissions.connect
        assert permissions.send_messages and permissions.read_message_history and permissions.request_to_speak
        assert not permissions.manage_channels and not permissions.mute_members
        # サーバー全体のロール権限を変更していない。
        assert role.permissions.value == 0
    assert not channel.permissions_for(member(guild, 6)).send_messages


@pytest.mark.asyncio
@pytest.mark.parametrize('private', [False, True])
@pytest.mark.parametrize('modules', ['stage_bot'], indirect=True)
async def test_existing_stage_repair_preserves_privacy_invites_and_blocks(modules, monkeypatch, private):
    m = modules
    guild, channel = guild_and_channel(m)
    ex_role = discord.utils.get(guild.roles, name='ex_member')
    invited, blocked, ordinary = [member(guild, uid, ex_role) for uid in (7, 8, 6)]
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=not private, connect=not private),
        ex_role: discord.PermissionOverwrite(attach_files=False),
        invited: discord.PermissionOverwrite(view_channel=True, connect=True),
        blocked: discord.PermissionOverwrite(view_channel=False, connect=False),
    }
    apply_overwrites(channel, overwrites)
    assert not channel.permissions_for(invited).send_messages
    assert not channel.permissions_for(invited).request_to_speak

    async def set_permissions(target, *, overwrite, reason):
        overwrites[target] = overwrite
        apply_overwrites(channel, overwrites)

    setter = AsyncMock(side_effect=set_permissions)
    monkeypatch.setattr(discord.StageChannel, 'set_permissions', setter)
    await m.events.repair_participant_permissions(channel)
    permissions = channel.permissions_for(invited)
    assert permissions.send_messages and permissions.read_message_history and permissions.request_to_speak
    assert not permissions.attach_files and not permissions.manage_channels and not permissions.mute_members
    assert channel.permissions_for(ordinary).view_channel is (not private)
    assert channel.permissions_for(ordinary).connect is (not private)
    assert not channel.permissions_for(blocked).view_channel
    assert not channel.permissions_for(blocked).connect
    assert overwrites[guild.default_role].view_channel is (not private)
    assert overwrites[invited].connect is True and overwrites[blocked].connect is False
    # 補正済みなら再接続しても同じAPIリクエストを繰り返さない。
    assert setter.await_count == 2
    await m.events.repair_participant_permissions(channel)
    assert setter.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['stage_bot'], indirect=True)
async def test_repair_failure_is_logged_and_next_role_is_attempted(modules, monkeypatch, caplog):
    m = modules
    _, channel = guild_and_channel(m)
    setter = AsyncMock(side_effect=[http_error(discord.Forbidden), None])
    monkeypatch.setattr(discord.StageChannel, 'set_permissions', setter)
    await m.events.repair_participant_permissions(channel)
    assert setter.await_count == 2
    assert 'channel_id=30 role_id=10' in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['stage_bot'], indirect=True)
async def test_ready_repairs_only_occupied_tracked_stages(modules, monkeypatch):
    m = modules
    guild, channel = guild_and_channel(m)
    guild._add_channel(channel)
    occupant = member(guild, 7)
    guild._add_member(occupant)
    guild._update_voice_state({'user_id': '7', 'session_id': 'test', 'deaf': False, 'mute': False,
                               'self_deaf': False, 'self_mute': False, 'suppress': True}, channel.id)
    m.storage.temp_channels['30'] = {'guild_id': guild.id}
    m.events.bot._connection.user = occupant
    monkeypatch.setattr(m.events.bot, 'get_channel', lambda cid: channel)
    repair = AsyncMock()
    delete = AsyncMock()
    monkeypatch.setattr(m.events, 'repair_participant_permissions', repair)
    monkeypatch.setattr(m.events, 'maybe_delete_temp_channel', delete)
    await m.events.on_ready()
    repair.assert_awaited_once_with(channel)
    delete.assert_not_awaited()
    guild._voice_states.clear()
    repair.reset_mock()
    await m.events.on_ready()
    repair.assert_not_awaited()
    delete.assert_awaited_once_with(channel)
