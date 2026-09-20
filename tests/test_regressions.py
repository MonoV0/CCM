"""Discordへ接続せず、実際のView/ModalとAPIのモックで回帰を検証する。"""
import importlib
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import discord
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def modules(request, tmp_path, monkeypatch):
    source = Path(__file__).resolve().parents[1] / request.param
    root = tmp_path / request.param
    shutil.copytree(source, root, ignore=shutil.ignore_patterns('data', '__pycache__', '*.log', '.env'))
    names = ['discord_settings', 'storage', 'bot_core', 'utils', 'operations', 'stage_context', 'onboarding', 'invitations', 'updater', 'views', 'events', 'commands']
    previous = {n: sys.modules.pop(n) for n in names if n in sys.modules}
    monkeypatch.chdir(root)
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setenv('DISCORD_TOKEN', 'test-only')
    monkeypatch.setenv('ADMIN_ID', '99')
    loaded = {n: importlib.import_module(n) for n in ['storage', 'views', 'events', 'commands']}
    loaded['kind'] = request.param
    yield NS(**loaded)
    await sys.modules['bot_core'].bot.close()
    for n in names:
        sys.modules.pop(n, None)
    sys.modules.update(previous)


def require(m, kind):
    if m.kind != kind:
        pytest.skip('other bot')


@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
def test_discord_settings_drive_roles_and_grade_buttons(modules, tmp_path, monkeypatch):
    import json
    settings = importlib.import_module('discord_settings')
    custom = {
        'roles': {'member': '参加者', 'ex_member': 'ゲスト'},
        'categories': {'welcome': '案内', 'external': '外部', 'grades': {'新入生': '日報_新入生'}},
    }
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps(custom, ensure_ascii=False), encoding='utf-8')
    assert settings.load_discord_settings(path) == custom
    monkeypatch.setattr(settings, 'ROLE_NAMES', custom['roles'])
    assert settings.get_role(NS(roles=[NS(name='参加者')]), 'member').name == '参加者'
    monkeypatch.setattr(modules.views, 'GRADE_CATEGORIES', custom['categories']['grades'])
    buttons = modules.views.GradeSelectView().children
    assert [button.label for button in buttons] == ['新入生', '← 戻る']
    custom['categories']['external'] = '日報_新入生'
    path.write_text(json.dumps(custom, ensure_ascii=False), encoding='utf-8')
    with pytest.raises(ValueError, match='重複'):
        settings.load_discord_settings(path)


def interaction(channel=None):
    return NS(
        channel=channel, user=NS(id=7, roles=[]),
        response=NS(defer=AsyncMock(), send_message=AsyncMock(), send_modal=AsyncMock()),
        followup=NS(send=AsyncMock()), edit_original_response=AsyncMock(),
        guild=NS(id=9, roles=[], get_channel=lambda cid: channel),
    )


def stage(m):
    ch = Mock(spec=discord.StageChannel)
    ch.id, ch.name, ch.mention, ch.members = 1, 'stage', '#stage', []
    ch.guild = NS(default_role=NS(id=9))
    ch.permissions_for.return_value = NS(manage_channels=True)
    ch.overwrites_for.return_value = discord.PermissionOverwrite(view_channel=False, connect=False)
    ch.delete, ch.edit, ch.set_permissions, ch.send = AsyncMock(), AsyncMock(), AsyncMock(), AsyncMock()
    m.storage.temp_channels['1'] = {'guild_id': 9, 'blocked_users': [], 'password': 'secret', 'tickets': {}, 'sub_admins': []}
    return ch


def http_error(cls=discord.HTTPException):
    return cls(NS(status=404 if cls is discord.NotFound else 403, reason='test'), 'simulated failure')


@pytest.mark.asyncio
@pytest.mark.parametrize('blocked', [True, False])
@pytest.mark.parametrize('route', ['password', 'ticket'])
@pytest.mark.parametrize("modules", ["stage_bot"], indirect=True)
async def test_join_block_and_success(modules, blocked, route):
    m = modules; require(m, 'stage_bot'); ch = stage(m)
    data = m.storage.temp_channels['1']
    data['blocked_users'] = [7] if blocked else []
    inter = interaction(ch)
    if route == 'password':
        modal = m.views.JoinPasswordModal(ch); modal.pwd_field._value = 'secret'
    else:
        data['tickets']['ABCDEFGH'] = {'used': False}
        modal = m.views.TicketJoinModal(); modal.token_field._value = 'ABCDEFGH'
    await modal.on_submit(inter)
    assert ch.set_permissions.await_count == (0 if blocked else 1)
    if route == 'ticket':
        assert data['tickets']['ABCDEFGH']['used'] is (not blocked)


