"""共有一覧への情報漏れを実際のDiscord権限計算とモックAPIで検証する。"""
import asyncio
import copy
import importlib
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import discord
import pytest
import pytest_asyncio

from test_regressions import modules, http_error  # noqa: F401
from test_stage_participants import guild_and_channel, member, apply_overwrites

pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)]


async def items(values):
    for value in values:
        yield value


@pytest_asyncio.fixture
async def index(modules, monkeypatch):
    m = modules
    utils = importlib.import_module('utils')
    guild, _ = guild_and_channel(m)
    category = discord.CategoryChannel(state=guild._state, guild=guild,
                                      data={'id': '40', 'name': '日報_A26', 'position': 0, 'permission_overwrites': []})
    guild._add_channel(category)

    def text(cid, name):
        channel = discord.TextChannel(state=guild._state, guild=guild, data={
            'id': str(cid), 'name': name, 'type': 0, 'position': 0, 'parent_id': str(category.id),
            'permission_overwrites': [],
        })
        guild._add_channel(channel)
        return channel

    index_channel, source = text(50, m.storage.INDEX_CHANNEL_NAME), text(60, 'public')
    roles = {r.name: r for r in guild.roles}
    base = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
            roles['member']: discord.PermissionOverwrite(view_channel=True),
            roles['ex_member']: discord.PermissionOverwrite(view_channel=True)}
    apply_overwrites(index_channel, base)
    apply_overwrites(source, base)
    viewer = member(guild, 7, roles['ex_member'])
    guild._add_member(viewer)
    history = Mock(side_effect=lambda **kw: items([]))
    send = AsyncMock(return_value=NS(pin=AsyncMock()))
    edit = AsyncMock()
    monkeypatch.setattr(discord.TextChannel, 'history', history)
    monkeypatch.setattr(discord.TextChannel, 'send', send)
    monkeypatch.setattr(discord.TextChannel, 'edit', edit)
    yield NS(m=m, utils=utils, guild=guild, category=category, index=index_channel, source=source,
             roles=roles, base=base, viewer=viewer, text=text, history=history, send=send, edit=edit)


async def test_normal_channel_is_listed_despite_everyone_denial(index):
    c = index
    assert not c.source.permissions_for(c.guild.default_role).view_channel
    assert c.utils.visible_to_index_audience(c.source, c.index)


@pytest.mark.parametrize('restriction', ['ex_role', 'individual', 'only_owner', 'unknown_member'])
async def test_restricted_channel_is_not_listable(index, restriction):
    c = index
    overwrites = dict(c.base)
    if restriction == 'ex_role':
        overwrites[c.roles['ex_member']] = discord.PermissionOverwrite(view_channel=False)
    elif restriction == 'individual':
        overwrites[c.viewer] = discord.PermissionOverwrite(view_channel=False)
    elif restriction == 'only_owner':
        overwrites = {c.guild.default_role: discord.PermissionOverwrite(view_channel=False),
                      c.viewer: discord.PermissionOverwrite(view_channel=True)}
    else:
        overwrites[discord.Object(id=777, type=discord.Member)] = discord.PermissionOverwrite(view_channel=False)
    apply_overwrites(c.source, overwrites)
    assert not c.utils.visible_to_index_audience(c.source, c.index)


async def test_send_history_restrictions_alone_do_not_hide_visible_channels(index):
    c = index
    overwrites = dict(c.base)
    overwrites[c.viewer] = discord.PermissionOverwrite(send_messages=False, read_message_history=False)
    apply_overwrites(c.source, overwrites)
    assert c.utils.visible_to_index_audience(c.source, c.index)


