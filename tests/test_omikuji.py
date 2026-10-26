import importlib
import io
from types import SimpleNamespace as NS

import pytest
from openpyxl import load_workbook
from test_regressions import modules  # noqa: F401


@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
def test_matching_and_excel(modules):
    m = importlib.import_module('omikuji')
    assert m.matches(NS(reactions=[NS(emoji=NS(name='Ni_logo2W'), count=1)]))
    assert m.matches(NS(reactions=[NS(emoji=NS(name='Ni_logo1B'), count=1)]))
    assert not m.matches(NS(reactions=[NS(emoji='👍', count=1)]))
    book = load_workbook(io.BytesIO(m.workbook_bytes(
        [(1, '=危険ではなく本文', 'monovo', 'https://discord.com/channels/1/2/3')], ['42'], 10)))
    assert list(book['おみくじ'].values)[1] == (10, '=危険ではなく本文', 'monovo')
    assert book['おみくじ']['B2'].data_type == 's'
    assert '42' in book['使い方・取得結果']['A6'].value


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
async def test_collection_deduplicates_two_reactions_and_sorts(modules):
    m = importlib.import_module('omikuji')
    def message(mid):
        return NS(id=mid, content='本文', author=NS(name='monovo'), jump_url='url',
                  reactions=[NS(emoji=NS(name=name), count=1) for name in m.EMOJI_NAMES])
    class Channel:
        def history(self, **kwargs):
            assert kwargs == dict(limit=None, oldest_first=True)
            async def stream():
                yield message(2)
                yield message(1)
            return stream()
    progress = {'done': 0}
    rows, errors = await m.collect([Channel()], progress)
    assert [row[0] for row in rows] == [1, 2]
    assert errors == []
    assert progress['done'] == 1


def export_interaction():
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock
    return NS(
        id=123, guild_id=1, created_at=datetime.now(timezone.utc),
        user=NS(id=99), command=NS(name='omikuji_export'),
        guild=NS(id=1, text_channels=[], filesize_limit=10_000_000),
        response=NS(defer=AsyncMock(), send_message=AsyncMock(), is_done=lambda: False),
        followup=NS(send=AsyncMock()),
    )


def response_error(code):
    import discord
    status = 404 if code in (10062, 10008) else 400
    error_type = discord.NotFound if status == 404 else discord.HTTPException
    return error_type(NS(status=status, reason='simulated'), {'code': code, 'message': 'test-private-data'})


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
@pytest.mark.parametrize('code', [10062, 40060])
async def test_rejected_initial_response_does_not_start_or_refresh_job(modules, monkeypatch, caplog, code):
    import asyncio
    from unittest.mock import Mock
    commands = importlib.import_module('omikuji_commands')
    inter = export_interaction()
    inter.response.defer.side_effect = response_error(code)
    existing = {'task': asyncio.get_running_loop().create_future(), 'done': 0, 'total': 1}
    commands.JOBS[1] = existing
    read = Mock()
    monkeypatch.setattr(commands, 'load_data', read)
    monkeypatch.setattr(commands, 'load_hidden_data', read)
    await commands.omikuji_export.callback(inter, refresh=True)
    assert commands.JOBS[1] is existing
    assert not existing['task'].cancelled()
    read.assert_not_called()
    inter.response.defer.assert_awaited_once()
    inter.followup.send.assert_not_awaited()
    assert f'code={code}' in caplog.text
    assert 'response_s=' in caplog.text and 'age_s=' in caplog.text and 'pid=' in caplog.text
    assert 'test-private-data' not in caplog.text
    existing['task'].cancel()


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
async def test_successful_ack_precedes_collection_and_job_creation(modules, monkeypatch):
    from unittest.mock import AsyncMock, Mock
    commands = importlib.import_module('omikuji_commands')
    inter = export_interaction()
    order = []
    async def ack(**kwargs):
        order.append('ack')
    def data():
        assert order == ['ack']
        return {}
    inter.response.defer.side_effect = ack
    monkeypatch.setattr(commands, 'load_data', Mock(side_effect=data))
    monkeypatch.setattr(commands, 'load_hidden_data', Mock(return_value={}))
    monkeypatch.setattr(commands, 'run_job', AsyncMock())
    await commands.omikuji_export.callback(inter)
    await commands.JOBS[1]['task']
    commands.run_job.assert_awaited_once()
    inter.followup.send.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
async def test_other_ack_errors_still_propagate(modules):
    import discord
    commands = importlib.import_module('omikuji_commands')
    inter = export_interaction()
    inter.response.defer.side_effect = response_error(50013)
    with pytest.raises(discord.HTTPException):
        await commands.omikuji_export.callback(inter)
    assert commands.JOBS == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
@pytest.mark.parametrize('code', [10062, 40060])
async def test_global_handler_does_not_resend_rejected_interaction(modules, monkeypatch, code):
    from unittest.mock import AsyncMock
    inter = export_interaction()
    notify = AsyncMock()
    monkeypatch.setattr(modules.events, 'notify_admin_error', notify)
    await modules.events.on_app_command_error(inter, NS(original=response_error(code)))
    notify.assert_not_awaited()
    inter.response.send_message.assert_not_awaited()
    inter.followup.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
async def test_other_notfound_error_keeps_normal_error_reporting(modules, monkeypatch):
    from unittest.mock import AsyncMock
    inter = export_interaction()
    notify = AsyncMock()
    monkeypatch.setattr(modules.events, 'notify_admin_error', notify)
    await modules.events.on_app_command_error(inter, NS(original=response_error(10008)))
    notify.assert_awaited_once()
    inter.response.send_message.assert_awaited_once()
