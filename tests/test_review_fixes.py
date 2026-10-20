"""レビュー指摘（チャンネル乗っ取り・永続View・エラー通知・応答遅延・二重作成・JSON破損）の回帰テスト。"""
import asyncio
import importlib
import json
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from test_regressions import modules, interaction, http_error  # noqa: F401


def personal_guild(*channels):
    category = NS(name='日報_A26', channels=list(channels))
    return NS(categories=[category], get_channel=lambda cid: None)


def text_channel(cid, name, manage=None):
    channel = NS(id=cid, name=name)
    channel.overwrites_for = lambda member: discord.PermissionOverwrite(manage_channels=manage)
    return channel


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
@pytest.mark.parametrize('owner', ['linked_other', 'hidden_other', 'unclaimed', 'hidden_self'])
async def test_name_fallback_never_claims_other_users_channel(modules, owner):
    utils = importlib.import_module('utils')
    channel = text_channel(60, 'taro-diary')
    if owner == 'linked_other':
        modules.storage.save_data({'1': '60'})
    if owner in ('hidden_other', 'hidden_self'):
        modules.storage.save_hidden_data({'60': {'user_id': '1' if owner == 'hidden_other' else '7',
                                                 'hidden_at': datetime.now(timezone.utc).isoformat()}})
    member = NS(id=7, name='taro-diary')
    found = await utils.get_existing_channel(personal_guild(channel), member)
    claimable = owner in ('unclaimed', 'hidden_self')
    assert (found is channel) is claimable
    assert ('7' in modules.storage.load_data()) is claimable


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
async def test_owner_permission_fallback_skips_channels_linked_to_others(modules):
    utils = importlib.import_module('utils')
    modules.storage.save_data({'1': '60'})
    channel = text_channel(60, 'other', manage=True)
    assert await utils.get_existing_channel(personal_guild(channel), NS(id=7, name='me')) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
@pytest.mark.parametrize('kind', ['cooldown', 'dm', 'permission'])
async def test_user_side_command_errors_do_not_alert_admin(modules, monkeypatch, kind):
    notify = AsyncMock(); monkeypatch.setattr(modules.events, 'notify_admin_error', notify)
    error = {
        'cooldown': discord.app_commands.CommandOnCooldown(discord.app_commands.Cooldown(1, 60), 42.0),
        'dm': discord.app_commands.NoPrivateMessage(),
        'permission': discord.app_commands.MissingPermissions(['manage_guild']),
    }[kind]
    inter = NS(response=NS(is_done=lambda: False, send_message=AsyncMock()), followup=NS(send=AsyncMock()))
    await modules.events.on_app_command_error(inter, error)
    notify.assert_not_awaited()
    text = inter.response.send_message.call_args.args[0]
    assert ('42秒' in text) if kind == 'cooldown' else ('内部エラー' not in text)


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
async def test_internal_command_error_still_alerts_admin(modules, monkeypatch):
    notify = AsyncMock(); monkeypatch.setattr(modules.events, 'notify_admin_error', notify)
    inter = NS(command=NS(name='find'), user='u', response=NS(is_done=lambda: True), followup=NS(send=AsyncMock()))
    await modules.events.on_app_command_error(inter, RuntimeError('boom'))
    notify.assert_awaited_once()
    assert '内部エラー' in inter.followup.send.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
