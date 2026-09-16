"""招待審査の永続状態と、申請者が転送する招待API。

招待は管理者承認後のみ発行し、申請者へ送る。
POSTは再試行しない。不明な発行結果は issuing/failed のまま人が調査する。
"""
import re
import time
import uuid
from urllib.parse import quote

import aiohttp
import discord

from bot_core import bot, ADMIN_ID, TOKEN, logger
from storage import edit_invite_requests, load_invite_requests

INVITE_MAX_AGE = 86400
REVIEW_MAX_AGE = 7 * 86400


class InviteError(Exception):
    pass


def new_request(guild_id, applicant_id, invitee_name, relationship):
    now = time.time()
    request_id = uuid.uuid4().hex
    record = dict(
        guild_id=guild_id, applicant_id=applicant_id, invitee_name=invitee_name,
        relationship=relationship, flow="forward_v1",
        state="submitting", created_at=now, review_expires_at=now + REVIEW_MAX_AGE,
    )
    with edit_invite_requests() as data:
        for existing in data.values():
            if existing.get("flow") != "forward_v1" and existing["state"] in {"pending", "submitting"}:
                continue  # 旧方式の未発行申請は再申請可能。発行中・不明な結果は除外しない。
            if (existing["guild_id"] != guild_id or existing["applicant_id"] != applicant_id
                    or existing.get("invitee_name", existing.get("username", "")).casefold() != invitee_name.casefold()):
                continue
            # 不明な発行結果は経過時間に関係なく管理者による調査が必要。
            active = existing["state"] in {"issuing", "failed"}
            active |= existing["state"] in {"submitting", "pending"} and existing["review_expires_at"] > now
            active |= existing["state"] == "ready" and existing["expires_at"] > now
            if active:
                raise InviteError("同じ名前への処理中・有効な申請があります。管理者に確認してください。")
        data[request_id] = record
    return request_id, record


def transition(request_id, expected, state, **fields):
    with edit_invite_requests() as data:
        record = data.get(request_id)
        if record is None or record["state"] not in expected:
            raise InviteError("この申請は処理済み、または処理中です。再発行は行いません。")
        record.update(state=state, **fields)
        return dict(record)


def decide(request_id, actor_id, message_id, approved):
    if actor_id != ADMIN_ID:
        raise InviteError("管理者のみ操作できます。")
    with edit_invite_requests() as data:
        record = data.get(request_id)
        if not record or record["state"] != "pending" or record.get("message_id") != message_id:
            raise InviteError("この申請は処理済み、または無効です。")
        if record.get("flow") != "forward_v1":
            raise InviteError("旧方式の申請です。/invite から再申請してください。")
        if record["review_expires_at"] <= time.time():
            raise InviteError("申請の審査期限（7日）が切れています。再申請してください。")
        record.update(state="issuing" if approved else "rejected", reviewer_id=actor_id, decided_at=time.time())
        return dict(record)


def request_embed(request_id, record):
    embed = discord.Embed(title="📨 招待申請", color=0x5865F2)
    embed.add_field(name="申請者", value=f'<@{record["applicant_id"]}>（ID: {record["applicant_id"]}）', inline=False)
    embed.add_field(name="招待対象", value=discord.utils.escape_markdown(record.get("invitee_name", record.get("username", "不明"))), inline=False)
    embed.add_field(name="人物・関係性", value=discord.utils.escape_markdown(record["relationship"]), inline=False)
    embed.add_field(name="状態", value=record["state"], inline=False)
    embed.set_footer(text=f"申請ID: {request_id} / 審査期限7日 / 承認後は申請者がリンクを転送")
    return embed


class InviteAPI:
    """公開REST APIのみを使用。エラーにトークン・応答本文・招待URLを含めない。"""
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={"Authorization": f"Bot {TOKEN}"},
            timeout=aiohttp.ClientTimeout(total=20),
        )
        return self

    async def __aexit__(self, *args):
        await self.session.close()

    async def request(self, method, path, **kwargs):
        async with self.session.request(method, "https://discord.com/api/v10" + path, allow_redirects=False, **kwargs) as response:
            if method == "DELETE" and response.status == 404:
                return None
            if not 200 <= response.status < 300:
                raise InviteError(f"Discord APIでエラー（HTTP {response.status}）。再発行せず停止しました。")
            if response.status == 204:
                return None
            return await response.json()

    async def create(self, channel_id, request_id):
        # discord.py内部のPOST再試行による重複発行を避けるため、この発行は1回だけ行う。
        return await self.request("POST", f"/channels/{channel_id}/invites",
                                  json=dict(max_uses=1, max_age=INVITE_MAX_AGE, unique=True, temporary=False),
                                  headers={"X-Audit-Log-Reason": quote(f"CCM invite {request_id}")})

    async def delete(self, code):
        await self.request("DELETE", f"/invites/{code}")


