"""Discord bot commands cog."""

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Optional, TYPE_CHECKING

import discord
from discord import app_commands, ui
from discord.ext import commands
from loguru import logger

from api.models.anthropic import MessagesRequest, Message
from providers.rate_limit import GlobalRateLimiter
from providers.text import extract_text_from_content

if TYPE_CHECKING:
    from .bot import NimbusDiscordBot


class NimbusCog(commands.Cog):
    """Main cog for Nimbus Discord bot."""

    def __init__(self, bot: "NimbusDiscordBot"):
        self.bot = bot
        self.settings = bot.settings
        self.provider = bot.provider
        self.rate_limiter = bot.rate_limiter
        self.conversation_manager = bot.conversation_manager

    def _is_conversation_channel(self, channel_id: int) -> bool:
        """Check if channel is in the designated conversation category."""
        # Allow control channel for admin commands
        if channel_id == self.settings.discord_control_channel_id:
            return False
        # Otherwise check against all channels in the category
        # This is validated at runtime via category check in commands
        return True

    def _check_owner_access(self, user_id: int) -> bool:
        """Check if user has access based on owner-only mode."""
        # Check if user is blocked
        from .user_blocking import is_blocked
        if is_blocked(user_id):
            return False
        if not self.settings.discord_owner_only:
            return True  # Public mode - anyone can use
        return user_id == self.settings.discord_owner_id

    async def _check_rate_limits(
        self, interaction: discord.Interaction
    ) -> tuple[bool, str]:
        """Check all rate limits. Returns (allowed, error_message)."""
        # Check user cooldown (per-channel)
        allowed, retry = await self.rate_limiter.check_user_rate(interaction.user.id, interaction.channel_id)
        if not allowed:
            return False, f"Please wait {retry:.0f}s before asking again."

        # Check server rate
        allowed, retry = await self.rate_limiter.check_server_rate()
        if not allowed:
            return False, f"Server rate limit hit. Try again in {retry:.0f}s."

        return True, ""

    async def _stream_response_to_discord(
        self,
        interaction: discord.Interaction,
        request_data: MessagesRequest,
        input_tokens: int,
    ) -> str:
        """
        Stream NIM response to Discord.
        Returns the full response text.
        """
        await interaction.response.defer()

        # Get global rate limiter
        global_limiter = GlobalRateLimiter.get_instance()

        full_text = ""
        thinking_content = ""

        # Wait for rate limit slot and acquire concurrency slot
        await global_limiter.wait_if_blocked()

        async with global_limiter.concurrency_slot():
            try:
                request_id = f"discord_{uuid.uuid4().hex[:8]}"
                # Call stream_response directly - it's an async generator
                # execute_with_retry doesn't work with generators
                stream = self.provider.stream_response(
                    request_data, input_tokens, request_id=request_id
                )

                # Collect full response (Discord doesn't support true streaming)
                async for chunk in stream:
                    # Parse SSE chunk
                    if chunk.strip():
                        try:
                            event_data = chunk.split("data: ", 1)[-1].strip()
                            import json
                            data = json.loads(event_data)

                            if data.get("type") == "content_block_delta":
                                delta = data.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    full_text += delta.get("text", "")
                                elif delta.get("type") == "thinking_delta":
                                    thinking_content += delta.get("thinking", "")
                        except Exception:
                            # Skip malformed chunks
                            continue

            except Exception as e:
                logger.error(f"Stream error: {e}")
                await interaction.followup.send(
                    f"Error: {str(e)[:1900]}", ephemeral=True
                )
                return ""

        # Send final response
        content = full_text.strip() if full_text else "(No response)"
        threshold = self.settings.discord_split_threshold
        if len(content) > threshold:
            chunks = self._split_at_word_boundary(content, threshold)
            for i, chunk in enumerate(chunks):
                if i == 0:
                    await interaction.followup.send(chunk)
                else:
                    await interaction.channel.send(chunk)
        else:
            await interaction.followup.send(content)

        return content

    def _split_at_word_boundary(self, text: str, threshold: int) -> list[str]:
        """Split text at word boundaries, not mid-word."""
        chunks = []
        start = 0
        while start < len(text):
            if start + threshold >= len(text):
                chunks.append(text[start:])
                break
            chunk = text[start:start + threshold]
            last_space = chunk.rfind(' ')
            if last_space == -1:
                chunks.append(text[start:start + threshold])
                start += threshold
            else:
                chunks.append(text[start:start + last_space])
                start += last_space + 1
        return chunks

    @app_commands.command(name="ask", description="Ask NIM a question")
    @app_commands.describe(question="Your question to ask NIM")
    async def ask(self, interaction: discord.Interaction, question: str):
        """Ask NIM a question with conversation history."""
        # Check if user is blocked (silent fail for blocked users)
        from .user_blocking import is_blocked
        if is_blocked(interaction.user.id):
            return

        # Check owner access
        if not self._check_owner_access(interaction.user.id):
            await interaction.response.send_message(
                "🔒 This bot is in owner-only mode.", ephemeral=True
            )
            return

        # Check rate limits
        allowed, error = await self._check_rate_limits(interaction)
        if not allowed:
            await interaction.response.send_message(error, ephemeral=True)
            return

        # Log request to console (similar to Claude Code)
        print(
            f"[DISCORD] {interaction.user.display_name} ({interaction.user.id}) "
            f"asked: {question[:50]}{'...' if len(question) > 50 else ''}",
            flush=True
        )

        # Acquire channel lock
        channel_lock = self.rate_limiter.acquire_channel_lock(interaction.channel_id)
        async with channel_lock:
            # Check if auto-compact needed
            if self.conversation_manager.should_compact(interaction.channel_id):
                await interaction.response.send_message(
                    "🔄 Auto-compacting conversation before proceeding...",
                    ephemeral=True
                )
                await self._do_compact(interaction)
                await interaction.followup.send(
                    "Compaction complete. Now processing your question..."
                )

            # Get conversation history
            history = self.conversation_manager.get_history(interaction.channel_id)

            # Build request with system prompt
            messages = history + [{"role": "user", "content": question}]
            system_prompt = self.settings.discord_system_prompt
            request_data = MessagesRequest(
                model=self.settings.model,
                messages=[
                    Message(role=m["role"], content=m["content"])
                    for m in messages
                ],
                max_tokens=self.settings.discord_max_tokens,
                system=system_prompt,
            )

            # Count input tokens including system prompt
            from api.request_utils import get_token_count
            input_tokens = get_token_count(
                request_data.messages, system_prompt, request_data.tools
            )

            # Stream response
            response_text = await self._stream_response_to_discord(
                interaction, request_data, input_tokens
            )

            # Store in conversation history
            if response_text:
                self.conversation_manager.add_message(
                    interaction.channel_id, "user", question
                )
                self.conversation_manager.add_message(
                    interaction.channel_id, "assistant", response_text
                )

    @app_commands.command(
        name="compact", description="Summarize conversation and restart"
    )
    async def compact(self, interaction: discord.Interaction):
        """Manually trigger compaction."""
        # Check owner access
        if not self._check_owner_access(interaction.user.id):
            await interaction.response.send_message(
                "🔒 This bot is in owner-only mode.", ephemeral=True
            )
            return

        # Check this is a conversation channel (not DMs, control channel, etc)
        if not await self._check_conversation_channel(interaction):
            return

        await interaction.response.send_message(
            "🔄 Compacting conversation...", ephemeral=True
        )
        await self._do_compact(interaction)
        await interaction.followup.send(
            "✅ Conversation compacted. New context started."
        )

    async def _check_conversation_channel(self, interaction: discord.Interaction) -> bool:
        """Check if command is used in a valid conversation channel."""
        # Check control channel
        if interaction.channel_id == self.settings.discord_control_channel_id:
            await interaction.response.send_message(
                "❌ Cannot use this command in the control channel.", ephemeral=True
            )
            return False

        # Check conversation category
        channel = interaction.channel
        if not channel or not hasattr(channel, 'category_id'):
            await interaction.response.send_message(
                "❌ This command must be used in a text channel.", ephemeral=True
            )
            return False

        if channel.category_id != self.settings.discord_conversation_category_id:
            await interaction.response.send_message(
                "❌ This command can only be used in conversation channels.", ephemeral=True
            )
            return False

        return True

    async def _do_compact(self, interaction: discord.Interaction):
        """Perform compaction - summarize and reset."""
        # Get current conversation
        messages, token_count = self.conversation_manager.get_compact_context(
            interaction.channel_id
        )

        if not messages:
            await interaction.channel.send("Nothing to compact.")
            return

        # Build summary prompt
        conversation_text = "\n\n".join(
            f"{m['role'].capitalize()}: {m['content'][:500]}"
            for m in messages
        )
        summary_prompt = (
            "Please summarize the following conversation concisely, "
            "preserving key context and decisions:\n\n"
            f"{conversation_text[:8000]}"
            "\n\nSummary:"
        )

        # Send to NIM for summary
        summary_request = MessagesRequest(
            model=self.settings.model,
            messages=[Message(role="user", content=summary_prompt)],
            max_tokens=2000,  # Summary should be short
        )

        from api.request_utils import get_token_count
        input_tokens = get_token_count(
            summary_request.messages, None, None
        )

        # Wait for summary
        global_limiter = GlobalRateLimiter.get_instance()
        await global_limiter.wait_if_blocked()

        summary_text = ""
        async with global_limiter.concurrency_slot():
            try:
                request_id = f"compact_{uuid.uuid4().hex[:8]}"
                # Call directly - stream_response is an async generator
                stream = self.provider.stream_response(
                    summary_request, input_tokens, request_id=request_id
                )

                async for chunk in stream:
                    if chunk.strip():
                        try:
                            event_data = chunk.split("data: ", 1)[-1].strip()
                            import json
                            data = json.loads(event_data)
                            if data.get("type") == "content_block_delta":
                                delta = data.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    summary_text += delta.get("text", "")
                        except Exception:
                            continue
            except Exception as e:
                logger.error(f"Summary generation failed: {e}")
                summary_text = "[Summary generation failed]"

        # Delete messages from channel
        await self._clear_channel_messages(interaction.channel)

        # Post summary as new first message
        if summary_text:
            embed = discord.Embed(
                title="📝 Conversation Summary",
                description=summary_text[:4000],
                color=discord.Color.blue(),
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(text="New conversation context started from summary")
            await interaction.channel.send(embed=embed)

        # Update conversation manager with summary
        self.conversation_manager.compact(
            interaction.channel_id,
            f"[Previous conversation summary]: {summary_text}"
        )

    async def _do_compact_for_channel(self, channel: discord.TextChannel):
        """Perform compaction for a channel (used by live mode auto-compact)."""
        # Get current conversation
        messages, token_count = self.conversation_manager.get_compact_context(
            channel.id
        )

        if not messages:
            await channel.send("Nothing to compact.")
            return

        # Build summary prompt
        conversation_text = "\n\n".join(
            f"{m['role'].capitalize()}: {m['content'][:500]}"
            for m in messages
        )
        summary_prompt = (
            "Please summarize the following conversation concisely, "
            "preserving key context and decisions:\n\n"
            f"{conversation_text[:8000]}"
            "\n\nSummary:"
        )

        # Send to NIM for summary
        summary_request = MessagesRequest(
            model=self.settings.model,
            messages=[Message(role="user", content=summary_prompt)],
            max_tokens=2000,
        )

        from api.request_utils import get_token_count
        input_tokens = get_token_count(
            summary_request.messages, None, None
        )

        # Wait for summary
        global_limiter = GlobalRateLimiter.get_instance()
        await global_limiter.wait_if_blocked()

        summary_text = ""
        async with global_limiter.concurrency_slot():
            try:
                request_id = f"compact_{uuid.uuid4().hex[:8]}"
                stream = self.provider.stream_response(
                    summary_request, input_tokens, request_id=request_id
                )

                async for chunk in stream:
                    if chunk.strip():
                        try:
                            event_data = chunk.split("data: ", 1)[-1].strip()
                            import json
                            data = json.loads(event_data)
                            if data.get("type") == "content_block_delta":
                                delta = data.get("delta", {})
                                if delta.get("type") == "text_delta":
                                    summary_text += delta.get("text", "")
                        except Exception:
                            continue
            except Exception as e:
                logger.error(f"Summary generation failed: {e}")
                summary_text = "[Summary generation failed]"

        # Delete messages from channel
        await self._clear_channel_messages(channel)

        # Post summary as new first message
        if summary_text:
            embed = discord.Embed(
                title="📝 Conversation Summary",
                description=summary_text[:4000],
                color=discord.Color.blue(),
                timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(text="New conversation context started from summary")
            await channel.send(embed=embed)

        # Update conversation manager with summary
        self.conversation_manager.compact(
            channel.id,
            f"[Previous conversation summary]: {summary_text}"
        )

    async def _clear_channel_messages(
        self, channel: discord.TextChannel, limit: int = 1000
    ):
        """Clear messages from a channel."""
        now = discord.utils.utcnow()
        messages = []
        async for msg in channel.history(limit=limit):
            messages.append(msg)

        # Separate by age (14 day limit for bulk delete)
        recent = [
            m for m in messages
            if (now - m.created_at) < timedelta(days=14)
        ]
        old = [
            m for m in messages
            if (now - m.created_at) >= timedelta(days=14)
        ]

        # Bulk delete recent messages
        if recent:
            try:
                await channel.delete_messages(recent)
            except Exception as e:
                logger.warning(f"Bulk delete failed: {e}")

        # Delete old messages individually (rate limited)
        for msg in old:
            try:
                await msg.delete()
                await asyncio.sleep(0.5)  # Rate limit safety
            except Exception as e:
                logger.warning(f"Message delete failed: {e}")

    @app_commands.command(
        name="new", description="Clear conversation without summary"
    )
    async def new(self, interaction: discord.Interaction):
        """Clear conversation and channel without generating summary."""
        # Check owner access
        if not self._check_owner_access(interaction.user.id):
            await interaction.response.send_message(
                "🔒 This bot is in owner-only mode.", ephemeral=True
            )
            return

        # Check this is a conversation channel
        if not await self._check_conversation_channel(interaction):
            return

        await interaction.response.send_message(
            "🗑️ Clearing conversation...", ephemeral=True
        )

        # Clear conversation manager
        self.conversation_manager.clear(interaction.channel_id)

        # Clear channel messages
        await self._clear_channel_messages(interaction.channel)

        await interaction.followup.send(
            "✅ Conversation cleared. New context started."
        )

    @app_commands.command(name="status", description="Show bot and rate limit status")
    async def status(self, interaction: discord.Interaction):
        """Show current status."""
        # Get global rate limit status
        global_limiter = GlobalRateLimiter.get_instance()
        rate_status = global_limiter.get_status()

        # Get conversation stats
        token_count = self.conversation_manager.get_token_count(
            interaction.channel_id
        )
        should_compact = self.conversation_manager.should_compact(
            interaction.channel_id
        )

        embed = discord.Embed(
            title="NIMbus Status",
            color=discord.Color.green(),
        )
        embed.add_field(
            name="Model",
            value=self.settings.model,
            inline=True,
        )
        embed.add_field(
            name="NIM Rate Limit",
            value=f"{rate_status['current']}/{rate_status['max']} requests",
            inline=True,
        )
        embed.add_field(
            name="Conversation Tokens",
            value=f"{token_count:,} / {self.settings.discord_max_tokens:,}",
            inline=True,
        )
        if should_compact:
            embed.add_field(
                name="⚠️ Compaction",
                value="Will auto-compact soon",
                inline=False,
            )

        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="block", description="Block a user from using the bot (owner only)")
    @app_commands.describe(user="The user to block")
    async def block(self, interaction: discord.Interaction, user: discord.Member):
        """Block a user from using the bot."""
        # Only owner can block users
        if interaction.user.id != self.settings.discord_owner_id:
            await interaction.response.send_message(
                "🔒 Only the bot owner can block users.", ephemeral=True
            )
            return

        # Can't block the owner
        if user.id == self.settings.discord_owner_id:
            await interaction.response.send_message(
                "❌ Cannot block the bot owner.", ephemeral=True
            )
            return

        # Can't block the bot itself
        if user.id == self.bot.user.id:
            await interaction.response.send_message(
                "❌ Cannot block the bot.", ephemeral=True
            )
            return

        from .user_blocking import block_user
        newly_blocked = block_user(user.id)

        if newly_blocked:
            await interaction.response.send_message(
                f"🚫 Blocked {user.display_name} ({user.id}). They can no longer use the bot.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ {user.display_name} is already blocked.", ephemeral=True
            )

    @app_commands.command(name="unblock", description="Unblock a user (owner only)")
    @app_commands.describe(user="The user to unblock")
    async def unblock(self, interaction: discord.Interaction, user: discord.Member):
        """Unblock a previously blocked user."""
        # Only owner can unblock users
        if interaction.user.id != self.settings.discord_owner_id:
            await interaction.response.send_message(
                "🔒 Only the bot owner can unblock users.", ephemeral=True
            )
            return

        from .user_blocking import unblock_user
        was_unblocked = unblock_user(user.id)

        if was_unblocked:
            await interaction.response.send_message(
                f"✅ Unblocked {user.display_name} ({user.id}). They can now use the bot again.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ {user.display_name} was not blocked.", ephemeral=True
            )

    @app_commands.command(name="blocked", description="List blocked users (owner only)")
    async def blocked(self, interaction: discord.Interaction):
        """List all blocked users."""
        # Only owner can see blocked list
        if interaction.user.id != self.settings.discord_owner_id:
            await interaction.response.send_message(
                "🔒 Only the bot owner can view blocked users.", ephemeral=True
            )
            return

        from .user_blocking import get_blocked_users
        blocked_users = get_blocked_users()

        if not blocked_users:
            await interaction.response.send_message(
                "✅ No users are currently blocked.", ephemeral=True
            )
            return

        blocked_list = "\n".join(f"• <@{uid}> ({uid})" for uid in blocked_users)
        await interaction.response.send_message(
            f"🚫 **Blocked Users:**\n{blocked_list}", ephemeral=True
        )

    @ask.error
    @compact.error
    @new.error
    @block.error
    @unblock.error
    @blocked.error
    async def handle_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        """Handle command errors."""
        logger.error(f"Command error: {error}")
        if not interaction.response.is_done():
            await interaction.response.send_message(
                f"Error: {str(error)[:1900]}", ephemeral=True
            )
        else:
            await interaction.followup.send(
                f"Error: {str(error)[:1900]}", ephemeral=True
            )
