#!/usr/bin/env bash
# Run through ccm-update.service, outside both independent Bot service cgroups.
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

# Both units must be installed before stopping either Bot.
bot_services=(ccm-bot.service ccm-stage-bot.service)
systemctl --user cat "${bot_services[@]}" >/dev/null

restart_needed=0
recover() {
    result=$?
    trap - EXIT
    if [[ "$restart_needed" == 1 ]]; then
        echo 'Update interrupted/failed; attempting to start both Bots with the current checkout.' >&2
        systemctl --user start "${bot_services[@]}" || true
    fi
    exit "$result"
}
trap recover EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# Mark before stop so that a stop timeout also attempts recovery.
restart_needed=1
systemctl --user stop "${bot_services[@]}"
# Git never resets local data or creates a merge commit.
timeout 120 git pull --ff-only origin main
[[ "$(git rev-parse HEAD)" == "$(git rev-parse FETCH_HEAD)" ]] || {
    echo 'Local main contains commits absent from origin/main. Resolve manually.' >&2; exit 1;
}
timeout 300 "$repo_dir/.venv/bin/python" -m pip install --disable-pip-version-check -r requirements.txt
"$repo_dir/.venv/bin/python" -m pip check
systemctl --user start "${bot_services[@]}"
restart_needed=0
sleep 5
# is-active with multiple units succeeds if ANY unit is active; check each one.
health=0
for service in "${bot_services[@]}"; do
    if ! systemctl --user is-active --quiet "$service"; then
        printf 'Bot service is not active: %s\n' "$service" >&2
        health=1
    fi
done
if [[ "$health" != 0 ]]; then
    exit 1
fi
printf 'Both Bot services started at commit %s. Check their startup DMs and journal for Discord readiness.\n' "$(git rev-parse --short HEAD)"
