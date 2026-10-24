import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock

import aiohttp
import discord
import pytest
from test_regressions import modules  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)]


async def test_transient_failures_retry_with_backoff(modules, monkeypatch):
    events = modules.events
    sync = AsyncMock(side_effect=[aiohttp.ConnectionTimeoutError(), OSError(),
                                discord.HTTPException(NS(status=503, reason='unavailable'), 'retry'), []])
    sleep = AsyncMock()
    monkeypatch.setattr(events.bot.tree, 'sync', sync)
    monkeypatch.setattr(events.asyncio, 'sleep', sleep)
    await events.sync_commands_with_retry()
    assert sync.await_count == 4
    assert [call.args[0] for call in sleep.await_args_list] == [30, 60, 120]
    assert events._commands_synced


async def test_invalid_permissions_do_not_retry(modules, monkeypatch):
    events = modules.events
    sync = AsyncMock(side_effect=discord.Forbidden(NS(status=403, reason='forbidden'), 'denied'))
    sleep = AsyncMock()
    monkeypatch.setattr(events.bot.tree, 'sync', sync)
    monkeypatch.setattr(events.asyncio, 'sleep', sleep)
    await events.sync_commands_with_retry()
    assert not events._commands_synced
    sleep.assert_not_awaited()


async def test_on_ready_initializes_without_waiting_or_duplicating_sync(modules, monkeypatch):
    events = modules.events
    gate = asyncio.Event()
    sync = AsyncMock(side_effect=gate.wait)
    register = Mock()
    monkeypatch.setattr(events, 'restore_approval_views', Mock())
    monkeypatch.setattr(events, 'register_onboarding_views', register)
    monkeypatch.setattr(events.bot, 'add_view', Mock())
    monkeypatch.setattr(events.bot, 'fetch_user', AsyncMock(side_effect=RuntimeError('offline')))
    monkeypatch.setattr(events.cleanup_expired_hidden_channels, 'is_running', Mock(return_value=True))
    monkeypatch.setattr(events, 'sync_commands_with_retry', sync)
    await events.on_ready()
    task = events._command_sync_task
    await events.on_ready()
    assert events._command_sync_task is task
    assert register.call_count == 2
    await asyncio.sleep(0)
    sync.assert_awaited_once()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
