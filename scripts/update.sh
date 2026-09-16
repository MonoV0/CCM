#!/usr/bin/env bash
# Run through ccm-update.service: independent of the Bot's systemd cgroup.
set -Eeuo pipefail
umask 077
export GIT_TERMINAL_PROMPT=0

repo_dir="$(git rev-parse --show-toplevel)"
cd "$repo_dir"
exec 9>"$(git rev-parse --git-path ccm-update.lock)"
flock -n 9 || { echo 'An update is already running.' >&2; exit 1; }

[[ "$(git branch --show-current)" == main ]] || {
    echo 'Refusing update: checkout must be on main.' >&2; exit 1;
}
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || {
    echo 'Refusing update: tracked files have local changes. No changes were discarded.' >&2; exit 1;
}
[[ -x "$repo_dir/.venv/bin/python" ]] || {
    echo 'Missing .venv/bin/python. Follow the Raspberry Pi setup instructions.' >&2; exit 1;
}

restart_needed=0
recover() {
    result=$?
    trap - EXIT
    if [[ "$restart_needed" == 1 ]]; then
        echo 'Update interrupted/failed; attempting to start the Bot with the current checkout.' >&2
        systemctl --user start ccm-bot.service || true
    fi
    exit "$result"
}
trap recover EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Mark before stop so that a stop timeout also attempts recovery.
restart_needed=1
systemctl --user stop ccm-bot.service
# Git never resets local data or creates a merge commit.
timeout 120 git pull --ff-only origin main
[[ "$(git rev-parse HEAD)" == "$(git rev-parse FETCH_HEAD)" ]] || {
    echo 'Local main contains commits absent from origin/main. Resolve manually.' >&2; exit 1;
}
timeout 300 "$repo_dir/.venv/bin/python" -m pip install --disable-pip-version-check -r requirements.txt
"$repo_dir/.venv/bin/python" -m pip check
systemctl --user start ccm-bot.service
restart_needed=0
sleep 5
systemctl --user is-active --quiet ccm-bot.service
printf 'Bot service started at commit %s. Check its startup DM and journal for Discord readiness.\n' "$(git rev-parse --short HEAD)"
