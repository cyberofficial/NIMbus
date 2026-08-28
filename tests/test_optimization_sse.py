"""Regression tests for buffered response -> SSE conversion."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.optimization_handlers import optimization_response_to_sse


def _events(response):
    return list(optimization_response_to_sse(response, input_tokens=10))


def test_tool_use_block_is_not_dropped():
    response = {
        "id": "msg_test",
        "model": "deepseek-ai/deepseek-v4-pro-0813",
        "stop_reason": "tool_use",
        "content": [
            {"type": "text", "text": "Running it now."},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "Bash",
                "input": {"command": "git status"},
            },
        ],
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    joined = "\n".join(_events(response))

    # Text block still present
    assert '"type": "text"' in joined
    assert "Running it now." in joined

    # tool_use block must be emitted, not dropped
    assert '"type": "tool_use"' in joined
    assert '"name": "Bash"' in joined
    assert "input_json_delta" in joined
    # args arrive as JSON inside the SSE data line (escaped there)
    assert "git status" in joined

    # stop_reason preserved
    assert '"stop_reason": "tool_use"' in joined


def test_tool_only_response():
    response = {
        "id": "msg_test",
        "model": "m",
        "stop_reason": "tool_use",
        "content": [
            {"type": "tool_use", "id": "toolu_2", "name": "Read", "input": {}},
        ],
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }
    joined = "\n".join(_events(response))
    assert '"type": "tool_use"' in joined
    assert '"name": "Read"' in joined
    # empty input -> no input_json_delta, still a complete block
    assert "input_json_delta" not in joined
    assert '"stop_reason": "tool_use"' in joined


def test_empty_text_block_skipped():
    response = {
        "id": "msg_test",
        "model": "m",
        "stop_reason": "end_turn",
        "content": [
            {"type": "text", "text": ""},
            {"type": "text", "text": "hello"},
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    joined = "\n".join(_events(response))
    # one block = event line + type field
    assert joined.count("content_block_start") == 2
    assert "hello" in joined
