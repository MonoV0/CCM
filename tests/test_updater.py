"""Discordやsystemdに接続せず、管理者認可と更新手順を検証する。"""
import importlib
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from test_regressions import modules, interaction  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
@pytest.mark.parametrize('actor,accepted', [(7, True), (99, True), (99, False), (99, 'missing')])
async def test_update_admin_only_and_reply_before_stop(modules, monkeypatch, actor, accepted):
    inter = interaction(); inter.user.id = actor
    async def dispatch():
        inter.response.send_message.assert_awaited_once()
        if accepted == 'missing': raise FileNotFoundError()
        return accepted
    request = AsyncMock(side_effect=dispatch)
    monkeypatch.setattr(modules.commands, 'request_update', request)
    await modules.commands.update.callback(inter)
    assert request.await_count == (1 if actor == 99 else 0)
    assert inter.response.send_message.call_args.kwargs['ephemeral']
    assert inter.followup.send.await_count == (1 if actor == 99 and accepted in (False, 'missing') else 0)


@pytest.mark.asyncio
@pytest.mark.parametrize('modules', ['channel_manager'], indirect=True)
async def test_dispatch_uses_fixed_systemd_unit(modules, monkeypatch):
    updater = importlib.import_module('updater')
    spawn = AsyncMock(return_value=NS(wait=AsyncMock(return_value=0)))
    monkeypatch.setattr(updater.asyncio, 'create_subprocess_exec', spawn)
    assert await updater.request_update()
    assert spawn.call_args.args == ('systemctl', '--user', 'start', '--no-block', 'ccm-update.service')


@pytest.mark.parametrize('scenario', ['success', 'pull_failure', 'dirty', 'wrong_branch', 'locked', 'pip_failure', 'inactive'])
def test_shell_update_sequence_and_recovery(tmp_path, scenario):
    """実際のシェルを動かし、停止・pull・起動の順番と失敗時の再起動を確認する。"""
    root = Path(__file__).resolve().parents[1]
    repo = tmp_path / 'repo'; repo.mkdir(); (repo / '.git').mkdir()
    bin_dir = tmp_path / 'bin'; bin_dir.mkdir()
    log = tmp_path / 'calls'
    shim = bin_dir / 'shim'
    shim.write_text('''#!/usr/bin/env python3
import os, sys
from pathlib import Path
name = Path(sys.argv[0]).name
args = sys.argv[1:]
scenario = os.environ['SCENARIO']
with open(os.environ['CALLS'], 'a') as f: f.write(name + ' ' + ' '.join(args) + '\\n')
if name == 'git':
    if args == ['rev-parse', '--show-toplevel']: print(os.getcwd())
    elif args == ['rev-parse', '--git-path', 'ccm-update.lock']: print('.git/ccm-update.lock')
    elif args == ['branch', '--show-current']: print('feature' if scenario == 'wrong_branch' else 'main')
    elif args[0] == 'status': print(' M commands.py' if scenario == 'dirty' else '', end='')
    elif args[0] == 'pull' and scenario == 'pull_failure': sys.exit(1)
    else: print('abc123')
elif name == 'flock' and scenario == 'locked': sys.exit(1)
elif name == 'python' and scenario == 'pip_failure': sys.exit(1)
elif name == 'systemctl' and 'is-active' in args and scenario == 'inactive': sys.exit(1)
elif name == 'timeout': os.execvp(args[1], args[1:])
''')
    shim.chmod(0o755)
    for name in ['git', 'systemctl', 'flock', 'timeout', 'sleep']:
        (bin_dir / name).symlink_to(shim)
    (repo / '.venv' / 'bin').mkdir(parents=True)
    (repo / '.venv' / 'bin' / 'python').symlink_to(shim)
    env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'], CALLS=str(log), SCENARIO=scenario)
    result = subprocess.run(['bash', str(root / 'scripts/update.sh')], cwd=repo, env=env, capture_output=True, text=True)
    calls = log.read_text().splitlines()
    stop = 'systemctl --user stop ccm-bot.service'
    start = 'systemctl --user start ccm-bot.service'
    pull = 'git pull --ff-only origin main'
    if scenario in ['dirty', 'wrong_branch', 'locked']:
        assert stop not in calls and pull not in calls and start not in calls
    else:
        assert calls.index(stop) < calls.index(pull) < calls.index(start)
        assert calls.count(start) == 1
    assert (result.returncode == 0) is (scenario == 'success'), result.stderr
