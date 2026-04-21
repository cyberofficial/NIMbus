"""Centralized configuration using Pydantic Settings.

This configuration is exclusively for NVIDIA NIM API endpoints.
"""

import random
import string
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .nim import NimSettings

load_dotenv()


def generate_session_api_key() -> str:
    """Generate a random 32-char API key in format: 16chars.16chars"""
    chars = string.ascii_letters + string.digits
    first_half = ''.join(random.choices(chars, k=16))
    second_half = ''.join(random.choices(chars, k=16))
    return f"{first_half}.{second_half}"


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    This codebase exclusively uses NVIDIA NIM API endpoints.
    """

    # ==================== NVIDIA NIM Config ====================
    api_key: str = Field(default="", validation_alias="NVIDIA_NIM_API_KEY")

    # ==================== Model ====================
    # Model name without provider prefix (e.g., "meta/llama3-70b-instruct")
    model: str = "z-ai/glm5"

    # ==================== Provider Rate Limiting ====================
    provider_rate_limit: int = Field(default=40, validation_alias="PROVIDER_RATE_LIMIT")
    provider_rate_window: int = Field(
        default=60, validation_alias="PROVIDER_RATE_WINDOW"
    )
    provider_max_concurrency: int = Field(
        default=5, validation_alias="PROVIDER_MAX_CONCURRENCY"
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
    fast_prefix_detection: bool = True
    enable_network_probe_mock: bool = True
    enable_title_generation_skip: bool = True
    enable_suggestion_mode_skip: bool = True
    enable_filepath_extraction_mock: bool = True

    # ==================== NIM Settings ====================
    nim: NimSettings = Field(default_factory=NimSettings)  # type: ignore[arg-type]

    # ==================== Server ====================
    host: str = "0.0.0.0"
    port: int = 8082
    log_file: str = "server.log"
    proxy_api_key: str = Field(default="", validation_alias="PROXY_API_KEY")

    # ==================== Discord Bot ====================
    discord_bot_token: str = Field(default="", validation_alias="DISCORD_BOT_TOKEN")
    discord_guild_id: int = Field(default=0, validation_alias="DISCORD_GUILD_ID")
    discord_control_channel_id: int = Field(default=0, validation_alias="DISCORD_CONTROL_CHANNEL_ID")
    discord_conversation_category_id: int = Field(default=0, validation_alias="DISCORD_CONVERSATION_CATEGORY_ID")

    # Owner configuration for access control
    discord_owner_id: int = Field(default=0, validation_alias="DISCORD_OWNER_ID")
    discord_owner_only: bool = Field(default=True, validation_alias="DISCORD_OWNER_ONLY")

    # Token management for compaction
    discord_max_tokens: int = Field(default=202000, validation_alias="DISCORD_MAX_TOKENS")
    discord_compact_threshold: float = Field(default=0.8, validation_alias="DISCORD_COMPACT_THRESHOLD")

    # Rate limiting
    discord_user_cooldown: float = Field(default=10.0, validation_alias="DISCORD_USER_COOLDOWN")
    discord_server_limit: int = Field(default=20, validation_alias="DISCORD_SERVER_LIMIT")
    discord_server_window: float = Field(default=60.0, validation_alias="DISCORD_SERVER_WINDOW")

    # System prompt for Discord conversations
    discord_system_prompt: str = Field(
        default="You are a helpful Discord bot. Be friendly, casual, and conversational. "
               "Talk like a normal person - don't use formal analysis headers, bullet points, "
               "or structured formatting unless specifically asked. Keep responses natural and direct.",
        validation_alias="DISCORD_SYSTEM_PROMPT"
    )

    # Skip file attachments (future feature: process files)
    discord_skip_files: bool = Field(default=True, validation_alias="DISCORD_SKIP_FILES")

    @property
    def discord_enabled(self) -> bool:
        """Check if Discord bot is configured."""
        return bool(self.discord_bot_token and self.discord_guild_id)

    @field_validator("proxy_api_key", mode="after")
    @classmethod
    def validate_proxy_api_key(cls, v: str) -> str:
        """Auto-generate API key if blank or placeholder (fallback)."""
        if not v or v == "<replaceme>":
            return generate_session_api_key()
        return v

    @field_validator("model")
    @classmethod
    def validate_model_format(cls, v: str) -> str:
        """Validate model name format.

        Model should be in format: owner/model-name
        (e.g., "meta/llama3-70b-instruct", "qwen/qwen3.5-397b-a17b")
        """
        if not v or not v.strip():
            raise ValueError("Model name cannot be empty")
        v = v.strip()
        if "/" not in v:
            raise ValueError(
                f"Model must be in format 'owner/model-name'. "
                f"Got: {v!r}. Examples: 'meta/llama3-70b-instruct', 'qwen/qwen3.5-397b-a17b'"
            )
        return v

    @property
    def model_name(self) -> str:
        """Get the model name (same as model for NVIDIA NIM-only config)."""
        return self.model

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
