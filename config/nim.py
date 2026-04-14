"""NVIDIA NIM settings."""

import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class NimSettings(BaseModel):
    """NVIDIA NIM settings."""

    # All fields have defaults - Pylance/pyright reports false positives
    # pyright: reportCallIssue=false

    temperature: float = Field(default=1.0, ge=0.0)
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    top_k: int = -1
    max_tokens: int = Field(
        default_factory=lambda: int(os.environ.get("NIM_MAX_TOKENS", "202000")), ge=1
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

    reasoning_effort: Literal["low", "medium", "high"] = "high"
    include_reasoning: bool = True

    model_config = ConfigDict(extra="forbid")

    @field_validator("top_k")
    @classmethod
    def validate_top_k(cls, v):
        if v < -1:
            raise ValueError("top_k must be -1 or >= 0")
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
