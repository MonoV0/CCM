"""systemdに更新を委譲する。Bot自身でpullや子プロセスの再起動は行わない。"""
import asyncio

from bot_core import logger


async def request_update():
    # サービス名・引数は固定。Discordから任意のコマンドやブランチは受け取らない。
    process = await asyncio.create_subprocess_exec(
        "systemctl", "--user", "start", "--no-block", "ccm-update.service",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        code = await asyncio.wait_for(process.wait(), timeout=10)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        logger.error("更新サービスの受付結果が不明。自動再試行はしません。")
        return False
    if code != 0:
        logger.error("更新サービスの起動要求失敗（exit=%s）", code)
    return code == 0
