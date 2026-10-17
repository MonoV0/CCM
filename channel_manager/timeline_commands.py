"""TL設定・手動配信・リアクション検索と定期配信の登録。"""
import time
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import tasks

from bot_core import bot, logger
import timeline

LAST_ATTEMPT = {}


@bot.tree.command(name='tl_setup', description='TL専用カテゴリとチャンネルを作成し定期配信を開始します')
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def tl_setup(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with timeline.lock_for(interaction.guild.id):
        channel = await timeline.ensure_timeline(interaction.guild, timeline.load_settings())
        state = timeline.load_state()
        state[str(interaction.guild.id)] = {
            **state.get(str(interaction.guild.id), {}),
            'channel_id': channel.id, 'enabled': True,
            'last_post': datetime.now(timezone.utc).isoformat(),
        }
        if getattr(channel, 'category', None) is not None:
            state[str(interaction.guild.id)].setdefault('boundary_category_id', channel.category.id)
        timeline.save_state(state)
    await interaction.followup.send(
        f'✅ {channel.mention} を設定しました。定期配信を開始します。'
        '初回は設定間隔の経過後です。今すぐ投稿する場合は /tl_post を使ってください。'
        '自分だけで引く場合は /diary_gacha を使えます。', ephemeral=True)


@bot.tree.command(name='tl_stop', description='TLの定期配信を停止します')
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def tl_stop(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    async with timeline.lock_for(interaction.guild.id):
        state = timeline.load_state()
        if str(interaction.guild.id) in state:
            state[str(interaction.guild.id)]['enabled'] = False
            timeline.save_state(state)
    await interaction.followup.send('TLの定期配信を停止しました。再開は /tl_setup です。', ephemeral=True)


@bot.tree.command(name='tl_interval', description='TLの更新間隔と直近投稿の対象期間を設定します')
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(minutes='更新間隔（分、1～43200）', recent_hours='各カテゴリで探す直近の時間数（省略時は維持）')
async def tl_interval(interaction: discord.Interaction,
                      minutes: app_commands.Range[int, 1, 43200],
                      recent_hours: app_commands.Range[int, 1, 720] | None = None):
    await interaction.response.defer(ephemeral=True)
    async with timeline.lock_for(interaction.guild.id):
        state = timeline.load_state()
        entry = state.setdefault(str(interaction.guild.id), {})
        entry['interval_minutes'] = minutes
        if recent_hours is not None:
            entry['timeline_lookback_hours'] = recent_hours
        timeline.save_state(state)
        LAST_ATTEMPT.pop(interaction.guild.id, None)
    await interaction.followup.send(
        f'✅ 更新間隔を{minutes}分に設定しました。' +
        (f'対象は直近{recent_hours}時間です。' if recent_hours is not None else '') +
        '定期配信の開始・再開は /tl_setup です。', ephemeral=True)


@bot.tree.command(name='tl_boundary', description='TL境界と対象カテゴリを確認し、必要に応じて境界を指定します')
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.describe(category='このカテゴリより下を取得対象にする（省略で現在の設定を確認）')
async def tl_boundary(interaction: discord.Interaction, category: discord.CategoryChannel | None = None):
    await interaction.response.defer(ephemeral=True)
    async with timeline.lock_for(interaction.guild.id):
        config = timeline.load_settings()
        state = timeline.load_state()
        entry = state.setdefault(str(interaction.guild.id), {})
        if category is not None:
            if category.id not in {c.id for c in interaction.guild.categories}:
                await interaction.followup.send('このサーバーのカテゴリを指定してください。', ephemeral=True)
                return
            entry['boundary_category_id'] = category.id
            timeline.save_state(state)
        try:
            anchor = timeline.resolve_timeline_category(interaction.guild, config)
            below = timeline.category_boundary(interaction.guild, config)
        except timeline.TimelineBoundaryError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        target = interaction.guild.get_channel(entry['channel_id']) if entry.get('channel_id') else None
        sources = list(timeline.source_channels(interaction.guild, config, destination=target))
        names = [f'・{c.name} (ID: {c.id})' for c in interaction.guild.categories if c.id in below]
        text = (f'境界: **{discord.utils.escape_mentions(anchor.name)}** (ID: {anchor.id})\n'
                f'境界より下: {len(below)}カテゴリ／現在の条件で取得可能: {len(sources)}チャンネル\n'
                '閲覧権限・非表示・対象カテゴリ設定によって、さらに除外される場合があります。\n\n' +
                ('\n'.join(names)[:1300] if names else '境界より下にカテゴリがありません。TLカテゴリの位置を確認してください。'))
        await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name='tl_notify', description='TL更新時の全体通知をOFFまたは1回だけに設定します')
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
@app_commands.choices(mode=[app_commands.Choice(name='OFF', value='off'),
                            app_commands.Choice(name='更新ごとに1回', value='once')])
async def tl_notify(interaction: discord.Interaction, mode: str):
    await interaction.response.defer(ephemeral=True)
    if mode not in ('off', 'once'):
        await interaction.followup.send('off または once を選んでください。', ephemeral=True)
        return
    async with timeline.lock_for(interaction.guild.id):
        state = timeline.load_state()
        entry = state.setdefault(str(interaction.guild.id), {})
        if mode == 'once' and entry.get('channel_id'):
            target = interaction.guild.get_channel(entry['channel_id'])
            if target is not None and not target.permissions_for(interaction.guild.me).mention_everyone:
                await interaction.followup.send('Botに全体メンションの権限を付けてから設定してください。', ephemeral=True)
                return
        entry['notification_mode'] = mode
        timeline.save_state(state)
    text = '全体通知をOFFにしました。' if mode == 'off' else 'TL更新時の最初の1投稿だけで全体通知します。候補がない回は通知しません。'
    await interaction.followup.send(text, ephemeral=True)


@bot.tree.command(name='tl_post', description='日報ガチャをTLへ投稿します（同期間の重複は抑止）')
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
@app_commands.checks.has_permissions(manage_guild=True)
async def tl_post(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        sent = await timeline.publish(interaction.guild, force=True)
        text = '✅ TLに投稿しました。' if sent else '投稿対象がないか、この期間は配信済みです。'
    except (ValueError, RuntimeError) as error:
        text = str(error)
    await interaction.followup.send(text, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@bot.tree.command(name='diary_gacha', description='ランダムな日報を1件引きます（自分だけに表示）')
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 60, key=lambda i: (i.guild_id, i.user.id))
@app_commands.describe(days='直近何日の日報から引くか（省略時はJSON設定）')
async def diary_gacha(interaction: discord.Interaction,
                      days: app_commands.Range[int, 1, 30] | None = None):
    await interaction.response.defer(ephemeral=True)
    config = timeline.load_settings()
    messages, skipped = await timeline.collect(interaction.guild, config, days=days, user=interaction.user)
    async with timeline.lock_for(interaction.guild.id):
        # 収集中に権限・非表示設定が変わったチャンネルは抽選から除外。
        allowed = {c.id for c in timeline.source_channels(interaction.guild, config, user=interaction.user)}
        entry = timeline.load_state().get(str(interaction.guild.id), {})
        history = await timeline.pickup_history(interaction.guild, entry, config)
        candidates = timeline.unpicked([m for m in messages if m.channel.id in allowed], history)
        if not candidates:
            entry['recent_picks'] = history
            timeline.save_entry(interaction.guild.id, entry)
            await interaction.followup.send('対象の日報がありません。最近紹介した投稿しかないカテゴリはスキップします。新しい投稿を待つか、対象期間を確認してください。', ephemeral=True)
            return
        message = timeline.draw_diaries(candidates, 1)[0]
        try:
            message = await message.channel.fetch_message(message.id)
        except (discord.NotFound, discord.Forbidden):
            await interaction.followup.send('選ばれた日報が削除されたか、閲覧できなくなりました。もう一度引いてください。', ephemeral=True)
            return
        allowed = {c.id for c in timeline.source_channels(interaction.guild, config, user=interaction.user)}
        if message.channel.id not in allowed or message.author.bot:
            await interaction.followup.send('選ばれた日報を閲覧できません。もう一度引いてください。', ephemeral=True)
            return
        embed = timeline.diary_embed(message, note='抽選完了！気になった日報を読んでみよう。')
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label='元の日報を開く', url=message.jump_url))
        await interaction.followup.send(embed=embed, view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
        timeline.record_pick(interaction.guild.id, entry, history, message)




@bot.tree.command(name='reaction_search', description='リアクションの付いた最近の投稿を検索・一覧表示します')
@app_commands.guild_only()
@app_commands.checks.cooldown(1, 60, key=lambda i: (i.guild_id, i.user.id))
@app_commands.describe(emoji='絞り込む絵文字（省略時はすべて）', days='直近何日を検索するか',
                       minimum='最低リアクション数', limit='表示件数', channel='対象チャンネル（省略時は設定カテゴリ全体）')
async def reaction_search(interaction: discord.Interaction, emoji: str | None = None,
                          days: app_commands.Range[int, 1, 30] = 7,
                          minimum: app_commands.Range[int, 1, 1000] = 1,
                          limit: app_commands.Range[int, 1, 20] = 10,
                          channel: discord.TextChannel | None = None):
    await interaction.response.defer(ephemeral=True)
    config = timeline.load_settings()
    messages, skipped = await timeline.collect(interaction.guild, config, days=days,
                                               user=interaction.user, channel=channel)
    allowed = {c.id for c in timeline.source_channels(interaction.guild, config, user=interaction.user)}
    matches = timeline.ranked([m for m in messages if m.channel.id in allowed], [emoji] if emoji else [], minimum)
    embed = discord.Embed(title='🔎 リアクション検索', color=0x5865F2,
                          description=f'直近{days}日・{len(matches)}件ヒット（各ch最新{config["messages_per_channel"]}件まで）\n'
                                      f'取得不可: {skipped}ch。結果はあなただけに表示されます。')
    if not matches:
        embed.add_field(name='結果', value='条件に合う投稿はありません。対象カテゴリ・閲覧権限も確認してください。')
    # 合計6000文字を超えないよう1結果を短くする。
    for message in matches[:limit]:
        embed.add_field(name=f'{timeline.reaction_score(message, [emoji] if emoji else [])} リアクション',
                        value=f'{timeline.excerpt(message)[:120]}\n[投稿へ]({message.jump_url})', inline=False)
    await interaction.followup.send(embed=embed, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())


@tasks.loop(minutes=1)
async def timeline_tick():
    for guild in bot.guilds:
        # 配信失敗が続いた場合は15分間隔で再試行する。
        if time.monotonic() - LAST_ATTEMPT.get(guild.id, -10000) < 900:
            continue
        try:
            state = timeline.load_state()
            if state.get(str(guild.id), {}).get('enabled'):
                await timeline.publish(guild)
        except Exception:
            LAST_ATTEMPT[guild.id] = time.monotonic()
            logger.exception('TLの定期配信に失敗しました（guild=%s）。15分後以降に再試行します。', guild.id)


@timeline_tick.before_loop
async def before_timeline_tick():
    await bot.wait_until_ready()


@bot.listen('on_ready')
async def start_timeline():
    if not timeline_tick.is_running():
        timeline_tick.start()