@pytest.mark.asyncio
@pytest.mark.parametrize('result', ['success', 'missing', 'failure'])
@pytest.mark.parametrize("modules", ["stage_bot"], indirect=True)
async def test_end_stage_buttons(modules, result):
    m = modules; require(m, 'stage_bot'); ch = stage(m)
    if result != 'success': ch.delete.side_effect = http_error(discord.NotFound if result == 'missing' else discord.Forbidden)
    inter = interaction(ch)
    view = m.views.DeleteConfirmView(ch)
    await view.btn_delete_only.callback(inter)
    ch.delete.assert_awaited_once()
    assert ('1' in m.storage.temp_channels) is (result == 'failure')
    if result == 'failure': assert '削除できません' in inter.followup.send.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("modules", ["stage_bot"], indirect=True)
async def test_download_and_end(modules, monkeypatch):
    m = modules; require(m, 'stage_bot'); ch = stage(m)
    monkeypatch.setattr(m.views, 'generate_log_file', AsyncMock(return_value=None))
    await m.views.DeleteConfirmView(ch).btn_download_and_delete.callback(interaction(ch))
    ch.delete.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("modules", ["stage_bot"], indirect=True)
async def test_text_channel_panel_and_modal(modules):
    m = modules; require(m, 'stage_bot'); ch = stage(m)
    inter = interaction(NS(id=22)); inter.guild.get_channel = lambda cid: ch if cid == 1 else None
    inter.guild.get_member = lambda uid: NS(voice=NS(channel=ch))
    await m.commands.stage_panel.callback(inter)
    view = inter.response.send_message.call_args.kwargs['view']
    assert await view.interaction_check(inter)
    await view.btn_name.callback(inter)
    modal = inter.response.send_modal.call_args.args[0]
    assert await modal.interaction_check(inter)
    modal.input_field._value = 'renamed'
    await modal.on_submit(inter)
    assert ch.edit.call_args.kwargs['name'] == '🔒renamed'
    ch.permissions_for.return_value.manage_channels = False
    assert not await modal.interaction_check(inter)
    ch.permissions_for.return_value.manage_channels = True
    await view.btn_simple.callback(inter)
    simple = inter.response.send_message.call_args.kwargs['view']
    assert simple.get_channel(inter) is ch
    await simple.btn_private.callback(inter)
    ch.set_permissions.assert_awaited_once()
    await view.btn_preset.callback(inter)
    presets = inter.response.send_message.call_args.kwargs['view']
    await presets.btn_save.callback(inter)
    assert inter.response.send_modal.call_args.args[0].get_channel(inter) is ch
    await view.btn_stream.callback(inter)
    assert inter.response.send_modal.call_args.args[0].get_channel(inter) is ch
    inter.guild.get_channel = lambda cid: None
    assert not await view.interaction_check(inter)


@pytest.mark.asyncio
@pytest.mark.parametrize('result', ['success', 'missing', 'failure'])
@pytest.mark.parametrize("modules", ["channel_manager"], indirect=True)
async def test_expired_deletion_records(modules, monkeypatch, result):
    m = modules; require(m, 'channel_manager')
    ch = NS(delete=AsyncMock())
    if result != 'success': ch.delete.side_effect = http_error(discord.NotFound if result == 'missing' else discord.Forbidden)
    m.storage.save_data({'7': '1'})
    m.storage.save_hidden_data({'1': {'user_id': '7', 'hidden_at': (datetime.now(timezone.utc)-timedelta(days=31)).isoformat()}})
    monkeypatch.setattr(m.events.bot, 'get_channel', lambda cid: ch)
    await m.events.cleanup_expired_hidden_channels()
    assert bool(m.storage.load_data()) is (result == 'failure')
    assert bool(m.storage.load_hidden_data()) is (result == 'failure')


