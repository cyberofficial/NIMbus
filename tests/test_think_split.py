"""Regression tests for inline <think> reasoning splitting."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.think_parser import split_think_content

# Observed in the field: NVIDIA consumed the opening <think> server-side
ORPHAN_SAMPLE = (
    "Quoting the prompt: user asked for git status.\n"
    "Let me run git status.</think>"
)


def test_orphan_close_tag_is_reasoning():
    thinking, remaining = split_think_content(ORPHAN_SAMPLE)
    assert thinking == "Quoting the prompt: user asked for git status.\nLet me run git status."
    assert remaining == ""


def test_orphan_close_tag_with_trailing_content():
    thinking, remaining = split_think_content("reasoning here</think>The answer is 42.")
    assert thinking == "reasoning here"
    assert remaining == "The answer is 42."


def test_well_formed_pair():
    thinking, remaining = split_think_content("<think>pondering</think>The answer.")
    assert thinking == "pondering"
    assert remaining == "The answer."


def test_unterminated_think_block():
    thinking, remaining = split_think_content("<think>still thinking")
    assert thinking == "still thinking"
    assert remaining == ""


def test_no_think_tags_passthrough():
    text = "Just a normal reply."
    thinking, remaining = split_think_content(text)
    assert thinking == ""
    assert remaining == text
