"""運用者が編集する Discord のロール・カテゴリ設定。起動時に検証する。"""
import json
from pathlib import Path

SETTINGS_FILE = Path(__file__).with_name("discord_settings.json")


def _nonempty_name(value, path, *, limit=100):
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{SETTINGS_FILE}: {path} は空白を除いた名前を指定してください")
    if len(value) > limit:
        raise ValueError(f"{SETTINGS_FILE}: {path} は {limit} 文字以内にしてください")
    return value


def load_discord_settings(path=SETTINGS_FILE):
    with Path(path).open(encoding="utf-8") as stream:
        settings = json.load(stream)
    if not isinstance(settings, dict):
        raise ValueError(f"{path}: 設定は JSON オブジェクトにしてください")
    roles = settings.get("roles")
    categories = settings.get("categories")
    if not isinstance(roles, dict) or not isinstance(categories, dict):
        raise ValueError(f"{path}: roles と categories が必要です")
    for key in ("member", "ex_member"):
        _nonempty_name(roles.get(key), f"roles.{key}")
    for key in ("welcome", "external"):
        _nonempty_name(categories.get(key), f"categories.{key}")
    grades = categories.get("grades")
    if not isinstance(grades, dict) or not 1 <= len(grades) <= 12:
        raise ValueError(f"{path}: categories.grades は 1～12 件指定してください")
    for grade, category in grades.items():
        _nonempty_name(grade, "categories.grades の学年名", limit=80)
        _nonempty_name(category, f"categories.grades.{grade}")
    all_categories = [categories["welcome"], categories["external"], *grades.values()]
    if len(all_categories) != len(set(all_categories)):
        raise ValueError(f"{path}: カテゴリ名が重複しています")
    if roles["member"] == roles["ex_member"]:
        raise ValueError(f"{path}: 参加ロール名が重複しています")
    return settings


SETTINGS = load_discord_settings()
ROLE_NAMES = SETTINGS["roles"]
WELCOME_CATEGORY = SETTINGS["categories"]["welcome"]
EXTERNAL_CATEGORY = SETTINGS["categories"]["external"]
GRADE_CATEGORIES = SETTINGS["categories"]["grades"]


def personal_category_names():
    return [*GRADE_CATEGORIES.values(), EXTERNAL_CATEGORY]


def get_role(guild, key):
    """設定キーに対応する既存ロールを名前で探す。"""
    return next((role for role in guild.roles if role.name == ROLE_NAMES[key]), None)