@pytest.mark.asyncio
@pytest.mark.parametrize('result', ['success', 'missing', 'failure'])
@pytest.mark.parametrize("modules", ["channel_manager"], indirect=True)
async def test_self_delete(modules, result):
    m = modules; require(m, 'channel_manager')
    ch = NS(id=1, category=None, delete=AsyncMock())
    if result != 'success': ch.delete.side_effect = http_error(discord.NotFound if result == 'missing' else discord.Forbidden)
    m.storage.save_data({'7': '1'}); m.storage.save_hidden_data({'1': {'user_id': '7'}})
    inter = interaction(ch)
    await m.views.DeleteMyDataConfirmView(ch).confirm.callback(inter)
    assert bool(m.storage.load_data()) is (result == 'failure')
    assert bool(m.storage.load_hidden_data()) is (result == 'failure')
    text = inter.edit_original_response.call_args.kwargs['content']
    assert ('完全に削除しました' in text) is (result != 'failure')
    inter.response.defer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("modules", ["channel_manager"], indirect=True)
async def test_hidden_mydata(modules):
    m = modules; require(m, 'channel_manager')
    m.storage.save_data({'7': '1'})
    m.storage.save_hidden_data({'1': {'hidden_at': '2026-01-01T00:00:00+00:00'}})
    inter = interaction(NS(id=1, mention='#hidden'))
    await m.commands.mydata.callback(inter)
    embed = inter.followup.send.call_args.kwargs['embed']
    assert any('2026-01-31' in f.value for f in embed.fields)


@pytest.mark.asyncio
@pytest.mark.parametrize("modules", ["stage_bot"], indirect=True)
async def test_ticket_can_retry_api_failure(modules):
    m = modules; require(m, 'stage_bot'); ch = stage(m)
    data = m.storage.temp_channels['1']
    data['tickets']['ABCDEFGH'] = {'used': False}
    ch.set_permissions.side_effect = http_error()
    modal = m.views.TicketJoinModal(); modal.token_field._value = 'ABCDEFGH'
    await modal.on_submit(interaction(ch))
    assert not data['tickets']['ABCDEFGH']['used']


@pytest.mark.asyncio
@pytest.mark.parametrize("modules", ["stage_bot"], indirect=True)
async def test_presets_and_persistent_panel(modules):
    m = modules; require(m, 'stage_bot'); ch = stage(m)
    inter = interaction(NS(id=22)); inter.guild.get_channel = lambda cid: ch
    assert m.views.StageConsoleView().is_persistent()
    assert m.views.SimplePanelView().is_persistent()
    assert await m.views.StageConsoleView().interaction_check(interaction(ch))
    m.storage.user_presets['7'] = [{'label': 'saved', 'name': 'preset', 'topic': None, 'limit': 4}]
    menu = m.views.PresetMenuView('7', target_channel=ch)
    await menu.btn_apply.callback(inter)
    view = inter.response.send_message.call_args.kwargs['view']
    assert await view.interaction_check(inter)
    select = view.children[0]; select._values = ['0']
    await select.callback(inter)
    assert ch.edit.await_count == 2
    assert ch.edit.call_args.kwargs == {'user_limit': 4}


@pytest.mark.asyncio
@pytest.mark.parametrize("modules", ["stage_bot"], indirect=True)
async def test_privacy_permission_failure_does_not_report_success(modules):
    m = modules; require(m, 'stage_bot'); ch = stage(m)
    ch.set_permissions.side_effect = http_error()
    inter = interaction(ch)
    await m.views.SimplePanelView(target_channel=ch).btn_private.callback(inter)
    ch.edit.assert_not_awaited()
    assert '失敗' in inter.followup.send.call_args.args[0]


