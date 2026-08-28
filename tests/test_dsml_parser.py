"""Regression tests for DeepSeek DSML tool-call parsing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from providers.dsml_parser import (
    DsmlParser,
    is_dsml_model,
    normalize_dsml_markup,
    parse_dsml_tool_calls,
)

# Exact degraded sample from the field bug report
DEGRADED_SAMPLE = (
    "I will search several relevant directions first."
    '<||DSML||tool_calls><||DSML||invoke name="doc_knowlegebase">'
    '<||DSML||parameter name="query" string="true">bond detail fields display'
    "</||DSML||parameter></||DSML||invoke></||DSML||tool_calls>"
)

CANONICAL_SAMPLE = (
    "I will run the command."
    "<｜DSML｜tool_calls>\n"
    '<｜DSML｜invoke name="Bash">\n'
    '<｜DSML｜parameter name="command" string="true">git status</｜DSML｜parameter>\n'
    '<｜DSML｜parameter name="timeout" string="false">30000</｜DSML｜parameter>\n'
    "</｜DSML｜invoke>\n</｜DSML｜tool_calls>"
)


def test_normalize_degraded_tokens():
    assert "<｜DSML｜tool_calls>" in normalize_dsml_markup(DEGRADED_SAMPLE)
    assert '<｜DSML｜invoke name="doc_knowlegebase">' in normalize_dsml_markup(DEGRADED_SAMPLE)
    assert "</｜DSML｜tool_calls>" in normalize_dsml_markup(DEGRADED_SAMPLE)
    # Canonical text must be untouched
    assert normalize_dsml_markup(CANONICAL_SAMPLE) == CANONICAL_SAMPLE


def test_parse_degraded_sample():
    remaining, tools = parse_dsml_tool_calls(DEGRADED_SAMPLE)
    assert len(tools) == 1
    tool = tools[0]
    assert tool["type"] == "tool_use"
    assert tool["name"] == "doc_knowlegebase"
    assert tool["input"] == {"query": "bond detail fields display"}
    assert remaining == "I will search several relevant directions first."


def test_parse_canonical_sample_multi_param():
    remaining, tools = parse_dsml_tool_calls(CANONICAL_SAMPLE)
    assert len(tools) == 1
    assert tools[0]["name"] == "Bash"
    # string="true" -> raw text; string="false" -> JSON-typed value
    assert tools[0]["input"] == {"command": "git status", "timeout": 30000}
    assert remaining == "I will run the command."


def test_parse_multiple_invokes():
    text = (
        "<｜DSML｜tool_calls>"
        '<｜DSML｜invoke name="A"><｜DSML｜parameter name="x" string="true">1</｜DSML｜parameter></｜DSML｜invoke>'
        '<｜DSML｜invoke name="B"><｜DSML｜parameter name="y" string="true">2</｜DSML｜parameter></｜DSML｜invoke>'
        "</｜DSML｜tool_calls>"
    )
    _, tools = parse_dsml_tool_calls(text)
    assert [t["name"] for t in tools] == ["A", "B"]
    assert tools[0]["input"] == {"x": "1"}
    assert tools[1]["input"] == {"y": "2"}


def test_no_dsml_passthrough():
    text = "Just a normal reply with <b>html</b> in it."
    remaining, tools = parse_dsml_tool_calls(text)
    assert tools == []
    assert remaining == text


def test_is_dsml_model():
    assert is_dsml_model("deepseek-ai/deepseek-v4-pro-0813")
    assert is_dsml_model("deepseek-ai/deepseek-v4-flash-0731")
    assert not is_dsml_model("meta/llama-3.1-70b-instruct")
    assert not is_dsml_model(None)


def test_streaming_parser_chunked_degraded_sample():
    parser = DsmlParser()
    remaining = ""
    tools = []
    # Feed in small chunks to force tag splits across chunk boundaries
    for i in range(0, len(DEGRADED_SAMPLE), 7):
        text, new_tools = parser.feed(DEGRADED_SAMPLE[i : i + 7])
        remaining += text
        tools.extend(new_tools)
    tools.extend(parser.flush())

    assert len(tools) == 1
    assert tools[0]["name"] == "doc_knowlegebase"
    assert tools[0]["input"] == {"query": "bond detail fields display"}
    assert "<|DBML" not in remaining  # no leaked markup
    assert "DSML" not in remaining
