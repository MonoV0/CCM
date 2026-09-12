"""UIとイベントが共有するステージ操作。UIモジュールには依存しない。"""
import discord

from storage import temp_channels, save_temp_channels


def is_blocked(channel_id: int, user_id: int) -> bool:
    return user_id in temp_channels.get(str(channel_id), {}).get("blocked_users", [])


async def maybe_delete_temp_channel(channel, force: bool = False) -> bool:
    """削除成功・既に削除済みの場合だけ管理記録を消す。失敗時は再試行可能にする。"""
    key = str(channel.id)
    if key not in temp_channels or not isinstance(channel, discord.StageChannel):
        return False
    if not force and channel.members:
        return False
    try:
        await channel.delete(reason="一時ステージの自動削除または終了ボタン")
    except discord.NotFound:
        pass
    except discord.HTTPException:
        return False
    temp_channels.pop(key, None)
    save_temp_channels()
    return True


async def toggle_privacy(interaction, channel):
    """通常・簡易パネル共通の公開切り替え。権限変更を名前変更より優先する。"""
    await interaction.response.defer(ephemeral=True)
    role = channel.guild.default_role
    overwrite = channel.overwrites_for(role)
    make_public = overwrite.view_channel is False
    overwrite.view_channel = make_public
    if make_public:
        overwrite.connect = True
        overwrite.stream = True
    try:
        await channel.set_permissions(role, overwrite=overwrite)
    except discord.HTTPException:
        await interaction.followup.send("❌ 公開状態の変更に失敗しました。", ephemeral=True)
        return
    name = channel.name.removeprefix("🔒").strip() if make_public else (
        channel.name if channel.name.startswith("🔒") else f"🔒{channel.name}"
    )
    note = ""
    try:
        await channel.edit(name=name[:100])
    except discord.HTTPException:
        note = "（チャンネル名の更新には失敗しました）"
    label = "🔓 公開" if make_public else "🔒 非公開"
    await interaction.followup.send(f"{label}に変更しました。{note}", ephemeral=True)
