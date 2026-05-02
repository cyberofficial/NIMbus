"""Discord UI Views and Modals for NIMbus."""

import asyncio

import discord
from discord import ui
from loguru import logger


class CreateChannelModal(ui.Modal, title="Create Conversation Channel"):
    """Modal for creating a new conversation channel."""

    channel_name = ui.TextInput(
        label="Channel Name",
        placeholder="e.g., project-alpha",
        max_length=100,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        """Create the channel when modal is submitted."""
        try:
            # Get the conversation category
            settings = interaction.client.settings
            category = interaction.guild.get_channel(
                settings.discord_conversation_category_id
            )

            if not category:
                await interaction.response.send_message(
                    "❌ Conversation category not found. Check configuration.",
                    ephemeral=True,
                )
                return

            # Create the channel
            new_channel = await interaction.guild.create_text_channel(
                name=self.channel_name.value,
                category=category,
                topic=f"NIM conversation thread - Created by {interaction.user.display_name}",
            )

            await interaction.response.send_message(
                f"✅ Created {new_channel.mention} in {category.mention}",
                ephemeral=True,
            )

            # Send initial message in new channel
            embed = discord.Embed(
                title="🤖 Live Conversation Channel",
                description="Just type your messages - NIM will respond automatically! "
                           "The bot tracks who's speaking for context.",
                color=discord.Color.blue(),
            )
            embed.add_field(
                name="Commands (also available)",
                value="`/ask <question>` - One-shot question (no history)\n"
                      "`/compact` - Summarize and restart\n"
                      "`/new` - Clear without summary\n"
                      "`/status` - Show bot status",
                inline=False,
            )
            await new_channel.send(embed=embed)

        except discord.Forbidden:
            await interaction.response.send_message(
                "❌ I don't have permission to create channels.",
                ephemeral=True,
            )
        except Exception as e:
            logger.error(f"Failed to create channel: {e}")
            await interaction.response.send_message(
                f"❌ Error creating channel: {e}",
                ephemeral=True,
            )


class ControlPanelView(ui.View):
    """Persistent view with control panel buttons."""

    def __init__(self):
        super().__init__(timeout=None)  # Persistent view

    @ui.button(
        label="Create Channel",
        style=discord.ButtonStyle.green,
        custom_id="control:create_channel",
        emoji="➕",
    )
    async def create_channel(self, interaction: discord.Interaction, button: ui.Button):
        """Open modal to create a new conversation channel."""
        # Check owner access
        settings = interaction.client.settings
        if settings.discord_owner_only and interaction.user.id != settings.discord_owner_id:
            await interaction.response.send_message(
                "🔒 Only the bot owner can create channels.", ephemeral=True
            )
            return

        await interaction.response.send_modal(CreateChannelModal())

    @ui.button(
        label="List Channels",
        style=discord.ButtonStyle.blurple,
        custom_id="control:list_channels",
        emoji="📋",
    )
    async def list_channels(self, interaction: discord.Interaction, button: ui.Button):
        """List all conversation channels."""
        try:
            settings = interaction.client.settings
            category = interaction.guild.get_channel(
                settings.discord_conversation_category_id
            )

            if not category:
                await interaction.response.send_message(
                    "❌ Conversation category not found.", ephemeral=True
                )
                return

            # Get all channels in the category
            channels = [
                ch for ch in category.channels
                if isinstance(ch, discord.TextChannel)
            ]

            if not channels:
                await interaction.response.send_message(
                    "📭 No conversation channels found in this category.",
                    ephemeral=True,
                )
                return

            # Build list
            channel_list = "\n".join(
                f"{i+1}. {ch.mention} ({ch.name})"
                for i, ch in enumerate(channels[:25])  # Discord limit
            )

            embed = discord.Embed(
                title="📋 Conversation Channels",
                description=f"Channels in {category.mention}:\n\n{channel_list}",
                color=discord.Color.blue(),
            )
            embed.set_footer(text=f"Total: {len(channels)} channels")

            await interaction.response.send_message(embed=embed, ephemeral=True)

        except Exception as e:
            logger.error(f"Failed to list channels: {e}")
            await interaction.response.send_message(
                f"❌ Error listing channels: {e}", ephemeral=True
            )

    @ui.button(
        label="Bot Status",
        style=discord.ButtonStyle.gray,
        custom_id="control:bot_status",
        emoji="📊",
    )
    async def bot_status(self, interaction: discord.Interaction, button: ui.Button):
        """Show bot status."""
        from providers.rate_limit import GlobalRateLimiter

        settings = interaction.client.settings
        global_limiter = GlobalRateLimiter.get_instance()
        rate_status = global_limiter.get_status()

        embed = discord.Embed(
            title="📊 NIMbus Bot Status",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Model",
            value=settings.model,
            inline=True,
        )
        embed.add_field(
            name="NIM Rate Limit",
            value=f"{rate_status['current']}/{rate_status['max']} requests",
            inline=True,
        )
        embed.add_field(
            name="Max Tokens",
            value=f"{settings.discord_max_tokens:,}",
            inline=True,
        )
        embed.add_field(
            name="Compact Threshold",
            value=f"{settings.discord_compact_threshold:.0%}",
            inline=True,
        )
        embed.add_field(
            name="Owner Only Mode",
            value="Enabled" if settings.discord_owner_only else "Disabled",
            inline=True,
        )
        embed.add_field(
            name="Server Rate Limit",
            value=f"{settings.discord_server_limit} req / {settings.discord_server_window}s",
            inline=True,
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)
