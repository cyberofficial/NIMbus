"""Discord bot client integration with NIMbus."""

import asyncio
import json
import os
import uuid

import discord
from discord import app_commands
from discord.ext import commands
from loguru import logger

from config.settings import Settings
from providers.provider import NvidiaNimProvider
from providers.request_queue import RequestPriority

from .cog import NimbusCog
from .conversation import ConversationManager
from .rate_limit import DiscordRateLimiter
from .tools import WEB_SEARCH_TOOLS, execute_fetch_page, execute_web_search

# Debug flag for verbose web search logging
WEB_SEARCH_DEBUG = os.getenv("WEB_SEARCH_DEBUG", "false").lower() == "true"


class NimbusDiscordBot(commands.Bot):
    """Discord bot for NIMbus - NVIDIA NIM proxy."""

    def __init__(self, settings: Settings, provider: NvidiaNimProvider):
        # Set up intents - need messages and guilds for live chat
        intents = discord.Intents.default()
        intents.messages = True  # Required for on_message event
        intents.message_content = True  # Required for reading message content
        intents.guilds = True  # Required for channel category access

        # MUST be set before super().__init__() so _get_command_prefix() can access them
        self.settings = settings
        self.provider = provider

        super().__init__(
            command_prefix=self._get_command_prefix(),
            intents=intents,
        )

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
        self._typing_locks: dict[int, float] = {}
        self._typing_retry_delays: dict[int, float] = {}  # channel_id -> current retry delay

        # Resolve Discord model
        self._discord_model = settings.discord_model
        if self._discord_model is None:
            logger.warning("No Discord model configured (DISCORD_MODEL empty, MODEL not set, "
                           "or no valid model found in Claude settings). Discord bot will not start.")

    def _get_command_prefix(self):
        """Return a prefix callable that supports both @mentions and the configured prefix."""
        prefix = self.settings.discord_command_prefix or "!!"

        def _prefix(bot, message):
            if message.content.startswith(f"<@{bot.user.id}>") or message.content.startswith(f"<@!{bot.user.id}>"):
                return ""
            return message.content.startswith(prefix)

        return _prefix

    def _is_bot_mentioned(self, message: discord.Message) -> bool:
        """Check if the message contains a bot @mention anywhere in the text."""
        content = message.content
        bot_user = getattr(self, "user", None)
        if bot_user is None:
            return False
        bot_id = getattr(bot_user, "id", None)
        if bot_id is None:
            return False
        # Support both <@id> and <@!id> formats (regular and nickname mention)
        return f"<@{bot_id}>" in content or f"<@!{bot_id}>" in content

    async def setup_hook(self) -> None:
        """Set up bot - called before login."""
        # Add the main cog
        await self.add_cog(NimbusCog(self))

        # Filter commands based on settings
        await self._filter_commands_by_settings()

        # Commands are synced in on_ready after bot connects

    async def _filter_commands_by_settings(self) -> None:
        """Unregister commands that are disabled in settings."""
        # Map command names to their setting
        command_toggles = {
            'ask': self.settings.discord_cmd_ask,
            'compact': self.settings.discord_cmd_compact,
            'new': self.settings.discord_cmd_new,
            'status': self.settings.discord_cmd_status,
            'download': self.settings.discord_cmd_download,
            'block': self.settings.discord_cmd_block,
            'unblock': self.settings.discord_cmd_blocked,
            'blocked': self.settings.discord_cmd_blocked,
            'newchannel': self.settings.discord_cmd_newchannel,
        }

        removed = []
        for command_name, is_enabled in command_toggles.items():
            if not is_enabled:
                try:
                    self.tree.remove_command(command_name)
                    removed.append(command_name)
                except Exception as e:
                    logger.debug(f"Could not remove command {command_name}: {e}")

        if removed:
            logger.info(f"Disabled Discord commands: {', '.join(removed)}")

    async def on_ready(self) -> None:
        """Called when bot is ready."""
        if self.user is not None:
            logger.info(f"Discord bot logged in as {self.user} (ID: {self.user.id})")
        else:
            logger.info(f"Discord bot logged in")

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
                if isinstance(channel, discord.TextChannel):
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
                if isinstance(channel, (discord.TextChannel, discord.VoiceChannel, discord.Thread)):
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
                is_bot_msg = self.user is not None and msg.author.id == self.user.id  # type: ignore[attr-defined]
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
        if self._discord_model is None:
            logger.warning("Discord bot startup skipped: no model configured.")
            return
        logger.info("Starting Discord bot...")
        await self.start(self.settings.discord_bot_token)

    async def is_conversation_channel(self, channel_id: int) -> bool:
        """Check if a channel is in one of the conversation categories or conversation channels."""
        # First check if it's a directly specified conversation channel
        channel_ids = self.settings.discord_conversation_channel_ids
        if channel_ids and channel_id in channel_ids:
            return True

        # Check category-based configuration
        category_ids = self.settings.discord_conversation_category_ids
        # Fallback to single category for backward compatibility
        if not category_ids and self.settings.discord_conversation_category_id:
            category_ids = {self.settings.discord_conversation_category_id}

        # If category IDs configured, check channel's category
        if category_ids:
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

        # If no category IDs configured but we have specific channels, deny this channel
        # If no configuration at all, deny (safer default)
        return False

    async def _handle_prefix_command(self, message: discord.Message, content_after_prefix: str) -> bool:
        """Handle text-prefix commands like !!ask, !!compact, !!new, !!status. Returns True if handled."""
        cog = self.get_cog('NimbusCog')
        if not cog or not isinstance(cog, NimbusCog):
            await message.channel.send("❌ Internal error: NimbusCog not loaded.")
            return True

        parts = content_after_prefix.split()
        cmd = parts[0].lower() if parts else ""
        args = content_after_prefix[len(cmd):].strip()

        if cmd == "ask" and self.settings.discord_cmd_prefix_ask:
            if not args:
                await message.channel.send("Usage: `!!ask <your question>`")
                return True
            if hasattr(message.channel, "send") and hasattr(message.author, "display_name"):
                await cog.prefix_ask(message, args)
            return True

        if cmd == "compact" and self.settings.discord_cmd_prefix_compact:
            channel = message.channel
            user = message.author
            if hasattr(channel, "send") and hasattr(user, "display_name"):
                await cog.prefix_compact(channel, user)  # type: ignore[arg-type]
            return True

        if cmd == "new" and self.settings.discord_cmd_prefix_new:
            channel = message.channel
            user = message.author
            if hasattr(channel, "send") and hasattr(user, "display_name"):
                await cog.prefix_new(channel, user)  # type: ignore[arg-type]
            return True

        if cmd == "status" and self.settings.discord_cmd_prefix_status:
            channel = message.channel
            user = message.author
            if hasattr(channel, "send") and hasattr(user, "display_name"):
                await cog.prefix_status(channel, user)  # type: ignore[arg-type]
            return True

        if cmd == "help":
            await self._send_prefix_help(message.channel)
            return True

        await self._send_prefix_help(message.channel)
        return True

    async def _handle_conversation_message(self, message: discord.Message, content: str, replied_message=None):
        """Handle a message in a conversation channel with optional web search."""
        from api.models.anthropic import MessagesRequest, Message
        from providers.rate_limit import GlobalRateLimiter
        from api.request_utils import get_token_count
        channel = message.channel
        user = message.author

        # Extract model early and validate (used throughout this method)
        discord_model = self._discord_model
        if discord_model is None:
            return
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

        # Check for auto-compact (if enabled)
        if self.settings.discord_auto_compact:
            if self.conversation_manager.should_compact(channel.id):
                await channel.send(
                    "🔄 Auto-compacting conversation...\n\n"
                    "*Tip: Run `/compact` manually to backup chat history to your DMs first.*"
                )
                cog = self.get_cog('NimbusCog')
                if isinstance(cog, NimbusCog) and isinstance(channel, discord.TextChannel):
                    await cog._do_compact_for_channel(channel)

        # Format message with username and user ID for context
        safe_content = content.replace("@everyone", "@every\u200bone").replace("@here", "@her\u200be")
        formatted_content = f"{user.display_name} (ID: {user.id}): {safe_content}"

        # Add reply context if this is a reply
        if replied_message:
            reply_author = replied_message.author.display_name
            reply_content = replied_message.content or "(no text content)"
            if len(reply_content) > 500:
                reply_content = reply_content[:500] + "..."
            formatted_content = f"[Replying to {reply_author}'s message: \"{reply_content}\"]\n{formatted_content}"

        # Get history
        history = self.conversation_manager.get_history_for_nim(channel.id)

        # Check if web search is enabled
        web_search_enabled = (
            getattr(self.settings, 'discord_enable_web_search', True)
            and discord_model is not None
        )

        # Build initial request with system prompt and optional web search tools
        messages = history + [{"role": "user", "content": formatted_content}]
        system_prompt = self.settings.discord_system_prompt
        request_data = MessagesRequest(
            model=discord_model,
            messages=[Message(role=m["role"], content=m["content"]) for m in messages],
            max_tokens=self.settings.discord_max_tokens,
            system=system_prompt,
            tools=WEB_SEARCH_TOOLS if web_search_enabled else None,
        )

        # Count tokens including system prompt
        input_tokens = get_token_count(
            request_data.messages, system_prompt, request_data.tools
        )

        # Log request
        channel_name = getattr(channel, 'name', 'DM')
        print(
            f"[DISCORD-LIVE] {user.display_name} ({user.id}) in #{channel_name}: "
            f"{content[:50]}{'...' if len(content) > 50 else ''}",
            flush=True
        )

        # Show typing indicator with periodic refresh and exponential backoff on rate limits
        import time

        # Get or initialize retry delay for this channel (starts at 10 seconds)
        base_interval = self._typing_retry_delays.get(channel.id, 10.0)

        # Start typing indicator - this will be refreshed periodically
        typing_task = None
        typing_active = True

        async def refresh_typing():
            """Periodically refresh typing indicator with exponential backoff on rate limits."""
            nonlocal typing_active, base_interval
            while typing_active:
                try:
                    async with channel.typing():
                        await asyncio.sleep(2.0)  # Brief sleep to ensure typing is registered
                except discord.HTTPException as e:
                    if e.status == 429:
                        # Rate limited - increase delay exponentially
                        base_interval = min(base_interval + 5.0, 60.0)  # Cap at 60s
                        self._typing_retry_delays[channel.id] = base_interval
                        logger.warning(f"Typing rate limited in channel {channel.id}, increased interval to {base_interval}s")
                    else:
                        logger.debug(f"Typing error in channel {channel.id}: {e}")
                except Exception:
                    pass

                if not typing_active:
                    break
                await asyncio.sleep(base_interval)

        # Start the typing refresh task
        typing_task = asyncio.create_task(refresh_typing())

        global_limiter = GlobalRateLimiter.get_instance()
        await global_limiter.wait_if_blocked()

        # Tool handling state
        tools_used = set()
        max_iterations = getattr(self.settings, 'discord_web_search_max_iterations', 10)
        max_results = getattr(self.settings, 'discord_web_search_max_results', 5)
        iteration = 0

        # Current request data (will be updated in loop with tool results)
        current_request = request_data
        current_input_tokens = input_tokens

        full_text = ""

        try:
            # Main loop: keep sending requests until model stops calling tools
            while iteration < max_iterations:
                iteration += 1
                request_id = f"discord_live_{uuid.uuid4().hex[:8]}"

                try:
                    async with global_limiter.concurrency_slot():
                        stream = self.provider.stream_response(
                            current_request, current_input_tokens, request_id=request_id, priority=RequestPriority.HIGH
                        )

                        # Track tool calls in this stream
                        current_tool_use = None
                        current_tool_input = ""
                        tool_results = []  # List of (tool_name, tool_input, tool_result)

                        async for chunk in stream:
                            if not chunk.strip():
                                continue
                            try:
                                event_data = chunk.split("data: ", 1)[-1].strip()
                                data = json.loads(event_data)

                                # Track tool_use start
                                if data.get("type") == "content_block_start":
                                    block = data.get("block", {})
                                    if block.get("type") == "tool_use":
                                        current_tool_use = {
                                            "id": block.get("id"),
                                            "name": block.get("name"),
                                            "input": ""
                                        }
                                        current_tool_input = ""

                                # Accumulate tool input
                                elif data.get("type") == "content_block_delta" and current_tool_use:
                                    delta = data.get("delta", {})
                                    if delta.get("type") == "input_json_delta":
                                        current_tool_input += delta.get("partial_json", "")

                                # Tool complete
                                elif data.get("type") == "content_block_stop" and current_tool_use:
                                    current_tool_use["input"] = current_tool_input
                                    tool_results.append(current_tool_use)
                                    tools_used.add(current_tool_use["name"])
                                    current_tool_use = None
                                    current_tool_input = ""

                                # Collect text for final output
                                elif data.get("type") == "content_block_delta":
                                    delta = data.get("delta", {})
                                    if delta.get("type") == "text_delta":
                                        full_text += delta.get("text", "")

                            except Exception:
                                continue

                        # If no tools were called, we're done
                        if not tool_results:
                            break

                except Exception as e:
                    logger.error(f"Stream error during web search iteration {iteration}: {e}")
                    break

                # Execute tool calls and build tool results
                tool_result_messages = []
                for tool in tool_results:
                    tool_id = tool["id"]
                    tool_name = tool["name"]
                    tool_input_str = tool["input"]

                    logger.info(f"[WEB SEARCH] tool={tool_name} input={tool_input_str!r}")

                    try:
                        tool_input = json.loads(tool_input_str) if tool_input_str else {}

                        if tool_name == "web_search":
                            query = tool_input.get("query", "")
                            result = await execute_web_search(query, max_results=max_results)
                        elif tool_name == "fetch_page":
                            url = tool_input.get("url", "")
                            offset = tool_input.get("offset", 0)
                            limit = tool_input.get("limit", 10000)
                            search = tool_input.get("search")
                            result = await execute_fetch_page(url, offset, limit, search)
                        else:
                            result = f"Unknown tool: {tool_name}"

                        logger.info(f"[WEB SEARCH] tool={tool_name} | result_len={len(result)}")
                        tool_result_messages.append({
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_id,
                                    "content": result
                                }
                            ]
                        })

                    except Exception as e:
                        logger.error(f"Tool {tool_name} execution failed: {e}")
                        tool_result_messages.append({
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_id,
                                    "content": f"Error executing tool: {e}",
                                    "is_error": True
                                }
                            ]
                        })

                # Build next request with tool results
                assistant_content = [{"type": "tool_use", **t} for t in tool_results]
                new_messages = list(current_request.messages) + [
                    {"role": "assistant", "content": assistant_content}
                ] + tool_result_messages

                current_request = MessagesRequest(
                    model=discord_model,
                    messages=[Message(role=str(m["role"]), content=m["content"]) for m in new_messages],  # type: ignore[arg-type, index]
                    max_tokens=self.settings.discord_max_tokens,
                    system=system_prompt,
                    tools=WEB_SEARCH_TOOLS if web_search_enabled else None,
                )
                current_input_tokens = get_token_count(
                    current_request.messages, system_prompt, current_request.tools
                )

        finally:
            # Stop typing indicator
            typing_active = False
            if typing_task:
                typing_task.cancel()
                try:
                    await typing_task
                except asyncio.CancelledError:
                    pass

        # Record successful typing completion (reset delay on success)
        self._typing_retry_delays[channel.id] = 10.0
        self._typing_locks[channel.id] = time.monotonic()

        # Add disclaimer prefix if web search tools were used
        if tools_used:
            full_text = "-# This response used online resources, please make sure to verify the information\n\n" + full_text

        # Store the assistant response in conversation (with tools_used)
        if full_text:
            safe_response = full_text.replace("@everyone", "@every\u200bone").replace("@here", "@her\u200be")
            self.conversation_manager.add_message_with_user(
                channel.id, "assistant", safe_response, None, "NIM",
                tools_used=list(tools_used),
                auto_compact=self.settings.discord_auto_compact
            )

        # Send response (split into chunks if too long for Discord 2000 char limit)
        content_out = full_text.strip() if full_text else "(No response)"
        # Strip @everyone/@here from bot's own output to prevent accidental mass-pings
        content_out = content_out.replace("@everyone", "@every\u200bone").replace("@here", "@her\u200be")
        if len(content_out) > 1900:
            # Split into chunks of ~1900 chars and send multiple messages
            chunks = [content_out[i:i+1900] for i in range(0, len(content_out), 1900)]
            for i, chunk in enumerate(chunks):
                if i == 0:
                    # First chunk: reply to the user's message
                    await message.reply(chunk)
                else:
                    # Subsequent chunks: send as follow-up in the same thread
                    await channel.send(chunk)
        else:
            await message.reply(content_out)

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
        """Handle messages in conversation channels.

        All messages are recorded in history. A response is only sent when:
          - the bot is @mentioned at start; OR
          - the message starts with the configured prefix; OR
          - DISCORD_REQUIRE_MENTION is false
        """
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
            print("[DEBUG] Skipping slash command", flush=True)
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

        content = message.content

        # Prevent accidental mass-pings to @everyone / @here
        content = content.replace("@everyone", "@every\u200bone").replace("@here", "@her\u200be")

        # Unconditionally save every message in a conversation channel to history,
        # regardless of whether the bot will respond. This ensures all channel
        # conversations are persisted to .discord_data/conversations.json.
        # Check if this is a reply to a bot message - if so, include the bot's message content
        reply_message = ""
        if message.reference and message.reference.message_id:
            try:
                replied_msg = await message.channel.fetch_message(message.reference.message_id)
                # Check if the replied message is from the bot
                if self.user and replied_msg.author.id == self.user.id:
                    reply_message = replied_msg.content or ""
            except Exception:
                pass  # Failed to fetch replied message, continue without reply context

        self.conversation_manager.add_message_with_user(
            message.channel.id, "user", content, message.author.id, message.author.display_name,
            reply_message=reply_message, auto_compact=self.settings.discord_auto_compact
        )

        # Check if this is a reply to the bot's message
        is_reply_to_bot = False
        if message.reference and message.reference.message_id and self.user:
            try:
                replied_msg = await message.channel.fetch_message(message.reference.message_id)
                if replied_msg.author.id == self.user.id:
                    is_reply_to_bot = True
            except Exception:
                pass  # Failed to fetch, treat as not a reply to bot

        is_mention = self._is_bot_mentioned(message)
        has_prefix = content.startswith(self.settings.discord_command_prefix)
        should_respond = is_mention or has_prefix or is_reply_to_bot or not self.settings.discord_require_mention

        # Leave @mention in content for full conversation context
        # (the bot tag shows who is being addressed)
        if is_mention:
            print(f"[DEBUG] Bot mentioned, content kept with mention: {content[:50]}", flush=True)

        # Handle prefix commands
        if has_prefix:
            prefix = self.settings.discord_command_prefix
            content_after_prefix = content[len(prefix):].strip()
            print(f"[DEBUG] Prefix detected, content after: {content_after_prefix[:50]}", flush=True)
            routed = await self._handle_prefix_command(message, content_after_prefix)
            if routed:
                return

        # Handle message replies for additional context
        replied_message = None
        if message.reference and message.reference.message_id:
            try:
                replied_message = await message.channel.fetch_message(message.reference.message_id)
                print(f"[DEBUG] Message is reply to: {replied_message.author.display_name}: {replied_message.content[:50]}", flush=True)
            except Exception as e:
                print(f"[DEBUG] Failed to fetch replied message: {e}", flush=True)

        # Queue message for sequential processing per channel
        if should_respond:
            await self._queue_message(
                message.channel.id,
                message,
                content,
                replied_message,
            )

    async def _queue_message(self, channel_id: int, message: discord.Message, content: str, replied_message=None):
        """Queue a message for sequential processing in the channel."""
        session = self.conversation_manager.get_session(channel_id)
        if session is None:
            session = self.conversation_manager.get_session(channel_id)
            if session is None:
                # Shouldn't happen, but create if needed
                from .conversation import ConversationSession
                session = ConversationSession(channel_id=channel_id)
                self.conversation_manager._sessions[channel_id] = session

        # Add message to queue (store full message object for reply support)
        await session.processing_queue.put({
            'message': message,
            'content': content,
            'replied_message': replied_message,
        })

        # Start processor if not already running
        if not session.is_processing:
            asyncio.create_task(self._process_message_queue(channel_id))

    async def _process_message_queue(self, channel_id: int):
        """Process messages in FIFO order for a channel."""
        session = self.conversation_manager.get_session(channel_id)
        if not session or session.is_processing:
            return

        session.is_processing = True
        try:
            while not session.processing_queue.empty():
                try:
                    msg_data = await asyncio.wait_for(session.processing_queue.get(), timeout=1.0)
                    # Retry up to 3 times on failure
                    max_retries = 3
                    last_error = None
                    for attempt in range(max_retries):
                        try:
                            await self._handle_conversation_message(
                                msg_data['message'],
                                msg_data['content'],
                                msg_data.get('replied_message')
                            )
                            last_error = None
                            break  # Success, exit retry loop
                        except Exception as e:
                            last_error = e
                            if attempt < max_retries - 1:
                                logger.warning(
                                    f"Error processing message in channel {channel_id} "
                                    f"(attempt {attempt + 1}/{max_retries}): {e}"
                                )
                                await asyncio.sleep(1.0)  # Brief delay before retry
                            else:
                                logger.error(
                                    f"Error processing message in channel {channel_id} "
                                    f"after {max_retries} attempts: {e}"
                                )
                                # Send error notification to the channel
                                try:
                                    await msg_data['message'].channel.send(
                                        "⚠️ An error occurred while processing your message. "
                                        "Please try again."
                                    )
                                except Exception:
                                    pass  # Can't even send error message, give up
                    session.processing_queue.task_done()
                except asyncio.TimeoutError:
                    break
                except Exception as e:
                    logger.error(f"Unexpected error in queue processing for channel {channel_id}: {e}")
                    break
        finally:
            session.is_processing = False

    async def _send_prefix_help(self, channel):
        """Send a help message listing available prefix commands."""
        prefix = self.settings.discord_command_prefix or "!!"
        lines = [f"**Prefix Commands** (use `{prefix}<cmd>`):", ""]
        if self.settings.discord_cmd_prefix_ask:
            lines.append(f"`{prefix}ask <question>` — Ask the AI a question with conversation history")
        if self.settings.discord_cmd_prefix_compact:
            lines.append(f"`{prefix}compact` — Summarize the conversation and start fresh")
        if self.settings.discord_cmd_prefix_new:
            lines.append(f"`{prefix}new` — Clear conversation without saving a summary")
        if self.settings.discord_cmd_prefix_status:
            lines.append(f"`{prefix}status` — Show bot status, rate limits, and stats")
        lines.append(f"`{prefix}help` — Show this help message")
        lines.append("")
        display = getattr(getattr(self, "user", None), "display_name", getattr(getattr(self, "user", None), "name", "bot"))
        lines.append(f"You can also just **@{display}** me directly to chat.")
        if not any([
            self.settings.discord_cmd_prefix_ask,
            self.settings.discord_cmd_prefix_compact,
            self.settings.discord_cmd_prefix_new,
            self.settings.discord_cmd_prefix_status,
        ]):
            lines.insert(1, "_(No prefix commands are currently enabled)_")
        await channel.send("\n".join(lines))

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
