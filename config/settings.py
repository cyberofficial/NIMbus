"""Centralized configuration using Pydantic Settings.

This configuration is exclusively for NVIDIA NIM API endpoints.
"""

import json
import random
import string
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
    fast_prefix_detection: bool = True
    enable_network_probe_mock: bool = True
    enable_title_generation_skip: bool = True
    enable_suggestion_mode_skip: bool = False  # DISABLED: Too prone to false positives
    enable_filepath_extraction_mock: bool = True
    enable_recap_skip: bool = False  # DISABLED: Blocks legitimate recap requests

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

    # ==================== Server ====================
    host: str = "0.0.0.0"
    port: int = 8082
    log_file: str = "server.log"
    proxy_api_key: str = Field(default="", validation_alias="PROXY_API_KEY")

    # ==================== Discord Bot ====================
    discord_bot_token: str = Field(default="", validation_alias="DISCORD_BOT_TOKEN")

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

    @property
    def discord_enabled(self) -> bool:
        """Check if Discord bot is configured."""
        return bool(self.discord_bot_token and self.discord_guild_id)

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
        """
        models = self.model_list
        if not models:
            return self.model
        claude_lower = claude_id.lower()

        # 1. Try Claude tier keyword matching (e.g. "claude-opus-4-7" → "opus")
        for keyword, position in self._CLAUDE_MODEL_MAP.items():
            if keyword in claude_lower:
                target = min(position, len(models) - 1)
                return models[target]

        # 2. Check if the incoming name is already a model in our list
        #    (handles windows:settings.json where Claude Code sends NIM names
        #    like "kimi-k2.6" directly from ANTHROPIC_DEFAULT_OPUS_MODEL)
        for m in models:
            if claude_id in m or m in claude_id:
                return m

        # 3. Unknown model → fall back to first NIM model
        return models[0]

    @property
    def model_name(self) -> str:
        """Get the primary model name (first in the list).

        Kept for backward compatibility; prefer get_model_for_claude()
        for request-specific mapping.
        """
        return self.model_list[0] if self.model_list else self.model

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
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
