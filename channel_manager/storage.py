"""
永続化まわり: channel_data / hidden_channels / starred_channels のJSON読み書きと、
グレード（学年）カテゴリなどの静的な設定値。
"""
import json
from pathlib import Path

DATA_FILE = Path("channel_data.json")
HIDDEN_DATA_FILE = Path("hidden_channels.json")
STARRED_DATA_FILE = Path("starred_channels.json")
ERROR_LOG_FILE = Path("bot_errors.log")

HIDDEN_RETENTION_DAYS = 30

def load_data() -> dict:
    if not DATA_FILE.exists():
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_hidden_data() -> dict:
    """{channel_id(str): {"user_id": str, "hidden_at": iso8601}} を保持する"""
    if not HIDDEN_DATA_FILE.exists():
        return {}
    with open(HIDDEN_DATA_FILE, "r") as f:
        return json.load(f)

def save_hidden_data(data: dict):
    with open(HIDDEN_DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def load_starred_data() -> dict:
    """{user_id(str): {"channels": [channel_id(str), ...], "category_id": str|None, "index_channel_id": str|None}} を保持する"""
    if not STARRED_DATA_FILE.exists():
        return {}
    with open(STARRED_DATA_FILE, "r") as f:
        return json.load(f)

def save_starred_data(data: dict):
    with open(STARRED_DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

GRADE_CATEGORIES = {
    "A21": "日報_A21",
    "A22": "日報_A22",
    "A23": "日報_A23",
    "A24": "日報_A24",
    "A25": "日報_A25",
    "A26": "日報_A26",
}

INDEX_CHANNEL_NAME = "📌チャンネル一覧"
# Discordの1カテゴリあたりのチャンネル上限は50。index分を1つ確保して49を上限とする。
MAX_CHANNELS_PER_CATEGORY = 49

RULES_TEXT = """・日報などは自分のチャンネルでお願いします　訪問・コメントは自由です。
・他人に見られてる意識をもち、不快になるような発言・表現は避けてください。
・自分以外のメンバーのチャンネルへの荒らし行為、またはそれに準ずるものは禁止です。
・このサーバーをbotなどの研究環境としての利用は禁止です。
・このサーバー内で起きた個人間のトラブルや問題行為に対して、管理者は責任を取らないものとします。
・ルールに反するような行為が見られる場合、対応処置をとります。
・下記【データの取り扱いについて】も含めて、メンバーロールを取得したことで同意したものとみなします。
・なにかあれば <@421526096048291840> に連絡、もしくはhttps://discord.com/channels/1438017912492392501/1438497518735593524 をご利用ください。"""
