"""招待APIはすべてモックし、審査・永続化・対象制限・配送を検証する。"""
import asyncio
import importlib
import json
import time
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from test_regressions import modules, interaction, http_error  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)]
TARGET = 123456789012345678


def pending(m):
    service = importlib.import_module('invitations')
    key, record = service.new_request(9, 7, NS(id=TARGET, name='friend'), 'friend', '大学の友人です')
    service.transition(key, {'submitting'}, 'pending', message_id=55)
    return service, key


def api_setup(m, monkeypatch):
    service, key = pending(m)
    channel = NS(id=8, permissions_for=lambda me: NS(create_instant_invite=True))
    guild = NS(me=NS(guild_permissions=NS(manage_guild=True)), text_channels=[channel])
    monkeypatch.setattr(service.bot, 'get_guild', lambda gid: guild)
    api = NS(create=AsyncMock(return_value={'code': 'safe-code', 'max_uses': 1, 'max_age': 86400}), verify=AsyncMock(), delete=AsyncMock())
    class Context:
        async def __aenter__(self): return api
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(service, 'TargetedInviteAPI', Context)
    return service, key, api


async def test_required_input_and_username_not_identity(modules, monkeypatch):
    service = importlib.import_module('invitations')
    fetch = AsyncMock(return_value=NS(id=TARGET, name='different', bot=False))
    monkeypatch.setattr(service.bot, 'fetch_user', fetch)
    for name, reason, user_id in [('friend', ' ', str(TARGET)), ('friend', '大学の友人', 'not-an-id'), ('friend', '大学の友人', str(TARGET))]:
        modal = modules.views.InviteModal()
        modal.name._value, modal.reason._value, modal.user_id._value = name, reason, user_id
        await modal.on_submit(interaction())
        assert not modules.storage.load_invite_requests()
    assert modal.reason.required and modal.user_id.required
    assert fetch.await_count == 1


async def test_submission_persists_review_and_never_creates_invite(modules, monkeypatch):
    service = importlib.import_module('invitations')
    message = NS(id=55, edit=AsyncMock())
    admin = NS(send=AsyncMock(return_value=message))
    fetch = AsyncMock(side_effect=[NS(id=TARGET, name='friend', bot=False), admin])
    monkeypatch.setattr(service.bot, 'fetch_user', fetch)
    issue = AsyncMock(); monkeypatch.setattr(modules.views, 'issue_invite', issue)
    modal = modules.views.InviteModal()
    modal.name._value, modal.reason._value, modal.user_id._value = 'friend', '同じ大学の友人', str(TARGET)
    await modal.on_submit(interaction())
    record = next(iter(modules.storage.load_invite_requests().values()))
    assert record['state'] == 'pending' and record['target_id'] == TARGET
    assert 'view' not in admin.send.call_args.kwargs
    assert isinstance(message.edit.call_args.kwargs['view'], modules.views.ApprovalView)
    issue.assert_not_awaited()


async def test_admin_message_failure_cannot_bypass_review(modules, monkeypatch):
    service = importlib.import_module('invitations')
    admin = NS(send=AsyncMock(side_effect=http_error()))
    monkeypatch.setattr(service.bot, 'fetch_user', AsyncMock(side_effect=[NS(id=TARGET, name='friend', bot=False), admin]))
    modal = modules.views.InviteModal()
    modal.name._value, modal.reason._value, modal.user_id._value = 'friend', '大学の友人', str(TARGET)
    await modal.on_submit(interaction())
    key, record = next(iter(modules.storage.load_invite_requests().items()))
    assert record['state'] == 'submitting'
    with pytest.raises(service.InviteError): service.decide(key, 99, 55, True)


@pytest.mark.parametrize('actor,message,age', [(7, 55, 0), (99, 56, 0), (99, 55, -1)])
async def test_invalid_review_is_closed(modules, actor, message, age):
    service, key = pending(modules)
    if age: service.transition(key, {'pending'}, 'pending', review_expires_at=time.time() - 1)
    with pytest.raises(service.InviteError): service.decide(key, actor, message, True)
    assert modules.storage.load_invite_requests()[key]['state'] == 'pending'


async def test_reject_is_final_and_replay_blocked(modules):
    service, key = pending(modules)
    assert service.decide(key, 99, 55, False)['state'] == 'rejected'
    with pytest.raises(service.InviteError): service.decide(key, 99, 55, True)


