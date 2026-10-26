"""長時間の履歴取得はバックグラウンドで実行し、再実行で結果を取得する。"""
import asyncio
import io

import discord
from discord import app_commands

from bot_core import bot, ADMIN_ID, logger
from storage import load_data, load_hidden_data, GRADE_CATEGORIES
from utils import is_personal_channel_category
from omikuji import collect, workbook_bytes
from interaction_lifecycle import defer_export

JOBS = {}


async def run_job(channels, job, start):
    try:
        rows, errors = await collect(channels, job)
        job['file'] = await asyncio.to_thread(workbook_bytes, rows, errors, start)
        job['count'] = len(rows)
        job['errors'] = len(errors)
    except Exception:
        logger.exception('Omikuji export failed')
        job['failed'] = True


@bot.tree.command(name='omikuji_export', description='おみくじ候補のExcel収集を開始／進捗確認／ダウンロードします')
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.describe(start='開始番号（収集開始時のみ使用）', refresh='前回の結果を破棄して再収集')
async def omikuji_export(interaction: discord.Interaction,
                         start: app_commands.Range[int, 1, 1000000] = 1,
                         refresh: bool = False):
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message('管理者本人のみ使用できます。', ephemeral=True)
        return
    if not await defer_export(interaction):
        return
    key = interaction.guild.id
    job = JOBS.get(key)
    if job and not job['task'].done():
        await interaction.followup.send(
            f"収集中: {job['done']}/{job['total']}チャンネル。後で同じコマンドを実行してください。", ephemeral=True)
        return
    if job and not refresh:
        if job.get('failed'):
            await interaction.followup.send('収集に失敗しました。ログを確認し、refresh:Trueで再試行してください。', ephemeral=True)
            return
        if len(job['file']) > interaction.guild.filesize_limit:
            await interaction.followup.send('Excelが添付容量の上限を超えました。管理者にログと収集範囲の確認を依頼してください。', ephemeral=True)
            return
        await interaction.followup.send(
            f"{job['count']}件。取得失敗: {job['errors']}チャンネル（Excel内に記載）。",
            file=discord.File(io.BytesIO(job['file']), filename='omikuji.xlsx'), ephemeral=True)
        return
    known = {int(cid) for cid in load_data().values()} | {int(cid) for cid in load_hidden_data()}
    channels = [c for c in interaction.guild.text_channels if c.id in known or
                (c.category and is_personal_channel_category(c.category.name, GRADE_CATEGORIES.values()))]
    job = {'done': 0, 'total': len(channels)}
    JOBS[key] = job
    job['task'] = asyncio.create_task(run_job(channels, job, start))
    await interaction.followup.send(
        f'{len(channels)}個の個人チャンネルを収集します。後で /omikuji_export を実行すると進捗確認・Excel取得ができます。'
        'Bot再起動時には収集結果が消えます。', ephemeral=True)
