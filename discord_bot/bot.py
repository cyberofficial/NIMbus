"""Discord bot client integration with NIMbus."""

import asyncio
import os

import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

from config.settings import Settings
from providers.provider import NvidiaNimProvider

from .cog import NimbusCog
from .conversation import ConversationManager
from .rate_limit import DiscordRateLimiter


class NimbusDiscordBot(commands.Bot):
    """Discord bot for NIMbus - NVIDIA NIM proxy."""

    def __init__(self, settings: Settings, provider: NvidiaNimProvider):
        # Set up intents - need messages and guilds for live chat
        intents = discord.Intents.default()
        intents.messages = True  # Required for on_message event
        intents.message_content = True  # Required for reading message content
        intents.guilds = True  # Required for channel category access

        super().__init__(
            command_prefix=None,  # Slash commands only
            intents=intents,
        )

        self.settings = settings
        self.provider = provider

        # Initialize rate limiter
        self.rate_limiter = DiscordRateLimiter(
            user_cooldown=settings.discord_user_cooldown,
            server_limit=settings.discord_server_limit,
            server_window=settings.discord_server_window,
        )

        # Initialize conversation manager
        self.conversation_manager = ConversationManager(
            max_tokens=settings.discord_max_tokens,
            compact_threshold=settings.discord_compact_threshold,
        )

        # Guild restriction (primary guild for backward compatibility)
        self._guild_id = settings.discord_guild_id

    async def setup_hook(self) -> None:
        """Set up bot - called before login."""
        # Add the main cog
        await self.add_cog(NimbusCog(self))

        # Commands are synced in on_ready after bot connects

    async def on_ready(self) -> None:
        """Called when bot is ready."""
        logger.info(f"Discord bot logged in as {self.user} (ID: {self.user.id})")

        # Sync commands on every startup (Discord sometimes clears them)
        await self._sync_commands_to_all_guilds()

        # Send startup message to control channels
        await self._send_control_startup()

    async def _sync_commands_to_all_guilds(self) -> None:
        """Sync slash commands to all configured guilds."""
        guild_ids = self.settings.discord_guild_ids or {self._guild_id}
        synced_count = 0
        for guild_id in guild_ids:
            if guild_id:
                try:
                    guild = discord.Object(id=guild_id)
                    self.tree.copy_global_to(guild=guild)
                    await self.tree.sync(guild=guild)
                    synced_count += 1
                    logger.info(f"Commands synced to guild {guild_id}")
                except Exception as e:
                    logger.error(f"Failed to sync commands to guild {guild_id}: {e}")
        logger.info(f"Commands synced to {synced_count} guild(s)")

    async def _send_control_startup(self) -> None:
        """Send startup message to all control channels and clean old bot messages."""
        control_channel_ids = self.settings.discord_control_channel_ids
        # Fallback to single channel for backward compatibility (only if set and non-zero)
        if not control_channel_ids and self.settings.discord_control_channel_id:
            single_id = self.settings.discord_control_channel_id
            if single_id and single_id != 0:
                control_channel_ids = {single_id}

        if not control_channel_ids:
            logger.debug("No control channels configured, skipping startup message")
            return

        for channel_id in control_channel_ids:
            try:
                channel = self.get_channel(channel_id)
                if not channel:
                    logger.warning(f"Control channel {channel_id} not found")
                    continue

                # Clean old bot messages from control channel
                await self._cleanup_control_channel(channel)

                embed = discord.Embed(
                    title="NIMbus Bot Online",
                    description="Discord bot is ready to handle requests.",
                    color=discord.Color.green(),
                )
                embed.add_field(name="Model", value=self.settings.model, inline=True)
                embed.add_field(
                    name="Max Tokens",
                    value=f"{self.settings.discord_max_tokens:,}",
                    inline=True,
                )
                embed.add_field(
                    name="Compact Threshold",
                    value=f"{self.settings.discord_compact_threshold:.0%}",
                    inline=True,
                )

                # Import and add control panel view
                from .views import ControlPanelView
                view = ControlPanelView()
                self.add_view(view)  # Register persistent view
                await channel.send(embed=embed, view=view)

            except Exception as e:
                logger.error(f"Failed to send control startup to channel {channel_id}: {e}")

    async def _cleanup_control_channel(self, channel: discord.TextChannel, limit: int = 100) -> None:
        """Delete old bot messages from control channel."""
        from datetime import datetime, timedelta
        from discord.utils import utcnow

        now = utcnow()
        bot_messages = []

        try:
            async for msg in channel.history(limit=limit):
                # Delete bot's own messages and messages with our control panel embeds
                is_bot_msg = msg.author.id == self.user.id
                # Also delete messages that have our control panel embeds
                has_embed = bool(msg.embeds) and any(
                    e.title == "NIMbus Bot Online" for e in msg.embeds
                )
                if is_bot_msg or has_embed:
                    bot_messages.append(msg)
        except Exception as e:
            logger.warning(f"Failed to fetch channel history: {e}")
            return

        if not bot_messages:
            return

        # Bulk delete recent messages (< 14 days)
        recent = [m for m in bot_messages if (now - m.created_at) < timedelta(days=14)]
        old = [m for m in bot_messages if (now - m.created_at) >= timedelta(days=14)]

        if recent:
            try:
                await channel.delete_messages(recent)
                logger.info(f"Cleaned {len(recent)} recent messages from control channel")
            except Exception as e:
                logger.warning(f"Bulk delete failed: {e}")

        # Delete old messages individually
        for msg in old:
            try:
                await msg.delete()
            except Exception:
                pass

    async def start_bot(self) -> None:
        """Start the bot - called from FastAPI lifespan."""
        logger.info("Starting Discord bot...")
        await self.start(self.settings.discord_bot_token)

    async def is_conversation_channel(self, channel_id: int) -> bool:
        """Check if a channel is in one of the conversation categories."""
        category_ids = self.settings.discord_conversation_category_ids
        # Fallback to single category for backward compatibility
        if not category_ids and self.settings.discord_conversation_category_id:
            category_ids = {self.settings.discord_conversation_category_id}

        if not category_ids:
            return False

        channel = self.get_channel(channel_id)
        if not channel:
            # Try fetching from API if not in cache
            try:
                channel = await self.fetch_channel(channel_id)
            except Exception:
                return False
        if not channel:
            return False

        return getattr(channel, 'category_id', None) in category_ids

    async def _process_message_queue(self, channel_id: int):
        """Process messages in FIFO order for a channel."""
        from .conversation import ConversationSession
        session = self.conversation_manager.get_session(channel_id)
        if not session or session.is_processing:
            return

        session.is_processing = True
        try:
            while not session.processing_queue.empty():
                try:
                    msg_data = await asyncio.wait_for(session.processing_queue.get(), timeout=1.0)
                    await self._handle_conversation_message(
                        msg_data['channel'],
                        msg_data['user'],
                        msg_data['content'],
                        msg_data.get('replied_message')
                    )
                    session.processing_queue.task_done()
                except asyncio.TimeoutError:
                    break
                except Exception as e:
                    logger.error(f"Error processing message in channel {channel_id}: {e}")
                    break
        finally:
            session.is_processing = False

    async def _handle_conversation_message(self, channel, user, content, replied_message=None):
        """Handle a message in a conversation channel."""
        from api.models.anthropic import MessagesRequest, Message
        from providers.rate_limit import GlobalRateLimiter
        from api.request_utils import get_token_count

        # Check owner access for owner-only mode
        if self.settings.discord_owner_only and user.id != self.settings.discord_owner_id:
            return

        # Check if user is blocked
        from .user_blocking import is_blocked
        if is_blocked(user.id):
            return

        # Check rate limits (per-channel cooldown)
        allowed, _ = await self.rate_limiter.check_user_rate(user.id, channel.id)
        if not allowed:
            return

        allowed, _ = await self.rate_limiter.check_server_rate()
        if not allowed:
            await channel.send("⏳ Server rate limit hit. Please wait a moment.")
            return

        # Check for compaction warning (warns once 5% before threshold)
        should_warn, percentage = self.conversation_manager.should_warn_about_compact(channel.id)
        if should_warn:
            threshold_pct = self.conversation_manager._compact_threshold * 100
            await channel.send(
                f"⚠️ This conversation is at **{percentage:.0%}** of the token limit. "
                f"Auto-compaction will trigger at **{threshold_pct:.0%}** to summarize "
                f"and reset the conversation."
            )

        # Check for auto-compact
        if self.conversation_manager.should_compact(channel.id):
            await channel.send(
                "🔄 Auto-compacting conversation...\n\n"
                "*Tip: Run `/compact` manually to backup chat history to your DMs first.*"
            )
            cog = self.get_cog('NimbusCog')
            if cog:
                await cog._do_compact_for_channel(channel)

        # Format message with username for context
        formatted_content = f"{user.display_name}: {content}"

        # Add reply context if this is a reply
        if replied_message:
            reply_author = replied_message.author.display_name
            reply_content = replied_message.content or "(no text content)"
            if len(reply_content) > 500:
                reply_content = reply_content[:500] + "..."
            formatted_content = f"[Replying to {reply_author}'s message: \"{reply_content}\"]\n{formatted_content}"

        # Get history
        history = self.conversation_manager.get_history_for_nim(channel.id)

        # Build request with system prompt
        messages = history + [{"role": "user", "content": formatted_content}]
        system_prompt = self.settings.discord_system_prompt
        request_data = MessagesRequest(
            model=self.settings.model,
            messages=[Message(role=m["role"], content=m["content"]) for m in messages],
            max_tokens=self.settings.discord_max_tokens,
            system=system_prompt,
        )

        # Count tokens including system prompt
        input_tokens = get_token_count(
            request_data.messages, system_prompt, request_data.tools
        )

        # Log request
        print(
            f"[DISCORD-LIVE] {user.display_name} ({user.id}) in #{channel.name}: "
            f"{content[:50]}{'...' if len(content) > 50 else ''}",
            flush=True
        )

        # Show typing indicator
        async with channel.typing():
            global_limiter = GlobalRateLimiter.get_instance()
            await global_limiter.wait_if_blocked()

            full_text = ""
            async with global_limiter.concurrency_slot():
                try:
                    import uuid
                    request_id = f"discord_live_{uuid.uuid4().hex[:8]}"
                    stream = self.provider.stream_response(
                        request_data, input_tokens, request_id=request_id
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
                                        full_text += delta.get("text", "")
                            except Exception:
                                continue
                except Exception as e:
                    await channel.send(f"❌ Error: {str(e)[:1900]}")
                    return

        # Store in conversation
        if full_text:
            self.conversation_manager.add_message_with_user(
                channel.id, "user", content, user.id, user.display_name
            )
            self.conversation_manager.add_message_with_user(
                channel.id, "assistant", full_text, None, "NIM"
            )

        # Send response (split into chunks if too long for Discord 2000 char limit)
        content_out = full_text.strip() if full_text else "(No response)"
        if len(content_out) > 1900:
            # Split into chunks of ~1900 chars and send multiple messages
            chunks = [content_out[i:i+1900] for i in range(0, len(content_out), 1900)]
            for chunk in chunks:
                await channel.send(chunk)
        else:
            await channel.send(content_out)

    def _split_at_word_boundary(self, text: str, threshold: int) -> list[str]:
        """Split text at word boundaries, not mid-word."""
        chunks = []
        start = 0
        while start < len(text):
            if start + threshold >= len(text):
                # Remaining text fits in threshold
                chunks.append(text[start:])
                break

            # Find the last space before threshold
            chunk = text[start:start + threshold]
            last_space = chunk.rfind(' ')

            if last_space == -1:
                # No space found, have to cut mid-word
                chunks.append(text[start:start + threshold])
                start += threshold
            else:
                # Cut at word boundary
                chunks.append(text[start:start + last_space])
                start += last_space + 1  # Skip the space

        return chunks

    async def on_message(self, message: discord.Message):
        """Handle messages in conversation channels."""
        # Check if user is blocked
        from .user_blocking import is_blocked
        if is_blocked(message.author.id):
            return

        # Always print to console for debugging
        print(f"[DEBUG on_message] {message.author.display_name}: {message.content[:50]}", flush=True)

        # Skip bot messages immediately before any processing
        if message.author.bot:
            print("[DEBUG] Skipping bot message", flush=True)
            return

        # Skip DMs and slash command attempts
        if not message.guild:
            print("[DEBUG] Skipping DM", flush=True)
            return
        if message.content.startswith('/'):
            print("[DEBUG] Skipping command message", flush=True)
            return

        # Skip messages with attachments if configured
        if self.settings.discord_skip_files and message.attachments:
            print("[DEBUG] Skipping message with attachments", flush=True)
            return

        # Check conversation category
        is_conv = await self.is_conversation_channel(message.channel.id)
        print(f"[DEBUG] is_conversation_channel: {is_conv}", flush=True)
        if not is_conv:
            return

        print(f"[DEBUG] Processing message: {message.content[:50]}", flush=True)

        # Handle message replies for additional context
        replied_message = None
        if message.reference and message.reference.message_id:
            try:
                replied_message = await message.channel.fetch_message(message.reference.message_id)
                print(f"[DEBUG] Message is reply to: {replied_message.author.display_name}: {replied_message.content[:50]}", flush=True)
            except Exception as e:
                print(f"[DEBUG] Failed to fetch replied message: {e}", flush=True)

        # Get session and queue message
        session = self.conversation_manager.get_session(message.channel.id)
        await session.processing_queue.put({
            'channel': message.channel,
            'user': message.author,
            'content': message.content,
            'replied_message': replied_message,
        })

        # Process queue
        asyncio.create_task(self._process_message_queue(message.channel.id))

    async def close_bot(self) -> None:
        """Close the bot - called from FastAPI shutdown."""
        logger.info("Shutting down Discord bot...")
        await self.close()

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        """Handle command errors."""
        logger.error(f"Command error: {error}")

        if isinstance(error, commands.CommandNotFound):
            return  # Ignore unknown commands

        await ctx.send(f"An error occurred: {error}")