async def test_concurrent_approvals_create_once(modules, monkeypatch):
    service, key, api = api_setup(modules, monkeypatch)
    async def approve():
        try: record = service.decide(key, 99, 55, True)
        except service.InviteError: return
        return await service.issue_invite(key, record)
    results = await asyncio.gather(approve(), approve(), approve())
    assert sum(r is not None for r in results) == 1
    api.create.assert_awaited_once_with(8, TARGET, key)
    api.verify.assert_awaited_once_with('safe-code', TARGET)
    record = modules.storage.load_invite_requests()[key]
    assert record['state'] == 'ready' and record['reviewer_id'] == 99


@pytest.mark.parametrize('failure', ['timeout', 'restriction', 'wrong_limits', 'permission', 'save'])
async def test_issue_failures_never_release_link_or_retry(modules, monkeypatch, failure):
    service, key, api = api_setup(modules, monkeypatch)
    if failure == 'timeout': api.create.side_effect = TimeoutError()
    elif failure == 'restriction': api.verify.side_effect = service.InviteError('unverified')
    elif failure == 'wrong_limits': api.create.return_value['max_uses'] = 0
    elif failure == 'permission': monkeypatch.setattr(service.bot, 'get_guild', lambda gid: None)
    record = service.decide(key, 99, 55, True)
    if failure == 'save': monkeypatch.setattr(service, 'transition', Mock(side_effect=OSError('disk full')))
    with pytest.raises(Exception): await service.issue_invite(key, record)
    persisted = modules.storage.load_invite_requests()[key]
    assert persisted['state'] in {'failed', 'issuing'}
    assert 'https://' not in service.status_text(key, persisted)
    with pytest.raises(service.InviteError): service.decide(key, 99, 55, True)
    if failure in {'restriction', 'wrong_limits', 'save'}: api.delete.assert_awaited_once_with('safe-code')
    assert api.create.await_count <= 1


async def test_restart_restores_pending_not_issuing(modules, monkeypatch):
    service, key = pending(modules)
    add = Mock(); monkeypatch.setattr(service.bot, 'add_view', add)
    service.restore_approval_views(modules.views.ApprovalView)
    service.restore_approval_views(modules.views.ApprovalView)
    add.assert_called_once()
    view = add.call_args.args[0]
    assert view.is_persistent() and add.call_args.kwargs['message_id'] == 55
    assert key in view.approve.custom_id
    service.decide(key, 99, 55, True)
    monkeypatch.setattr(service.bot, '_invite_views_restored', False)
    add.reset_mock()
    importlib.reload(service)
    service.restore_approval_views(modules.views.ApprovalView)
    add.assert_not_called()
    with pytest.raises(service.InviteError): service.decide(key, 99, 55, True)
    with pytest.raises(service.InviteError): service.new_request(9, 7, NS(id=TARGET, name='friend'), 'friend', '説明')


@pytest.mark.parametrize('direct,applicant', [(True, True), (False, True), (False, False)])
async def test_delivery_and_same_link_fallback(modules, monkeypatch, direct, applicant):
    service, key, api = api_setup(modules, monkeypatch)
    record = await service.issue_invite(key, service.decide(key, 99, 55, True))
    send = AsyncMock(side_effect=[direct, applicant]); monkeypatch.setattr(service, 'send_dm', send)
    assert await service.deliver_invite(key, record) == (direct, applicant)
    assert send.call_args_list[0].args[0] == TARGET
    assert 'https://discord.gg/safe-code' in send.call_args_list[0].args[1]
    assert ('https://' in send.call_args_list[1].args[1]) is (not direct)
    assert modules.storage.load_invite_requests()[key]['delivery'] == ('sent' if direct else 'fallback')
    api.create.assert_awaited_once()


@pytest.mark.parametrize('viewer,guild_id,allowed', [(7, 9, True), (99, 9, True), (8, 9, False), (7, 10, False)])
async def test_status_is_private_and_authorized(modules, monkeypatch, viewer, guild_id, allowed):
    service, key, api = api_setup(modules, monkeypatch)
    await service.issue_invite(key, service.decide(key, 99, 55, True))
    inter = interaction(); inter.user.id = viewer; inter.guild.id = guild_id
    await modules.commands.invite_status.callback(inter, key)
    call = inter.response.send_message.call_args
    assert ('https://' in call.args[0]) is allowed
    assert call.kwargs['ephemeral']
    api.create.assert_awaited_once()