def welcome_interaction():
    welcome = Mock(spec=discord.TextChannel)
    welcome.id = 50
    welcome.category = NS(name='ようこそ')
    welcome.overwrites_for.return_value = discord.PermissionOverwrite(view_channel=True)
    welcome.delete = AsyncMock()
    inter = interaction(welcome)
    inter.user.mention = '<@7>'
    return inter


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
@pytest.mark.parametrize('route', ['grade', 'external', 'member', 'existing'])
@pytest.mark.parametrize('registered', [False, True])
async def test_onboarding_routes_cleanup(modules, monkeypatch, route, registered):
    m = modules
    onboarding = importlib.import_module('onboarding')
    monkeypatch.setattr(onboarding.asyncio, 'sleep', AsyncMock())
    personal = NS(id=60, mention='#personal', category=None, send=AsyncMock())
    monkeypatch.setattr(m.views, 'get_existing_channel', AsyncMock(return_value=personal if registered else None))
    create = AsyncMock(return_value=personal)
    monkeypatch.setattr(m.views, 'create_personal_channel', create)
    monkeypatch.setattr(m.views, 'restore_channel_permissions', AsyncMock())
    inter = welcome_interaction()
    if route == 'grade':
        await m.views.GradeButton('A26', '日報_A26', 0).callback(inter)
    else:
        view = m.views.RoleSelectView()
        button = {'external': view.ex_member_button, 'member': view.member_button, 'existing': view.already_have_channel}[route]
        await button.callback(inter)
    complete = registered or route in ('grade', 'external')
    assert inter.channel.delete.await_count == int(complete)
    if route == 'grade' and registered: create.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
@pytest.mark.parametrize('failure', ['notice', 'temporary_delete', 'forbidden', 'missing'])
async def test_onboarding_cleanup_failures(modules, monkeypatch, failure):
    onboarding = importlib.import_module('onboarding')
    monkeypatch.setattr(onboarding.asyncio, 'sleep', AsyncMock())
    notify = AsyncMock(); monkeypatch.setattr(onboarding, 'notify_admin_error', notify)
    inter = welcome_interaction()
    if failure == 'notice': inter.followup.send.side_effect = http_error()
    if failure == 'temporary_delete': inter.channel.delete.side_effect = [http_error(), None]
    if failure == 'forbidden': inter.channel.delete.side_effect = http_error(discord.Forbidden)
    if failure == 'missing': inter.channel.delete.side_effect = http_error(discord.NotFound)
    await onboarding.finish_onboarding(inter, NS(id=60, mention='#personal'))
    assert inter.channel.delete.await_count == (2 if failure == 'temporary_delete' else 1)
    assert notify.await_count == int(failure == 'forbidden')


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
@pytest.mark.parametrize('unsafe', ['same_channel', 'other_category', 'other_member'])
async def test_onboarding_preserves_unrelated_channels(modules, monkeypatch, unsafe):
    onboarding = importlib.import_module('onboarding')
    inter = welcome_interaction()
    personal = NS(id=60, mention='#personal')
    if unsafe == 'same_channel': personal.id = inter.channel.id
    if unsafe == 'other_category': inter.channel.category.name = '日報_A26'
    if unsafe == 'other_member': inter.channel.overwrites_for.return_value.view_channel = None
    await onboarding.finish_onboarding(inter, personal)
    inter.channel.delete.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
async def test_creation_survives_optional_post_and_index_failures(modules, monkeypatch):
    utils = importlib.import_module('utils')
    category = NS(id=3)
    channel = NS(id=60, set_permissions=AsyncMock(), send=AsyncMock(side_effect=http_error()))
    member = Mock(); member.id = 7; member.name = 'test'; member.mention = '<@7>'
    guild = NS(default_role=Mock(), roles=[], create_text_channel=AsyncMock(return_value=channel))
    monkeypatch.setattr(utils, 'get_or_create_available_category', AsyncMock(return_value=category))
    monkeypatch.setattr(utils, 'update_channel_index', AsyncMock(side_effect=http_error()))
    result = await utils.create_personal_channel(guild, member, '日報_A26')
    assert result is channel
    assert modules.storage.load_data() == {'7': '60'}
    assert channel.send.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
async def test_incomplete_creation_keeps_welcome(modules, monkeypatch):
    monkeypatch.setattr(modules.views, 'get_existing_channel', AsyncMock(return_value=None))
    monkeypatch.setattr(modules.views, 'create_personal_channel', AsyncMock(side_effect=http_error()))
    inter = welcome_interaction()
    with pytest.raises(discord.HTTPException):
        await modules.views.GradeButton('A26', '日報_A26', 0).callback(inter)
    inter.channel.delete.assert_not_awaited()