async def test_cleanup_task_reports_and_survives_unexpected_errors(modules, monkeypatch):
    notify = AsyncMock(); monkeypatch.setattr(modules.events, 'notify_admin_error', notify)
    modules.storage.save_hidden_data({'1': {'user_id': '7', 'hidden_at': (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()}})
    monkeypatch.setattr(modules.events.bot, 'get_channel', Mock(side_effect=RuntimeError('cache broken')))
    await modules.events.cleanup_expired_hidden_channels()
    notify.assert_awaited_once()


@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
def test_onboarding_views_are_persistent_and_registered(modules, monkeypatch):
    v = modules.views
    for view in (v.RulesView(), v.RoleSelectView(), v.GradeSelectView()):
        assert view.is_persistent()
    added = []
    monkeypatch.setattr(v.bot, 'add_view', lambda view, **kw: added.append(type(view).__name__))
    v.register_onboarding_views()
    assert added == ['RulesView', 'RoleSelectView', 'GradeSelectView']


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
async def test_double_click_creates_one_personal_channel(modules, monkeypatch):
    v = modules.views
    personal = NS(id=60, mention='#personal', category=None, send=AsyncMock())
    created = []

    async def existing(guild, member):
        return personal if created else None

    async def create(guild, member, category_name):
        await asyncio.sleep(0)  # 作成中に2回目のクリックが割り込む状況を作る
        created.append(category_name)
        return personal

    monkeypatch.setattr(v, 'get_existing_channel', existing)
    monkeypatch.setattr(v, 'create_personal_channel', create)
    monkeypatch.setattr(v, 'restore_channel_permissions', AsyncMock())
    monkeypatch.setattr(v, 'finish_onboarding', AsyncMock())
    button = v.GradeButton('A26', '日報_A26', 0)
    await asyncio.gather(button.callback(interaction()), button.callback(interaction()))
    assert created == ['日報_A26']


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
async def test_star_toggle_defers_before_slow_discord_calls(modules, monkeypatch):
    v = modules.views
    order = []
    inter = interaction(NS(id=60))
    inter.response.defer.side_effect = lambda **kw: order.append('defer')

    async def slow_create(guild, member, data):
        order.append('create')
        return {'channels': [], 'category_id': '1', 'index_channel_id': '2'}

    monkeypatch.setattr(v, 'get_or_create_favorites_category', slow_create)
    monkeypatch.setattr(v, 'rebuild_favorites_index', AsyncMock())
    await v.ChannelSettingsView().toggle_star.callback(inter)
    assert order == ['defer', 'create']
    assert 'お気に入りに登録' in inter.followup.send.call_args.args[0]
    inter.response.send_message.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
@pytest.mark.parametrize('fails', [False, True])
async def test_rename_defers_and_reports_rate_limit(modules, monkeypatch, fails):
    v = modules.views
    channel = NS(id=60, category=None, edit=AsyncMock(side_effect=http_error() if fails else None))
    monkeypatch.setattr(v, 'get_existing_channel', AsyncMock(return_value=channel))
    modal = v.RenameModal(); modal.new_name._value = 'My Diary'
    inter = interaction()
    await modal.on_submit(inter)
    inter.response.defer.assert_awaited_once()
    text = inter.followup.send.call_args.args[0]
    assert ('10分間に2回' in text) is fails
    assert ('my-diary' in text) is not fails


@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
def test_channel_manager_saves_leave_no_temp_files(modules, tmp_path):
    modules.storage.save_data({'7': '60'})
    modules.storage.save_hidden_data({})
    modules.storage.save_starred_data({})
    assert modules.storage.load_data() == {'7': '60'}
    assert not list(modules.storage.DATA_FILE.resolve().parent.glob('.*.json.*'))


@pytest.mark.parametrize('modules', ['stage_bot'], indirect=True)
def test_stage_corrupt_json_is_preserved_before_defaults(modules):
    storage = modules.storage
    path = storage.DATA_DIR / 'broken.json'
    path.write_text('{"1": ', encoding='utf-8')
    assert storage.load_json(path, {}) == {}
    backups = list(storage.DATA_DIR.glob('broken.json.corrupt-*'))
    assert len(backups) == 1 and backups[0].read_text(encoding='utf-8') == '{"1": '
    storage.save_json(path, {'ok': True})
    assert json.loads(path.read_text(encoding='utf-8')) == {'ok': True}
    assert not list(storage.DATA_DIR.glob('.broken.json.*'))