async def test_expired_link_not_disclosed(modules):
    service, key = pending(modules)
    record = service.transition(key, {'pending'}, 'ready', code='old', expires_at=time.time() - 1)
    assert 'https://' not in service.status_text(key, record)


async def test_corrupt_storage_and_atomic_save_failure(modules, monkeypatch):
    service, key = pending(modules)
    original = modules.storage.INVITE_DATA_FILE.read_text()
    with monkeypatch.context() as patch:
        patch.setattr(modules.storage.os, 'replace', Mock(side_effect=OSError('disk full')))
        with pytest.raises(OSError): service.decide(key, 99, 55, True)
    assert modules.storage.INVITE_DATA_FILE.read_text() == original
    modules.storage.INVITE_DATA_FILE.write_text('{broken')
    with pytest.raises(json.JSONDecodeError): service.decide(key, 99, 55, True)


@pytest.mark.parametrize('status,csv_text,valid', [(2, f'user_id\n{TARGET}\n', True), (2, 'user_id\n999\n', False), (2, f'user_id\n{TARGET}\n999\n', False), (3, '', False), (0, '', False), (1, '', False)])
async def test_target_restriction_verified_exactly(modules, monkeypatch, status, csv_text, valid):
    service = importlib.import_module('invitations')
    api = service.TargetedInviteAPI()
    async def request(method, path): return csv_text if path.endswith('/target-users') else {'status': status}
    api.request = AsyncMock(side_effect=request)
    monkeypatch.setattr(service.asyncio, 'sleep', AsyncMock())
    if valid: await api.verify('code', TARGET)
    else:
        with pytest.raises(service.InviteError): await api.verify('code', TARGET)
    assert api.request.await_count <= 5


async def test_rest_payload_is_targeted_without_role_grants(modules):
    service = importlib.import_module('invitations')
    api = service.TargetedInviteAPI(); api.request = AsyncMock()
    await api.create(8, TARGET, 'request')
    call = api.request.call_args
    assert call.args == ('POST', '/channels/8/invites')
    fields = {field[0]['name']: field[2] for field in call.kwargs['data']._fields}
    assert fields['target_users_file'] == str(TARGET).encode()
    assert json.loads(fields['payload_json']) == dict(max_uses=1, max_age=86400, unique=True, temporary=False)


async def test_nonadmin_button_cannot_call_issue(modules, monkeypatch):
    service, key = pending(modules)
    issue = AsyncMock(); monkeypatch.setattr(modules.views, 'issue_invite', issue)
    await modules.views.ApprovalView(key).approve.callback(interaction())
    issue.assert_not_awaited()
    assert modules.storage.load_invite_requests()[key]['state'] == 'pending'


@pytest.mark.parametrize('status', [403, 429, 500])
async def test_rest_errors_do_not_retry_post(modules, status):
    service = importlib.import_module('invitations')
    api = service.TargetedInviteAPI()
    class Response:
        async def __aenter__(self): return NS(status=status)
        async def __aexit__(self, *args): pass
    api.session = NS(request=Mock(return_value=Response()))
    with pytest.raises(service.InviteError): await api.create(8, TARGET, 'request')
    api.session.request.assert_called_once()
    assert api.session.request.call_args.kwargs['allow_redirects'] is False


async def test_approval_keeps_audit_and_rejects_replay(modules, monkeypatch):
    service, key, api = api_setup(modules, monkeypatch)
    monkeypatch.setattr(service, 'send_dm', AsyncMock(return_value=True))
    inter = interaction(); inter.user.id = 99
    inter.message = NS(id=55, edit=AsyncMock())
    view = modules.views.ApprovalView(key)
    await view.approve.callback(inter)
    edit = inter.message.edit.call_args.kwargs
    assert edit['view'] is None
    fields = edit['embed'].to_dict()['fields']
    assert any(str(TARGET) in field['value'] for field in fields)
    assert any('大学の友人' in field['value'] for field in fields)
    assert any('99' in field['value'] for field in fields)
    await view.approve.callback(inter)
    await view.reject.callback(inter)
    api.create.assert_awaited_once()
    assert modules.storage.load_invite_requests()[key]['state'] == 'ready'