async def test_multiple_roles_follow_effective_permissions_not_raw_denials(index):
    c = index
    extra = discord.Role(guild=c.guild, state=c.guild._state,
                         data={'id': '12', 'name': 'extra', 'permissions': '0', 'position': 3})
    c.guild._roles[extra.id] = extra
    c.viewer._roles = discord.utils.SnowflakeList([c.roles['ex_member'].id, extra.id])
    overwrites = dict(c.base)
    overwrites[extra] = discord.PermissionOverwrite(view_channel=False)
    apply_overwrites(c.source, overwrites)
    assert c.source.permissions_for(c.viewer).view_channel  # ex_memberの許可が有効
    assert c.utils.visible_to_index_audience(c.source, c.index)


async def test_index_with_wider_audience_does_not_disclose_channel(index):
    c = index
    apply_overwrites(c.index, {c.guild.default_role: discord.PermissionOverwrite(view_channel=True)})
    assert not c.utils.visible_to_index_audience(c.source, c.index)


async def test_unknown_individual_index_viewer_is_not_assumed_to_have_source_access(index):
    c = index
    overwrites = dict(c.base)
    overwrites[discord.Object(id=777, type=discord.Member)] = discord.PermissionOverwrite(view_channel=True)
    apply_overwrites(c.index, overwrites)
    assert not c.utils.visible_to_index_audience(c.source, c.index)


async def test_member_who_cannot_view_index_does_not_narrow_its_audience(index):
    c = index
    restricted = dict(c.base)
    restricted[c.viewer] = discord.PermissionOverwrite(view_channel=False)
    apply_overwrites(c.source, restricted)
    apply_overwrites(c.index, restricted)
    assert c.utils.visible_to_index_audience(c.source, c.index)


async def test_index_excludes_private_hidden_names_and_counts_and_removes_all_old_posts(index):
    c = index
    private, hidden = c.text(61, 'secret-name'), c.text(62, 'hidden-name')
    restricted = dict(c.base)
    restricted[c.viewer] = discord.PermissionOverwrite(view_channel=False)
    apply_overwrites(private, restricted)
    apply_overwrites(hidden, c.base)
    c.m.storage.save_hidden_data({'62': {'user_id': '8'}})
    c.m.storage.save_data({'7': '60', '8': '61'})
    secret_owner = member(c.guild, 8, c.roles['member'])
    secret_owner.nick = 'secret-owner'
    c.guild._add_member(secret_owner)
    old = [NS(author=c.guild.me, delete=AsyncMock()) for _ in range(60)]
    human = NS(author=c.viewer, delete=AsyncMock())
    c.history.side_effect = lambda **kw: items([*old, human])
    await c.utils.update_channel_index(c.guild, c.category)
    c.history.assert_called_once_with(limit=None)
    for message in old:
        message.delete.assert_awaited_once()
    human.delete.assert_not_awaited()
    text = '\n'.join(call.args[0] for call in c.send.call_args_list)
    assert '<#60>' in text and '（1件）' in text
    for secret in ('<#61>', '<#62>', 'secret-name', 'hidden-name', 'secret-owner'):
        assert secret not in text


@pytest.mark.parametrize('missing', [True, False])
async def test_failed_old_post_deletion_does_not_report_a_successful_rebuild(index, missing):
    c = index
    message = NS(author=c.guild.me, delete=AsyncMock(side_effect=http_error(
        discord.NotFound if missing else discord.Forbidden)))
    c.history.side_effect = lambda **kw: items([message])
    if missing:
        await c.utils.update_channel_index(c.guild, c.category)
        c.send.assert_awaited_once()
    else:
        with pytest.raises(discord.Forbidden):
            await c.utils.update_channel_index(c.guild, c.category)
        c.send.assert_not_awaited()


