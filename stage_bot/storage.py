"""
永続化まわり: JSON設定の読み込み・保存、guild_config/temp_channels/user_presets。
"""
import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger("stage-bot")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
GUILD_CONFIG_PATH = DATA_DIR / "guild_config.json"
TEMP_CHANNELS_PATH = DATA_DIR / "temp_channels.json"
PRESETS_PATH = DATA_DIR / "presets.json"

DEFAULT_NAME_TEMPLATE = "🎤・{username} のステージ"
DEFAULT_TOPIC_TEMPLATE = "{username} の雑談ステージ"


def load_json(path: Path, default):
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            # 次回の保存で上書きして記録を失わないよう、壊れたファイルは退避してから初期値で続行する。
            backup = path.with_name(f"{path.name}.corrupt-{int(time.time())}")
            try:
                os.replace(path, backup)
                log.error("%s の読み込みに失敗しました。%s に退避し、初期値を使用します。", path, backup)
            except OSError:
                log.exception("%s の読み込みと退避に失敗しました。初期値を使用します。", path)
    return default


def save_json(path: Path, data) -> None:
    """一時ファイルへ書いてから置き換え、書き込み途中の停止でJSONを壊さない。"""
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


guild_config: dict = load_json(GUILD_CONFIG_PATH, {})

# 旧バージョンのデータと互換性を持たせる処理
raw_temp_channels = load_json(TEMP_CHANNELS_PATH, {})
temp_channels: dict = {}
for k, v in raw_temp_channels.items():
    if isinstance(v, int):
        temp_channels[k] = {"guild_id": v, "password": None, "invite_msg_ids": [], "sub_admins": [], "blocked_users": [], "tickets": {}}
    else:
        if "invite_msg_ids" not in v:
            v["invite_msg_ids"] = []
        if "sub_admins" not in v:
            v["sub_admins"] = []
        if "blocked_users" not in v:
            v["blocked_users"] = []
        if "tickets" not in v:
            v["tickets"] = {}
        if "reaction_thread_id" not in v:
            v["reaction_thread_id"] = None
        temp_channels[k] = v

# ユーザーごとのプリセットデータ: {user_id_str: [{name, topic, limit, label}, ...]}
user_presets: dict = load_json(PRESETS_PATH, {})


def save_guild_config() -> None:
    save_json(GUILD_CONFIG_PATH, guild_config)


def save_temp_channels() -> None:
    save_json(TEMP_CHANNELS_PATH, temp_channels)


def save_presets() -> None:
    save_json(PRESETS_PATH, user_presets)
