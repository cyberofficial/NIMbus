"""NVIDIA NIM settings."""

import json
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _load_reasoning_effort() -> Literal['low', 'medium', 'high']:
    """Load reasoning effort from environment variable."""
    value = os.environ.get("NIM_REASONING_EFFORT", "high").lower()
    if value not in ("low", "medium", "high"):
        value = "high"
    return value  # type: ignore[return-value]


def _load_reasoning_effort_mappings() -> dict[str, dict[str, str]] | None:
    """Load reasoning effort mappings from environment variable as JSON."""
    raw = os.environ.get("NIM_REASONING_EFFORT_MAPPINGS", "")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


class NimSettings(BaseModel):
    """NVIDIA NIM settings."""

    # All fields have defaults - Pylance/pyright reports false positives
    # pyright: disable=reportCallIssue  # All fields have defaults - Pylance/pyright reports false positives

    temperature: float = Field(default=1.0, ge=0.0)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    top_k: int = -1
    max_tokens: int = Field(
        default_factory=lambda: int(os.environ.get("NIM_MAX_TOKENS", "32000")), ge=1
    )
    presence_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)
    frequency_penalty: float = Field(default=0.0, ge=-2.0, le=2.0)

    min_p: float = Field(default=0.0, ge=0.0, le=1.0)
    repetition_penalty: float = Field(default=1.0, ge=0.0)

    seed: int | None = None
    stop: str | None = None

    parallel_tool_calls: bool = True
    return_tokens_as_token_ids: bool = False
    include_stop_str_in_output: bool = False
    ignore_eos: bool = False

    min_tokens: int = Field(default=0, ge=0)
    chat_template: str | None = None
    request_id: str | None = None

    reasoning_effort: Literal["low", "medium", "high"] = Field(
        default_factory=_load_reasoning_effort
    )
    include_reasoning: bool = True
    thinking: bool = Field(
        default_factory=lambda: os.environ.get("NIM_THINKING", "true").lower()
        not in ("false", "0", "no", "off")
    )
    reasoning_effort_mappings: dict[str, dict[str, str]] | None = Field(
        default_factory=_load_reasoning_effort_mappings
    )

    # NEW: Reasoning budget (0 = auto from model config, -1 = unlimited)
    reasoning_budget: int = Field(
        default_factory=lambda: int(os.environ.get("NIM_REASONING_BUDGET", "0"))
    )

    # NEW: Enable thinking globally (overrides model config if false)
    enable_thinking: bool = Field(
        default_factory=lambda: os.environ.get("NIM_ENABLE_THINKING", "true").lower()
        not in ("false", "0", "no", "off")
    )

    # NEW: Chat template kwargs effort flags
    chat_template_enable_thinking: bool = Field(
        default_factory=lambda: os.environ.get("NIM_CHAT_TEMPLATE_ENABLE_THINKING", "true").lower()
        not in ("false", "0", "no", "off")
    )
    chat_template_low_effort: bool = Field(
        default_factory=lambda: os.environ.get("NIM_CHAT_TEMPLATE_LOW_EFFORT", "false").lower()
        in ("true", "1", "yes", "on")
    )
    chat_template_medium_effort: bool = Field(
        default_factory=lambda: os.environ.get("NIM_CHAT_TEMPLATE_MEDIUM_EFFORT", "false").lower()
        in ("true", "1", "yes", "on")
    )
    chat_template_high_effort: bool = Field(
        default_factory=lambda: os.environ.get("NIM_CHAT_TEMPLATE_HIGH_EFFORT", "false").lower()
        in ("true", "1", "yes", "on")
    )

    def get_effort_map_for_model(self, model: str) -> dict[str, str]:
        """Get effort level mapping for a specific model."""
        model_lower = model.lower()
        if self.reasoning_effort_mappings:
            for pattern, mapping in self.reasoning_effort_mappings.items():
                if pattern in model_lower or model_lower.startswith(pattern):
                    return mapping
        # Return empty dict if no mapping found - will fall back to defaults
        return {}

    model_config = ConfigDict(extra="forbid")

    @field_validator("top_k", mode="after")
    @classmethod
    def validate_top_k(cls, v):
        if v < -1:
            raise ValueError("top_k must be -1 or >= 0")
        return v

    @field_validator("reasoning_budget", mode="after")
    @classmethod
    def validate_reasoning_budget(cls, v):
        if v < -1:
            raise ValueError("reasoning_budget must be -1 (unlimited) or >= 0")
        return v

    @field_validator("seed", mode="before")
    @classmethod
    def parse_optional_int(cls, v):
        if v == "" or v is None:
            return None
        return int(v)

    @field_validator("stop", "chat_template", "request_id", mode="before")
    @classmethod
    def parse_optional_str(cls, v):
        if v == "":
            return None
        return v