async def test_overlapping_rebuilds_use_latest_permissions_after_deleting_old_posts(index):
    c = index
    entered, release = asyncio.Event(), asyncio.Event()

    async def delete():
        entered.set()
        await release.wait()

    c.history.side_effect = lambda **kw: items([NS(author=c.guild.me, delete=delete)])
    first = asyncio.create_task(c.utils.update_channel_index(c.guild, c.category))
    await asyncio.wait_for(entered.wait(), timeout=1)
    private = dict(c.base)
    private[c.viewer] = discord.PermissionOverwrite(view_channel=False)
    apply_overwrites(c.source, private)
    second = asyncio.create_task(c.utils.update_channel_index(c.guild, c.category))
    await asyncio.sleep(0)
    assert c.edit.await_count == 1  # 同一カテゴリの更新を直列化
    release.set()
    await asyncio.gather(first, second)
    assert c.send.await_count == 2
    for call in c.send.call_args_list:
        assert '<#60>' not in call.args[0] and '（0件）' in call.args[0]


async def test_permission_change_rebuilds_index_but_position_change_does_not_loop(index, monkeypatch):
    c = index
    update = AsyncMock()
    monkeypatch.setattr(c.utils, 'update_channel_index', update)
    before = copy.copy(c.source)
    c.source.position = 1
    await c.m.events.on_guild_channel_update(before, c.source)
    update.assert_not_awaited()
    private = dict(c.base)
    private[c.viewer] = discord.PermissionOverwrite(view_channel=False)
    apply_overwrites(c.source, private)
    await c.m.events.on_guild_channel_update(before, c.source)
    update.assert_awaited_once_with(c.guild, c.category)


async def test_index_audience_and_category_permission_changes_trigger_refresh(index, monkeypatch):
    c = index
    update = AsyncMock()
    monkeypatch.setattr(c.utils, 'update_channel_index', update)
    for channel in (c.index, c.category):
        before = copy.copy(channel)
        apply_overwrites(channel, {c.guild.default_role: discord.PermissionOverwrite(view_channel=True)})
        await c.m.events.on_guild_channel_update(before, channel)
    assert update.await_count == 2


async def test_role_and_membership_changes_refresh_existing_indexes_only(index, monkeypatch):
    c = index
    update = AsyncMock()
    monkeypatch.setattr(c.utils, 'update_channel_index', update)
    await c.m.events.on_guild_role_update(NS(permissions=discord.Permissions.none()),
                                        NS(guild=c.guild, permissions=discord.Permissions(view_channel=True)))
    await c.m.events.on_guild_role_delete(NS(guild=c.guild))
    await c.m.events.on_member_update(NS(roles=[]), NS(guild=c.guild, roles=[c.roles['member']]))
    assert update.await_count == 3
    await c.m.events.on_member_update(NS(roles=[]), NS(guild=c.guild, roles=[]))
    assert update.await_count == 3
    # 一覧がないカテゴリでは何も作らない。
    await c.utils.refresh_existing_channel_indexes(c.guild, [NS(id=99, channels=[])])
    assert update.await_count == 3


async def test_channel_create_delete_and_move_refresh_the_relevant_existing_indexes(index, monkeypatch):
    c = index
    update = AsyncMock()
    monkeypatch.setattr(c.utils, 'update_channel_index', update)
    await c.m.events.on_guild_channel_create(c.source)
    await c.m.events.on_guild_channel_delete(c.source)
    assert update.await_count == 2
    await c.m.events.on_guild_channel_create(c.index)
    await c.m.events.on_guild_channel_delete(c.index)
    assert update.await_count == 2
    before = copy.copy(c.source)
    c.source.category_id = None
    await c.m.events.on_guild_channel_update(before, c.source)
    assert update.await_count == 3


async def test_ready_rebuilds_existing_indexes_and_refresh_errors_are_logged(index, monkeypatch, caplog):
    c = index
    c.m.events.bot._connection._guilds[c.guild.id] = c.guild
    update = AsyncMock()
    monkeypatch.setattr(c.utils, 'update_channel_index', update)
    await c.m.events.refresh_indexes_on_ready()
    update.assert_awaited_once_with(c.guild, c.category)
    update.side_effect = http_error(discord.Forbidden)
    await c.utils.refresh_existing_channel_indexes(c.guild)
    assert 'guild_id=9 category_id=40' in caplog.text
