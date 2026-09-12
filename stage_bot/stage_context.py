"""操作対象を固定し、パネル・モーダルで共通の権限検証を行う。"""
import discord
from storage import temp_channels


class StageContext:
    def __init__(self, *args, target_channel=None, **kwargs):
        self.target_channel = target_channel
        super().__init__(*args, **kwargs)

    def get_channel(self, interaction):
        # 常駐パネルは投稿先、スラッシュコマンドのパネルは明示されたステージ。
        if self.target_channel is None:
            return interaction.channel
        return interaction.guild.get_channel(self.target_channel.id)

    async def interaction_check(self, interaction):
        channel = self.get_channel(interaction)
        data = temp_channels.get(str(channel.id)) if isinstance(channel, discord.StageChannel) else None
        if data is None:
            await interaction.response.send_message("❌ 対象ステージは既に終了しているか、無効です。", ephemeral=True)
            return False
        if not (channel.permissions_for(interaction.user).manage_channels
                or interaction.user.id in data.get("sub_admins", [])):
            await interaction.response.send_message("⚠️ この操作を行えるのはステージ作成者またはサブ管理者のみです。", ephemeral=True)
            return False
        return True


class StageView(StageContext, discord.ui.View):
    pass


class StageModal(StageContext, discord.ui.Modal):
    pass
