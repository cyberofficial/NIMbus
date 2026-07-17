"""Centralized configuration using Pydantic Settings.

This configuration is exclusively for NVIDIA NIM API endpoints.
"""

import json
import random
import string
from dataclasses import field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .nim import NimSettings

load_dotenv()


def generate_session_api_key() -> str:
    """Generate a random 32-char API key in format: 16chars.16chars"""
    chars = string.ascii_letters + string.digits
    first_half = "".join(random.choices(chars, k=16))
    second_half = "".join(random.choices(chars, k=16))
    return f"{first_half}.{second_half}"


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    This codebase exclusively uses NVIDIA NIM API endpoints.
    """

    # ==================== NVIDIA NIM Config ====================
    api_key: str = Field(default="", validation_alias="NVIDIA_NIM_API_KEY")

    # ==================== Model ====================
    # Comma-separated model list mapping to Claude Code tiers by position:
    #   1 model:  All Claude tiers use the same NIM model
    #   2 models: Sonnet+Opus use first, Haiku uses second
    #   3 models: Sonnet, Opus, Haiku each get their own model
    # Format: owner/model-name (without provider prefix)
    # Examples:
    #   "deepseek-ai/deepseek-v4-flash"
    #   "qwen/qwen3-coder-480b-a35b-instruct,deepseek-ai/deepseek-v4-flash"
    #   "qwen/qwen3-coder-480b-a35b-instruct,minimaxai/minimax-m2.7,deepseek-ai/deepseek-v4-flash"
    model: str = "deepseek-ai/deepseek-v4-flash"

    # ==================== Provider Rate Limiting ====================
    provider_rate_limit: int = Field(default=40, validation_alias="PROVIDER_RATE_LIMIT")
    provider_rate_window: int = Field(
        default=60, validation_alias="PROVIDER_RATE_WINDOW"
    )
    provider_max_concurrency: int = Field(
        default=5, validation_alias="PROVIDER_MAX_CONCURRENCY"
    )

    # ==================== Request Queue ====================
    request_queue_enabled: bool = Field(
        default=True, validation_alias="REQUEST_QUEUE_ENABLED"
    )
    request_queue_max_concurrent: int = Field(
        default=32, ge=1, validation_alias="REQUEST_QUEUE_MAX_CONCURRENT"
    )
    request_queue_max_size: int = Field(
        default=600, ge=0, validation_alias="REQUEST_QUEUE_MAX_SIZE"
    )
    request_queue_timeout: float = Field(
        default=300.0, ge=1.0, validation_alias="REQUEST_QUEUE_TIMEOUT"
    )
    request_queue_num_workers: int = Field(
        default=4, ge=1, validation_alias="REQUEST_QUEUE_NUM_WORKERS"
    )
    request_queue_discord_priority: int = Field(
        default=2, ge=0, le=2, validation_alias="REQUEST_QUEUE_DISCORD_PRIORITY"
    )
    request_queue_api_priority: int = Field(
        default=1, ge=0, le=2, validation_alias="REQUEST_QUEUE_API_PRIORITY"
    )

    # ==================== Adaptive Rate Limiting ====================
    # Auto-restore adaptive rate limit after N successful requests without 429
    # 0 = never auto-restore (manual <nimrpm:reset> only). Default 5 requests.
    nim_rpm_reset: int = Field(default=5, ge=0, validation_alias="NIM_RPM_RESET")

    # ==================== Server Type ====================
    server_type: str = Field(default="stream", validation_alias="SERVER_TYPE")
    provider_max_wait_time: float = Field(
        default=30.0, ge=1.0, validation_alias="PROVIDER_MAX_WAIT_TIME"
    )
    provider_retry_on_truncation: int = Field(
        default=3, ge=0, validation_alias="PROVIDER_RETRY_ON_TRUNCATION"
    )
    provider_retry_delay: float = Field(
        default=1.0, ge=0, validation_alias="PROVIDER_RETRY_DELAY"
    )
    provider_resource_exhausted_retries: int = Field(
        default=10, ge=0, validation_alias="RESOURCE_EXHAUSTED_RETRIES"
    )

    # Echo the raw NVIDIA NIM reply to the console live, one line per chunk.
    # Each chunk carries a timestamp, so a frozen timestamp means the model is
    # stuck (not thinking). THINKING = reasoning_content, REPLY = generated text.
    # Works in both stream and buffer modes. Off by default (very chatty).
    show_nvidia_reply: bool = Field(default=False, validation_alias="SHOW_NIM_REPLY")

    # ==================== HTTP Client Timeouts ====================
    http_read_timeout: float = Field(
        default=300.0, validation_alias="HTTP_READ_TIMEOUT"
    )
    http_write_timeout: float = Field(
        default=10.0, validation_alias="HTTP_WRITE_TIMEOUT"
    )
    http_connect_timeout: float = Field(
        default=2.0, validation_alias="HTTP_CONNECT_TIMEOUT"
    )

    # ==================== Optimizations ====================
    # These speed up Claude Code by mocking/skipping unnecessary requests
    # NOTE: Suggestion mode skip and recap skip are currently disabled pending better detection logic
    fast_prefix_detection: bool = Field(default=True, validation_alias="FAST_PREFIX_DETECTION")
    enable_network_probe_mock: bool = Field(default=True, validation_alias="ENABLE_NETWORK_PROBE_MOCK")
    enable_title_generation_skip: bool = Field(default=True, validation_alias="ENABLE_TITLE_GENERATION_SKIP")
    enable_suggestion_mode_skip: bool = Field(default=False, validation_alias="ENABLE_SUGGESTION_MODE_SKIP")  # DISABLED: Too prone to false positives
    enable_filepath_extraction_mock: bool = Field(default=True, validation_alias="ENABLE_FILEPATH_EXTRACTION_MOCK")
    enable_recap_skip: bool = Field(default=False, validation_alias="ENABLE_RECAP_SKIP")  # DISABLED: Blocks legitimate recap requests

    # ==================== Model Swapper ====================
    swapper_enabled: bool = Field(default=False, validation_alias="SWAPPER_ENABLED")
    swapper_test_prompt: str = Field(
        default="Please reply with pong only, nothing else", validation_alias="SWAPPER_TEST_PROMPT"
    )
    swapper_test_timeout: float = Field(
        default=120.0, validation_alias="SWAPPER_TEST_TIMEOUT"
    )

    # ==================== NIM Settings ====================
    nim: NimSettings = Field(default_factory=NimSettings)  # type: ignore[arg-type]

    # ==================== MCP Server ====================
    web_search_fetch_timeout: float = Field(
        default=10.0, validation_alias="WEB_SEARCH_FETCH_TIMEOUT"
    )
    mcp_cache_ttl: int = Field(
        default=600, validation_alias="MCP_CACHE_TTL"
    )

    # ==================== Server ====================
    host: str = "0.0.0.0"
    port: int = 8082
    log_file: str = "server.log"
    proxy_api_key: str = Field(default="", validation_alias="PROXY_API_KEY")

    # ==================== Discord Bot ====================
    discord_bot_token: str = Field(default="", validation_alias="DISCORD_BOT_TOKEN")

    # Explicit enable/disable override for the Discord bot.
    # When set to "false" in .env, the bot will NOT start regardless of token/guild configuration.
    discord_enabled_field: bool = Field(default=True, validation_alias="DISCORD_ENABLED")

    # Multiple guilds/servers support (comma-separated list of guild IDs)
    @property
    def discord_guild_ids(self) -> set[int]:
        """Parse DISCORD_GUILD_ID as comma-separated list of guild IDs."""
        raw = getattr(self, "_discord_guild_id_raw", "")
        if not raw:
            return set()
        try:
            return set(int(gid.strip()) for gid in raw.split(",") if gid.strip())
        except ValueError:
            return set()

    @discord_guild_ids.setter
    def discord_guild_ids(self, value: set[int]) -> None:
        """Store guild IDs."""
        self._discord_guild_id_raw = ",".join(str(gid) for gid in value)

    # Legacy single guild support (for backward compatibility)
    discord_guild_id: int = Field(default=0, validation_alias="DISCORD_GUILD_ID")

    # Multiple control channels (comma-separated list)
    @property
    def discord_control_channel_ids(self) -> set[int]:
        """Parse DISCORD_CONTROL_CHANNEL_ID as comma-separated list."""
        raw = getattr(self, "_discord_control_channel_id_raw", "")
        if not raw:
            return set()
        try:
            return set(int(cid.strip()) for cid in raw.split(",") if cid.strip())
        except ValueError:
            return set()

    @discord_control_channel_ids.setter
    def discord_control_channel_ids(self, value: set[int]) -> None:
        """Store control channel IDs."""
        self._discord_control_channel_id_raw = ",".join(str(cid) for cid in value)

    # Single control channel ID (legacy)
    discord_control_channel_id: int = Field(
        default=0, validation_alias="DISCORD_CONTROL_CHANNEL_ID"
    )

    # Multiple conversation channels - specific channels to respond in (comma-separated)
    @property
    def discord_conversation_channel_ids(self) -> set[int]:
        """Parse DISCORD_CONVERSATION_CHANNEL_ID as comma-separated list of channel IDs."""
        raw = getattr(self, "discord_conversation_channel_id_raw", "")
        if not raw:
            return set()
        try:
            return set(int(cid.strip()) for cid in raw.split(",") if cid.strip())
        except ValueError:
            return set()

    @discord_conversation_channel_ids.setter
    def discord_conversation_channel_ids(self, value: set[int]) -> None:
        """Store conversation channel IDs."""
        self.discord_conversation_channel_id_raw = ",".join(str(cid) for cid in value)

    # Raw storage for conversation channel IDs (loaded from env)
    discord_conversation_channel_id_raw: str = Field(
        default="", validation_alias="DISCORD_CONVERSATION_CHANNEL_ID"
    )

    # Multiple conversation categories (comma-separated list)
    @property
    def discord_conversation_category_ids(self) -> set[int]:
        """Parse DISCORD_CONVERSATION_CATEGORY_ID as comma-separated list."""
        raw = getattr(self, "_discord_conversation_category_id_raw", "")
        if not raw:
            return set()
        try:
            return set(int(cid.strip()) for cid in raw.split(",") if cid.strip())
        except ValueError:
            return set()

    @discord_conversation_category_ids.setter
    def discord_conversation_category_ids(self, value: set[int]) -> None:
        """Store conversation category IDs."""
        self._discord_conversation_category_id_raw = ",".join(str(cid) for cid in value)

    # Single conversation category ID (legacy)
    discord_conversation_category_id: int = Field(
        default=0, validation_alias="DISCORD_CONVERSATION_CATEGORY_ID"
    )

    # Owner configuration for access control
    discord_owner_id: int = Field(default=0, validation_alias="DISCORD_OWNER_ID")
    discord_owner_only: bool = Field(
        default=True, validation_alias="DISCORD_OWNER_ONLY"
    )

    # Token management for compaction
    discord_max_tokens: int = Field(
        default=202000, validation_alias="DISCORD_MAX_TOKENS"
    )
    discord_compact_threshold: float = Field(
        default=0.8, validation_alias="DISCORD_COMPACT_THRESHOLD"
    )

    # Rate limiting
    discord_user_cooldown: float = Field(
        default=10.0, validation_alias="DISCORD_USER_COOLDOWN"
    )
    discord_server_limit: int = Field(
        default=20, validation_alias="DISCORD_SERVER_LIMIT"
    )
    discord_server_window: float = Field(
        default=60.0, validation_alias="DISCORD_SERVER_WINDOW"
    )

    # System prompt for Discord conversations
    discord_system_prompt: str = Field(
        default="You are a helpful Discord bot. Be friendly, casual, and conversational. "
        "Talk like a normal person - don't use formal analysis headers, bullet points, "
        "or structured formatting unless specifically asked. Keep responses natural and direct.",
        validation_alias="DISCORD_SYSTEM_PROMPT",
    )

    # Skip file attachments (future feature: process files)
    discord_skip_files: bool = Field(
        default=True, validation_alias="DISCORD_SKIP_FILES"
    )

    # Message splitting threshold (Discord has 2000 char limit)
    discord_split_threshold: int = Field(
        default=1900, validation_alias="DISCORD_SPLIT_THRESHOLD"
    )

    # Auto-compact feature toggle
    discord_auto_compact: bool = Field(
        default=True, validation_alias="DISCORD_AUTO_COMPACT"
    )

    # Discord model (optional, separate from main MODEL)
    # Allows using a different model for Discord bot vs API proxy
    discord_model_raw: str = Field(default="", validation_alias="DISCORD_MODEL")

    # Fable model override (optional)
    # Claude Code's new "fable" model has no settings.json env key (hard-coded).
    # By default, fable maps to the Opus NIM model. Set FABLE_OVERRIDE to a specific
    # NIM model (owner/model-name) to override that default. The "[1m]" context suffix
    # is supported and stripped automatically (e.g., "deepseek-ai/deepseek-v4-pro[1m]").
    fable_override_raw: str = Field(default="", validation_alias="FABLE_OVERRIDE")

    # Prefix command support
    discord_command_prefix: str = Field(
        default="!!", validation_alias="DISCORD_COMMAND_PREFIX"
    )

    # Mention requirement for live conversation
    discord_require_mention: bool = Field(
        default=True, validation_alias="DISCORD_REQUIRE_MENTION"
    )

    # Command toggles (all default to true)
    discord_cmd_ask: bool = Field(default=True, validation_alias="DISCORD_CMD_ASK")
    discord_cmd_compact: bool = Field(
        default=True, validation_alias="DISCORD_CMD_COMPACT"
    )
    discord_cmd_new: bool = Field(default=True, validation_alias="DISCORD_CMD_NEW")
    discord_cmd_status: bool = Field(
        default=True, validation_alias="DISCORD_CMD_STATUS"
    )
    discord_cmd_download: bool = Field(
        default=True, validation_alias="DISCORD_CMD_DOWNLOAD"
    )
    discord_cmd_block: bool = Field(default=True, validation_alias="DISCORD_CMD_BLOCK")
    discord_cmd_unblock: bool = Field(
        default=True, validation_alias="DISCORD_CMD_UNBLOCK"
    )
    discord_cmd_blocked: bool = Field(
        default=True, validation_alias="DISCORD_CMD_BLOCKED"
    )
    discord_cmd_newchannel: bool = Field(
        default=True, validation_alias="DISCORD_CMD_NEWCHANNEL"
    )

    discord_cmd_prefix_ask: bool = Field(
        default=True, validation_alias="DISCORD_CMD_PREFIX_ASK"
    )
    discord_cmd_prefix_compact: bool = Field(
        default=True, validation_alias="DISCORD_CMD_PREFIX_COMPACT"
    )
    discord_cmd_prefix_new: bool = Field(
        default=True, validation_alias="DISCORD_CMD_PREFIX_NEW"
    )
    discord_cmd_prefix_status: bool = Field(
        default=True, validation_alias="DISCORD_CMD_PREFIX_STATUS"
    )

    # Web search settings for Discord bot
    discord_enable_web_search: bool = Field(
        default=True, validation_alias="DISCORD_ENABLE_WEB_SEARCH"
    )
    discord_web_search_max_results: int = Field(
        default=5, validation_alias="DISCORD_WEB_SEARCH_MAX_RESULTS"
    )
    discord_web_search_max_iterations: int = Field(
        default=10, validation_alias="DISCORD_WEB_SEARCH_MAX_ITERATIONS"
    )
    discord_web_search_max_result_size: int = Field(
        default=5000, validation_alias="DISCORD_WEB_SEARCH_MAX_RESULT_SIZE"
    )
    discord_web_search_include_in_history: bool = Field(
        default=True, validation_alias="DISCORD_WEB_SEARCH_INCLUDE_IN_HISTORY"
    )

    @property
    def discord_enabled(self) -> bool:
        """Check if Discord bot is enabled.

        Respects the explicit DISABLED override first; falls back to checking
        that both a bot token and a guild ID are present.
        """
        if not self.discord_enabled_field:
            return False
        return bool(self.discord_bot_token and self.discord_guild_id)

    @property
    def discord_model(self) -> str | None:
        """Get the resolved Discord model.

        Resolution order:
        1. If DISCORD_MODEL explicitly set in env, use it
        2. Else if MODEL set and not windows:settings.json, use first model from MODEL
        3. Else if MODEL=windows:settings.json, use first available from:
           - ANTHROPIC_DEFAULT_OPUS_MODEL
           - ANTHROPIC_DEFAULT_SONNET_MODEL
           - ANTHROPIC_DEFAULT_HAIKU_MODEL
           (stripping [1m] suffix if present)
        4. Returns None if no model can be resolved (bot will not start)
        """
        # 1. Explicit DISCORD_MODEL
        if self.discord_model_raw:
            return _to_full_nim_model(self.discord_model_raw.strip())

        # 2. MODEL set and not windows:settings.json
        if self.model and self.model != "windows:settings.json":
            parts = [m.strip() for m in self.model.split(",") if m.strip()]
            if parts:
                return _to_full_nim_model(parts[0])

        # 3. MODEL=windows:settings.json - read from Claude settings in Opus->Sonnet->Haiku order
        if self.model == "windows:settings.json":
            # Reuse existing logic to read models from Claude settings
            # _model_list_from_claude_settings returns list: [sonnet, opus, haiku]
            # We want Opus (index 1), then Sonnet (index 0), then Haiku (index 2)
            models = self._model_list_from_claude_settings()
            for idx in (1, 0, 2):  # Opus, Sonnet, Haiku
                if idx < len(models) and models[idx]:
                    # Strip [1m] if present (handled in _model_list_from_claude_settings already)
                    return _to_full_nim_model(models[idx].strip())

        # No model available
        return None

    @property
    def fable_model(self) -> str:
        """Get the resolved fable model.

        Resolution order:
        1. If FABLE_OVERRIDE explicitly set in env, use it (strip [1m] if present)
        2. Otherwise, default to whatever the Opus model resolves to
        """
        # 1. Explicit FABLE_OVERRIDE
        if self.fable_override_raw:
            val = self.fable_override_raw.strip()
            if val.endswith("[1m]"):
                val = val[:-4]
            return _to_full_nim_model(val)

        # 2. Default to Opus model
        return self.get_model_for_claude("claude-opus-4-7")

    def is_conversation_channel(
        self, channel_id: int, category_id: int | None = None
    ) -> bool:
        """Check if a channel is a valid conversation channel.

        Priority:
        1. If specific conversation channels are configured, check if channel_id is in the list
        2. If conversation categories are configured, check if category_id matches
        3. If neither, return False (no allowed channels)

        If both channels AND categories are set, channel must be in either list.
        """
        channel_ids = self.discord_conversation_channel_ids
        category_ids = self.discord_conversation_category_ids

        # Fallback to single category for backward compatibility
        # (bot.py has the same fallback in its is_conversation_channel method)
        if not category_ids and self.discord_conversation_category_id:
            category_ids = {self.discord_conversation_category_id}

        # Check if in specific channels list
        in_channels = channel_id in channel_ids if channel_ids else False

        # Check if in categories (direct match or passed category_id)
        in_categories = False
        if category_ids and category_id is not None:
            in_categories = category_id in category_ids

        # If both configured, check either
        if channel_ids and category_ids:
            return in_channels or in_categories

        # If only channels configured
        if channel_ids:
            return in_channels

        # If only categories configured
        if category_ids:
            return in_categories

        # Nothing configured - allow nothing (safer default)
        return False

    @field_validator("proxy_api_key", mode="after")
    @classmethod
    def validate_proxy_api_key(cls, v: str) -> str:
        """Auto-generate API key if blank or placeholder (fallback)."""
        if not v or v == "<replaceme>":
            return generate_session_api_key()
        return v

    @field_validator("server_type", mode="after")
    @classmethod
    def validate_server_type(cls, v: str) -> str:
        """Validate server_type is 'stream' or 'buffer'."""
        if v not in ("stream", "buffer"):
            raise ValueError(f"SERVER_TYPE must be 'stream' or 'buffer', got: {v!r}")
        return v

    @field_validator("model")
    @classmethod
    def validate_model_format(cls, v: str) -> str:
        """Validate model name format.

        MODEL may be a single model or comma-separated list.
        Each entry must be in format: owner/model-name
        (e.g., "meta/llama3-70b-instruct", "qwen/qwen3.5-397b-a17b")

        Special value "windows:settings.json" reads models from
        Claude Code's settings.json instead.
        """
        if not v or not v.strip():
            raise ValueError("Model name cannot be empty")
        v = v.strip()
        if v == "windows:settings.json":
            return v  # special sentinel, skip format check
        for part in v.split(","):
            part = part.strip()
            if not part:
                continue
            if "/" not in part:
                raise ValueError(
                    f"Each model in MODEL must be in format 'owner/model-name'. "
                    f"Got invalid entry: {part!r}. "
                    f"Examples: 'meta/llama3-70b-instruct', 'qwen/qwen3.5-397b-a17b'"
                )
        return v

    @field_validator("nim", mode="after")
    @classmethod
    def disable_thinking_for_multi_model(cls, v: NimSettings, info) -> NimSettings:
        """Force-disable thinking when multiple models are configured.

        Thinking/reasoning parameters (thinking, reasoning_split, chat_template_kwargs,
        reasoning_effort, include_reasoning, return_tokens_as_token_ids) are not
        universally supported across NIM models (e.g., Qwen rejects them at the API level).
        When running multi-model setups, disable thinking to prevent API errors.

        When MODEL=windows:settings.json, we can't count models from the env var,
        so thinking is left as-is (preserving the user's NIM_THINKING setting).
        """
        model_raw = info.data.get("model", "")
        if model_raw == "windows:settings.json":
            return v  # can't determine count, keep user's thinking setting
        model_count = len([m.strip() for m in model_raw.split(",") if m.strip()])
        if model_count > 1 and v.thinking:
            from loguru import logger

            logger.info(
                "NIM config: {} models configured in MODEL - force-disabling thinking "
                "(unsupported by some models like qwen). Set a single MODEL to re-enable.",
                model_count,
            )
            v.thinking = False
        return v

    @property
    def model_list(self) -> list[str]:
        """Parse MODEL as comma-separated list of model names.

        Special value "windows:settings.json" reads models from
        Claude Code's settings.json (USERPROFILE/.claude/settings.json)
        instead, looking up ANTHROPIC_DEFAULT_SONNET_MODEL,
        ANTHROPIC_DEFAULT_OPUS_MODEL, and ANTHROPIC_DEFAULT_HAIKU_MODEL
        from the env section.
        """
        if self.model == "windows:settings.json":
            return self._model_list_from_claude_settings()
        return [m.strip() for m in self.model.split(",") if m.strip()]

    def _model_list_from_claude_settings(self) -> list[str]:
        """Read model list from Claude Code settings.json.

        Reads ANTHROPIC_DEFAULT_SONNET_MODEL, _OPUS_, _HAIKU_
        from USERPROFILE/.claude/settings.json env section.
        Strips the [1m] suffix and maps short names to full NIM model IDs.
        Falls back to deepseek-ai/deepseek-v4-flash if anything fails.
        """
        settings_path = Path.home() / ".claude" / "settings.json"
        if not settings_path.exists():
            return ["deepseek-ai/deepseek-v4-flash"]
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            env = data.get("env", {})
            models: list[str] = []
            for key in [
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL",
            ]:
                val = env.get(key, "")
                if val:
                    if val.endswith("[1m]"):
                        val = val[:-4]
                    # Map short names (Claude Code convention) to full NIM model IDs
                    models.append(_to_full_nim_model(val))
            return models if models else ["deepseek-ai/deepseek-v4-flash"]
        except json.JSONDecodeError, OSError:
            return ["deepseek-ai/deepseek-v4-flash"]

    # Claude model ID keyword → position in MODEL list
    # Claude API IDs: claude-sonnet-4-6, claude-opus-4-7, claude-haiku-4-5-20251001
    _CLAUDE_MODEL_MAP: dict[str, int] = {
        "sonnet": 0,  # Default/Sonnet 4.6
        "opus": 1,  # Opus 4.7
        "haiku": 2,  # Haiku 4.5
    }

    def get_model_for_claude(self, claude_id: str) -> str:
        """Map a Claude model ID to the corresponding NIM model by position.

        Uses substring matching on the Claude model ID to determine which tier
        it belongs to, then selects the NIM model from the corresponding position
        in the comma-separated MODEL list.

        Mapping (based on comma-separated positions in MODEL):
          1 model:  position 0 for everything
          2 models: position 0 for Sonnet+Opus, position 1 for Haiku
          3 models: position 0 for Sonnet, 1 for Opus, 2 for Haiku

        Falls back to models[0] if the Claude ID doesn't match any known tier.

        Prefix "nim:" bypasses tier mapping and resolves directly via NVIDIA catalog.
        """
        # 0. nim: prefix — direct NIM model lookup (bypasses tier mapping)
        if claude_id.startswith("nim:"):
            return _to_full_nim_model(claude_id[4:])

        models = self.model_list
        if not models:
            return self.model
        claude_lower = claude_id.lower()

        # 1. Check for fable FIRST (new model, hard-coded in Cline, defaults to Opus)
        if "fable" in claude_lower:
            return self.fable_model

        # 2. Try Claude tier keyword matching (e.g. "claude-opus-4-7" → "opus")
        for keyword, position in self._CLAUDE_MODEL_MAP.items():
            if keyword in claude_lower:
                target = min(position, len(models) - 1)
                return models[target]

        # 3. Check if the incoming name is already a model in our list
        #    (handles windows:settings.json where Claude Code sends NIM names
        #    like "kimi-k2.6" directly from ANTHROPIC_DEFAULT_OPUS_MODEL)
        for m in models:
            if claude_id in m or m in claude_id:
                return m

        # 4. Unknown model → fall back to first NIM model
        return models[0]

    @property
    def model_name(self) -> str:
        """Get the primary model name (first in the list).

        Kept for backward compatibility; prefer get_model_for_claude()
        for request-specific mapping.
        """
        return self.model_list[0] if self.model_list else self.model

    @staticmethod
    def _generate_env_alias(field_name: str) -> str:
        """Convert snake_case field name to PREFIX_UPPER_SNAKE env var.

        Pattern: field_name like "provider_rate_limit" -> "PROVIDER_RATE_LIMIT"
        For fields with explicit validation_alias, return the field name (explicit takes precedence).
        """
        # Special cases that don't follow the PREFIX_FIELD pattern
        special = {
            "api_key": "NVIDIA_NIM_API_KEY",   # not API_KEY
            "model": "MODEL",                  # not MODEL_MODEL
            "discord_enabled_field": "DISCORD_ENABLED",  # not DISCORD_ENABLED_FIELD
            "nim": None,                       # nested settings, no alias
        }
        if field_name in special:
            alias = special[field_name]
            if alias is None:
                # For nested settings with no alias, return field name as fallback
                return field_name.upper()
            return alias

        # Fields that should NOT have env aliases (constants, internal fields)
        no_alias = {
            "host", "port", "log_file", "proxy_api_key",  # server config
            "swapper_enabled", "swapper_test_prompt", "swapper_test_timeout",
            "web_search_fetch_timeout", "mcp_cache_ttl",
            "discord_bot_token", "discord_guild_id", "discord_control_channel_id",
            "discord_conversation_category_id", "discord_conversation_channel_id",
            "discord_owner_id", "discord_owner_only", "discord_max_tokens",
            "discord_compact_threshold", "discord_user_cooldown", "discord_server_limit",
            "discord_server_window", "discord_system_prompt", "discord_skip_files",
            "discord_split_threshold", "discord_auto_compact", "discord_model",
            "discord_cmd_ask", "discord_cmd_compact", "discord_cmd_new", "discord_cmd_status",
            "discord_cmd_download", "discord_cmd_block", "discord_cmd_unblock", "discord_cmd_blocked",
            "discord_cmd_newchannel", "discord_command_prefix", "discord_require_mention",
            "discord_cmd_prefix_ask", "discord_cmd_prefix_compact", "discord_cmd_prefix_new", "discord_cmd_prefix_status",
            "discord_enable_web_search", "discord_web_search_max_results", "discord_web_search_max_iterations",
            "discord_web_search_max_result_size", "discord_web_search_include_in_history",
            "provider_resource_exhausted_retries",
            "fable_override",
        }

        if field_name in no_alias:
            # Return field name as fallback - explicit validation_alias will take precedence
            return field_name.upper()

        # Default: convert to PREFIX_UPPER_SNAKE
        # field_name like "provider_rate_limit" -> "PROVIDER_RATE_LIMIT"
        parts = field_name.split("_")
        if len(parts) == 1:
            return field_name.upper()
        prefix = parts[0].upper()
        rest = "_".join(parts[1:]).upper()
        return f"{prefix}_{rest}"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        alias_generator=_generate_env_alias,
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


_NVIDIA_MODEL_CACHE: dict[str, str] | None = None
"""Cache of short_name -> full NIM ID, populated from NVIDIA /v1/models."""


def _fetch_nvidia_models() -> dict[str, str]:
    """Fetch available models from NVIDIA and build short→full mapping."""
    global _NVIDIA_MODEL_CACHE
    if _NVIDIA_MODEL_CACHE is not None:
        return _NVIDIA_MODEL_CACHE
    try:
        import httpx

        resp = httpx.get("https://integrate.api.nvidia.com/v1/models", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            mapping: dict[str, str] = {}
            for m in data.get("data", []):
                full_id = m.get("id", "")
                if "/" in full_id:
                    short = full_id.split("/", 1)[1]
                    mapping[short] = full_id
            _NVIDIA_MODEL_CACHE = mapping
            return mapping
    except Exception:
        pass
    _NVIDIA_MODEL_CACHE = {}
    return {}


def startup_model_cache() -> dict[str, str]:
    """Pre-warm NVIDIA model cache at startup to avoid first-request latency."""
    return _fetch_nvidia_models()


def _to_full_nim_model(name: str) -> str:
    """Map a short model name to a full NIM model ID (org/model-name).

    Dynamically queries NVIDIA's /v1/models endpoint (no auth needed)
    to find the correct org prefix for short names.
    """
    if "/" in name:
        return name
    # Try dynamic lookup from NVIDIA's model catalog
    mapping = _fetch_nvidia_models()
    if name in mapping:
        return mapping[name]
    # Last resort: return as-is and let NVIDIA reject it clearly
    return name


# ──────────────────────────────────────────────────────────────────────
# Reasoning Budgets Config
# ──────────────────────────────────────────────────────────────────────

from dataclasses import dataclass
from fnmatch import fnmatch


@dataclass
class ReasoningConfig:
    """Model-specific reasoning configuration."""
    max_reasoning_budget: int = 16384
    supports_thinking: bool = False
    thinking_style: str = "default"
    effort_mapping: dict[str, str] = field(default_factory=lambda: {"low": "low", "medium": "medium", "high": "high"})
    budget_per_effort: dict[str, int] = field(default_factory=lambda: {"high": 16384, "medium": 8192, "low": 2048})
    thinking_style: str = "default"  # "nemotron", "deepseek", "minimax", "default"


_REASONING_CONFIG_CACHE: dict[str, ReasoningConfig] = {}
_REASONING_CONFIG_LOADED = False
_REASONING_CONFIG_RAW: dict | None = None


def _load_reasoning_config() -> dict:
    """Load reasoning_config.json from project root directory."""
    global _REASONING_CONFIG_RAW
    if _REASONING_CONFIG_RAW is not None:
        return _REASONING_CONFIG_RAW

    config_path = Path(__file__).parent.parent / "reasoning_config.json"
    if not config_path.exists():
        _REASONING_CONFIG_RAW = {"models": {}, "defaults": {}}
        return _REASONING_CONFIG_RAW

    with config_path.open("r", encoding="utf-8") as f:
        _REASONING_CONFIG_RAW = json.load(f) or {"models": {}, "defaults": {}}
    return _REASONING_CONFIG_RAW


def _match_model_config(model: str, config: dict) -> tuple[int, bool, dict[str, str], dict[str, int], str]:
    """Match model against config patterns (supports glob), return (max_budget, supports_thinking, effort_mapping, budget_per_effort, thinking_style)."""
    # Check exact match first
    if model in config.get("models", {}):
        m = config["models"][model]
        effort_mapping = {}
        budget_per_effort = {}
        for item in m.get("efforts", []):
            parts = item.split(":")
            if len(parts) == 3:
                effort_level, mapped, budget = parts
                effort_mapping[effort_level] = mapped
                budget_per_effort[effort_level] = int(budget)
            elif len(parts) == 2:
                effort_level, second = parts
                # Second part could be budget (number) or mapped effort name (string)
                try:
                    budget_per_effort[effort_level] = int(second)
                    effort_mapping[effort_level] = effort_level
                except ValueError:
                    # Not a number - it's a mapped effort name
                    effort_mapping[effort_level] = second
                    # No budget specified, use default later
        return (
            m.get("max_budget", 16384),
            m.get("supports_thinking", False),
            effort_mapping,
            budget_per_effort,
            m.get("thinking_style", "default")
        )

    # Check glob patterns
    for pattern, m in config.get("models", {}).items():
        if "*" in pattern or "?" in pattern:
            if fnmatch(model, pattern):
                effort_mapping = {}
                budget_per_effort = {}
                for item in m.get("efforts", []):
                    parts = item.split(":")
                    if len(parts) == 3:
                        effort_level, mapped, budget = parts
                        effort_mapping[effort_level] = mapped
                        budget_per_effort[effort_level] = int(budget)
                    elif len(parts) == 2:
                        effort_level, second = parts
                        try:
                            budget_per_effort[effort_level] = int(second)
                            effort_mapping[effort_level] = effort_level
                        except ValueError:
                            effort_mapping[effort_level] = second
                return (
                    m.get("max_budget", 16384),
                    m.get("supports_thinking", False),
                    effort_mapping,
                    budget_per_effort,
                    m.get("thinking_style", "default")
                )

    # Fallback to defaults
    defaults = config.get("defaults", {})
    effort_mapping = {}
    budget_per_effort = {}
    for item in defaults.get("efforts", []):
        parts = item.split(":")
        if len(parts) == 3:
            effort_level, mapped, budget = parts
            effort_mapping[effort_level] = mapped
            budget_per_effort[effort_level] = int(budget)
        elif len(parts) == 2:
            effort_level, second = parts
            try:
                budget_per_effort[effort_level] = int(second)
                effort_mapping[effort_level] = effort_level
            except ValueError:
                effort_mapping[effort_level] = second
    return (
        defaults.get("max_budget", 16384),
        defaults.get("supports_thinking", False),
        effort_mapping,
        budget_per_effort,
        defaults.get("thinking_style", "default")
    )


def get_reasoning_config(model: str) -> ReasoningConfig:
    """Get ReasoningConfig for a model (supports glob patterns like 'deepseek-*').

    Thread-safe and caches results. Loads config from reasoning_budgets.yaml
    on first call.

    Args:
        model: Full NIM model ID (e.g., "nvidia/nemotron-3-ultra-550b-a55b")

    Returns:
        ReasoningConfig with max_reasoning_budget, supports_thinking, effort_mapping, thinking_style
    """
    if model in _REASONING_CONFIG_CACHE:
        return _REASONING_CONFIG_CACHE[model]

    config = _load_reasoning_config()
    max_budget, supports_thinking, effort_mapping, budget_per_effort, thinking_style = _match_model_config(model, config)

    result = ReasoningConfig(
        max_reasoning_budget=max_budget,
        supports_thinking=supports_thinking,
        effort_mapping=effort_mapping,
        budget_per_effort=budget_per_effort,
        thinking_style=thinking_style,
    )
    _REASONING_CONFIG_CACHE[model] = result
    return result


def clear_reasoning_config_cache() -> None:
    """Clear the reasoning config cache (useful for testing/config reload)."""
    _REASONING_CONFIG_CACHE.clear()
