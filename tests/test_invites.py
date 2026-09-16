"""招待APIはすべてモックし、審査・永続化・申請者への配送を検証する。"""
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
    key, record = service.new_request(9, 7, 'friend', '大学の友人です')
    service.transition(key, {'submitting'}, 'pending', message_id=55)
    return service, key


def api_setup(m, monkeypatch):
    service, key = pending(m)
    channel = NS(id=8, permissions_for=lambda me: NS(create_instant_invite=True))
    guild = NS(me=NS(guild_permissions=NS(manage_guild=False)), text_channels=[channel])
    monkeypatch.setattr(service.bot, 'get_guild', lambda gid: guild)
    api = NS(create=AsyncMock(return_value={'code': 'safe-code', 'max_uses': 1, 'max_age': 86400}), delete=AsyncMock())
    class Context:
        async def __aenter__(self): return api
        async def __aexit__(self, *args): pass
    monkeypatch.setattr(service, 'InviteAPI', Context)
    return service, key, api


async def test_required_name_and_relationship_without_user_id(modules, monkeypatch):
    service = importlib.import_module('invitations')
    fetch = AsyncMock(); monkeypatch.setattr(service.bot, 'fetch_user', fetch)
    for name, reason in [('friend', ' '), (' ', '大学の友人')]:
        modal = modules.views.InviteModal()
        modal.name._value, modal.reason._value = name, reason
        await modal.on_submit(interaction())
        assert not modules.storage.load_invite_requests()
    assert modal.reason.required and modal.name.required
    assert len(modal.children) == 2
    fetch.assert_not_awaited()


async def test_submission_persists_review_and_never_creates_invite(modules, monkeypatch):
    service = importlib.import_module('invitations')
    message = NS(id=55, edit=AsyncMock())
    admin = NS(send=AsyncMock(return_value=message))
    fetch = AsyncMock(return_value=admin)
    monkeypatch.setattr(service.bot, 'fetch_user', fetch)
    issue = AsyncMock(); monkeypatch.setattr(modules.views, 'issue_invite', issue)
    modal = modules.views.InviteModal()
    modal.name._value, modal.reason._value = 'friend', '同じ大学の友人'
    await modal.on_submit(interaction())
    record = next(iter(modules.storage.load_invite_requests().values()))
    assert record['state'] == 'pending' and record['invitee_name'] == 'friend'
    assert 'target_id' not in record
    fetch.assert_awaited_once_with(99)
    assert 'view' not in admin.send.call_args.kwargs
    assert isinstance(message.edit.call_args.kwargs['view'], modules.views.ApprovalView)
    issue.assert_not_awaited()


async def test_admin_message_failure_cannot_bypass_review(modules, monkeypatch):
    service = importlib.import_module('invitations')
    admin = NS(send=AsyncMock(side_effect=http_error()))
    monkeypatch.setattr(service.bot, 'fetch_user', AsyncMock(return_value=admin))
    modal = modules.views.InviteModal()
    modal.name._value, modal.reason._value = 'friend', '大学の友人'
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
    api.create.assert_awaited_once_with(8, key)
    record = modules.storage.load_invite_requests()[key]
    assert record['state'] == 'ready' and record['reviewer_id'] == 99


@pytest.mark.parametrize('failure', ['timeout', 'wrong_limits', 'permission', 'save'])
async def test_issue_failures_never_release_link_or_retry(modules, monkeypatch, failure):
    service, key, api = api_setup(modules, monkeypatch)
    if failure == 'timeout': api.create.side_effect = TimeoutError()
    elif failure == 'wrong_limits': api.create.return_value['max_uses'] = 0
    elif failure == 'permission': monkeypatch.setattr(service.bot, 'get_guild', lambda gid: None)
    record = service.decide(key, 99, 55, True)
    if failure == 'save': monkeypatch.setattr(service, 'transition', Mock(side_effect=OSError('disk full')))
    with pytest.raises(Exception): await service.issue_invite(key, record)
    persisted = modules.storage.load_invite_requests()[key]
    assert persisted['state'] in {'failed', 'issuing'}
    assert 'https://' not in service.status_text(key, persisted)
    with pytest.raises(service.InviteError): service.decide(key, 99, 55, True)
    if failure in {'wrong_limits', 'save'}: api.delete.assert_awaited_once_with('safe-code')
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
    with pytest.raises(service.InviteError): service.new_request(9, 7, 'friend', '説明')


@pytest.mark.parametrize('applicant', [True, False])
async def test_delivery_only_to_applicant_without_private_review_details(modules, monkeypatch, applicant):
    service, key, api = api_setup(modules, monkeypatch)
    record = await service.issue_invite(key, service.decide(key, 99, 55, True))
    send = AsyncMock(return_value=applicant); monkeypatch.setattr(service, 'send_dm', send)
    assert await service.deliver_invite(key, record) is applicant
    send.assert_awaited_once()
    recipient, message = send.call_args.args
    assert recipient == 7
    assert 'https://discord.gg/safe-code' in message and '1回限り' in message
    assert record['invitee_name'] in message
    assert record['relationship'] not in message and key not in message
    assert modules.storage.load_invite_requests()[key]['delivery'] == ('applicant_sent' if applicant else 'applicant_failed')
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


async def test_rest_payload_is_single_use_and_expiring_without_roles(modules):
    service = importlib.import_module('invitations')
    api = service.InviteAPI(); api.request = AsyncMock()
    await api.create(8, 'request')
    call = api.request.call_args
    assert call.args == ('POST', '/channels/8/invites')
    assert call.kwargs['json'] == dict(max_uses=1, max_age=86400, unique=True, temporary=False)


async def test_legacy_pending_requires_resubmission(modules):
    service, key = pending(modules)
    with modules.storage.edit_invite_requests() as records:
        record = records[key]
        record.pop('flow')
        record['target_id'] = TARGET
        record['username'] = record.pop('invitee_name')
    with pytest.raises(service.InviteError, match='再申請'):
        service.decide(key, 99, 55, True)
    assert modules.storage.load_invite_requests()[key]['state'] == 'pending'
    new_key, _ = service.new_request(9, 7, 'friend', '大学の友人')
    assert new_key != key


async def test_legacy_ready_link_retains_restriction_label(modules):
    service = importlib.import_module('invitations')
    record = dict(username='old', target_id=TARGET, state='ready', expires_at=time.time()+100, code='old')
    assert '対象ID本人のみ' in service.status_text('legacy', record)


async def test_nonadmin_button_cannot_call_issue(modules, monkeypatch):
    service, key = pending(modules)
    issue = AsyncMock(); monkeypatch.setattr(modules.views, 'issue_invite', issue)
    await modules.views.ApprovalView(key).approve.callback(interaction())
    issue.assert_not_awaited()
    assert modules.storage.load_invite_requests()[key]['state'] == 'pending'


@pytest.mark.parametrize('status', [403, 429, 500])
async def test_rest_errors_do_not_retry_post(modules, status):
    service = importlib.import_module('invitations')
    api = service.InviteAPI()
    class Response:
        async def __aenter__(self): return NS(status=status)
        async def __aexit__(self, *args): pass
    api.session = NS(request=Mock(return_value=Response()))
    with pytest.raises(service.InviteError): await api.create(8, 'request')
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
    assert any('friend' in field['value'] for field in fields)
    assert any('大学の友人' in field['value'] for field in fields)
    assert any('99' in field['value'] for field in fields)
    await view.approve.callback(inter)
    await view.reject.callback(inter)
    api.create.assert_awaited_once()
    assert modules.storage.load_invite_requests()[key]['state'] == 'ready'