async def issue_invite(request_id, record):
    code = None
    try:
        guild = bot.get_guild(record["guild_id"])
        if guild is None or guild.me is None:
            raise InviteError("Botのサーバー接続を確認してください。")
        channel = next((ch for ch in guild.text_channels if ch.permissions_for(guild.me).create_instant_invite), None)
        if channel is None:
            raise InviteError("招待を作成できるテキストチャンネルがありません。")
        async with InviteAPI() as api:
            result = await api.create(channel.id, request_id)
            code = result.get("code")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", code):
                code = None
                raise InviteError("招待コードを確認できませんでした。")
            # 公開前に記録し、クラッシュ時にも調査・削除できるようにする。
            transition(request_id, {"issuing"}, "issuing", code=code)
            if result.get("max_uses") != 1 or result.get("max_age") != INVITE_MAX_AGE:
                raise InviteError("招待の利用回数・有効期限が要求と一致しません。")
            # 発行開始時点から数え、Discord側より保守的な期限にする。
            expires_at = record["decided_at"] + INVITE_MAX_AGE
            return transition(request_id, {"issuing"}, "ready", expires_at=expires_at, delivery="pending")
    except Exception:
        revoked = False
        if code:
            try:
                async with InviteAPI() as api:
                    await api.delete(code)
                revoked = True
            except Exception:
                logger.error("招待 %s の無効化を確認できません。管理者の調査が必要です。", request_id)
        # 書き込み自体が失敗しても issuing のままで再実行されない。
        transition(request_id, {"issuing"}, "failed", revoked=revoked)
        raise


async def send_dm(user_id, content):
    try:
        user = await bot.fetch_user(user_id)
        await user.send(content, allowed_mentions=discord.AllowedMentions.none())
        return True
    except discord.HTTPException:
        return False


def status_text(request_id, record):
    name = discord.utils.escape_markdown(record.get("invitee_name", record.get("username", "不明")))
    text = f'申請ID: `{request_id}`\n招待対象: {name}\n状態: {record["state"]}'
    if record.get("flow") != "forward_v1" and record["state"] in {"pending", "submitting"}:
        return text + "\n旧方式の申請です。/invite から再申請してください。"
    if record["state"] == "ready":
        if record["expires_at"] <= time.time():
            return text + "\n招待の有効期限が切れています。再申請してください。"
        restriction = "対象ID本人のみ・" if record.get("target_id") else ""
        text += f'\n配送: {record.get("delivery", "pending")}\nhttps://discord.gg/{record["code"]}\n{restriction}1回限り。期限: <t:{int(record["expires_at"])}:F>（使用済みの場合は無効）'
    elif record["state"] in {"failed", "issuing", "submitting"}:
        text += "\n処理中、または安全のため停止しています。管理者に確認してください。"
    elif record["state"] == "pending" and record["review_expires_at"] <= time.time():
        text += "\n審査期限切れです。再申請してください。"
    return text


def forwarding_message(record):
    return (
        f'{discord.utils.escape_markdown(record["invitee_name"])}さん、CCMへの招待が承認されました！\n次のリンクから参加してください。\n'
        f'https://discord.gg/{record["code"]}\n'
        f'この招待は1回限り、<t:{int(record["expires_at"])}:F>まで有効です。\n'
        "ほかの方には共有しないでください。参加後はサーバー内の案内に沿って手続きしてください。"
    )


async def deliver_invite(request_id, record):
    # 人物説明・申請IDを含めず、そのまま相手に転送できるメッセージを1通送る。
    notified = await send_dm(record["applicant_id"], forwarding_message(record))
    transition(request_id, {"ready"}, "ready", delivery="applicant_sent" if notified else "applicant_failed")
    return notified


def restore_approval_views(view_class):
    # 再接続では再登録しない。issuingは絶対に再発行しない。
    if getattr(bot, "_invite_views_restored", False):
        return
    for request_id, record in load_invite_requests().items():
        if record["state"] == "pending" and record["review_expires_at"] > time.time():
            bot.add_view(view_class(request_id), message_id=record["message_id"])
        elif record["state"] in {"issuing", "failed", "submitting"}:
            logger.warning("招待 %s は %s。管理者の確認が必要です。", request_id, record["state"])
    bot._invite_views_restored = True
