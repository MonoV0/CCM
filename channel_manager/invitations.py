"""招待審査の永続状態と、Discordの対象者限定招待API。

通常の create_invite の target_user は配信視聴用なので使わない。
POSTは再試行しない。不明な発行結果は issuing/failed のまま人が調査する。
"""
import asyncio
import csv
import io
import json
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


def new_request(guild_id, applicant_id, target, username, relationship):
    now = time.time()
    request_id = uuid.uuid4().hex
    record = dict(
        guild_id=guild_id, applicant_id=applicant_id, target_id=target.id,
        username=username, resolved_username=target.name, relationship=relationship,
        state="submitting", created_at=now, review_expires_at=now + REVIEW_MAX_AGE,
    )
    with edit_invite_requests() as data:
        for existing in data.values():
            if existing["guild_id"] != guild_id or existing["target_id"] != target.id:
                continue
            # 不明な発行結果は経過時間に関係なく管理者による調査が必要。
            active = existing["state"] in {"issuing", "failed"}
            active |= existing["state"] in {"submitting", "pending"} and existing["review_expires_at"] > now
            active |= existing["state"] == "ready" and existing["expires_at"] > now
            if active:
                raise InviteError("この対象者には処理中・有効な申請があります。管理者に確認してください。")
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
        if record["review_expires_at"] <= time.time():
            raise InviteError("申請の審査期限（7日）が切れています。再申請してください。")
        record.update(state="issuing" if approved else "rejected", reviewer_id=actor_id, decided_at=time.time())
        return dict(record)


def request_embed(request_id, record):
    embed = discord.Embed(title="📨 招待申請", color=0x5865F2)
    embed.add_field(name="申請者", value=f'<@{record["applicant_id"]}>（ID: {record["applicant_id"]}）', inline=False)
    embed.add_field(name="招待対象（入力username）", value=discord.utils.escape_markdown(record["username"]), inline=False)
    embed.add_field(name="承認対象アカウント", value=f'<@{record["target_id"]}>\nusername: {discord.utils.escape_markdown(record["resolved_username"])}\nUser ID: {record["target_id"]}', inline=False)
    embed.add_field(name="人物・関係性", value=discord.utils.escape_markdown(record["relationship"]), inline=False)
    embed.add_field(name="状態", value=record["state"], inline=False)
    embed.set_footer(text=f"申請ID: {request_id} / 審査期限7日 / 承認対象IDの取り違えに注意")
    return embed


class TargetedInviteAPI:
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
            if path.endswith("/target-users"):
                return await response.text()
            return await response.json()

    async def create(self, channel_id, target_id, request_id):
        form = aiohttp.FormData()
        form.add_field("payload_json", json.dumps(dict(max_uses=1, max_age=INVITE_MAX_AGE, unique=True, temporary=False)))
        form.add_field("target_users_file", str(target_id).encode(), filename="target.csv", content_type="text/csv")
        return await self.request("POST", f"/channels/{channel_id}/invites", data=form,
                                  headers={"X-Audit-Log-Reason": quote(f"CCM invite {request_id}; target {target_id}")})

    async def verify(self, code, target_id):
        for attempt in range(5):
            job = await self.request("GET", f"/invites/{code}/target-users/job-status")
            if job.get("status") == 2:
                content = await self.request("GET", f"/invites/{code}/target-users")
                rows = list(csv.reader(io.StringIO(content)))
                if rows != [["user_id"], [str(target_id)]]:
                    raise InviteError("招待対象IDの照合に失敗しました。")
                return
            if job.get("status") != 1:
                break
            if attempt < 4:
                await asyncio.sleep(1)
        raise InviteError("対象者制限の反映を確認できませんでした。")

    async def delete(self, code):
        await self.request("DELETE", f"/invites/{code}")


async def issue_invite(request_id, record):
    code = None
    try:
        guild = bot.get_guild(record["guild_id"])
        if guild is None or guild.me is None or not guild.me.guild_permissions.manage_guild:
            raise InviteError("Botのサーバー管理権限を確認してください。")
        channel = next((ch for ch in guild.text_channels if ch.permissions_for(guild.me).create_instant_invite), None)
        if channel is None:
            raise InviteError("招待を作成できるテキストチャンネルがありません。")
        async with TargetedInviteAPI() as api:
            result = await api.create(channel.id, record["target_id"], request_id)
            code = result.get("code")
            if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", code):
                code = None
                raise InviteError("招待コードを確認できませんでした。")
            # 公開前に記録し、クラッシュ時にも調査・削除できるようにする。
            transition(request_id, {"issuing"}, "issuing", code=code)
            if result.get("max_uses") != 1 or result.get("max_age") != INVITE_MAX_AGE:
                raise InviteError("招待の利用回数・有効期限が要求と一致しません。")
            await api.verify(code, record["target_id"])
            # 発行開始時点から数え、Discord側より保守的な期限にする。
            expires_at = record["decided_at"] + INVITE_MAX_AGE
            return transition(request_id, {"issuing"}, "ready", expires_at=expires_at, delivery="pending")
    except Exception:
        revoked = False
        if code:
            try:
                async with TargetedInviteAPI() as api:
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
    text = f'申請ID: `{request_id}`\n対象ID: `{record["target_id"]}`\n状態: {record["state"]}'
    if record["state"] == "ready":
        if record["expires_at"] <= time.time():
            return text + "\n招待の有効期限が切れています。再申請してください。"
        text += f'\n本人へのDM: {record.get("delivery", "pending")}\nhttps://discord.gg/{record["code"]}\n対象ID本人のみ・1回限り。期限: <t:{int(record["expires_at"])}:F>（使用済みの場合は無効）'
    elif record["state"] in {"failed", "issuing", "submitting"}:
        text += "\n処理中、または安全のため停止しています。管理者に確認してください。"
    elif record["state"] == "pending" and record["review_expires_at"] <= time.time():
        text += "\n審査期限切れです。再申請してください。"
    return text


async def deliver_invite(request_id, record):
    direct = await send_dm(record["target_id"], f'CCMへの招待が承認されました。\n{status_text(request_id, record)}')
    record = transition(request_id, {"ready"}, "ready", delivery="sent" if direct else "fallback")
    if direct:
        message = f'✅ 申請 `{request_id}` を承認し、対象者本人（ID: {record["target_id"]}）へ招待をDMしました。'
    else:
        message = "✅ 承認されました。本人へのDMができないため、以下を対象者に転送してください。\n" + status_text(request_id, record)
    notified = await send_dm(record["applicant_id"], message)
    return direct, notified


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
