#!/usr/bin/env python3
"""
Claude Code Proxy Server Launcher

Cross-platform server launcher that reads configuration from .env file
and displays connection instructions for Linux, Windows CMD, and Windows PowerShell.
"""

import os
import random
import string
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv


def generate_session_api_key() -> str:
    """Generate a random 32-char API key in format: 16chars.16chars"""
    chars = string.ascii_letters + string.digits
    first_half = "".join(random.choices(chars, k=16))
    second_half = "".join(random.choices(chars, k=16))
    return f"{first_half}.{second_half}"


def get_env_path() -> Path:
    """Get the path to .env file."""
    script_dir = Path(__file__).parent.resolve()
    return script_dir / ".env"


def load_env_config() -> dict:
    """Load configuration from .env file."""
    env_path = get_env_path()

    if not env_path.exists():
        print(f"Error: .env file not found at {env_path}")
        print("Please copy .env.example to .env and configure it:")
        print("  cp .env.example .env")
        sys.exit(1)

    # Load .env file
    load_dotenv(env_path)

    # Check if PROXY_API_KEY needs to be auto-generated
    proxy_api_key = os.getenv("PROXY_API_KEY", "")
    key_was_generated = False

    if not proxy_api_key or proxy_api_key == "<replaceme>":
        proxy_api_key = generate_session_api_key()
        os.environ["PROXY_API_KEY"] = proxy_api_key  # Set for downstream code
        key_was_generated = True

    # Extract relevant settings
    config = {
        "host": os.getenv("HOST", "0.0.0.0"),
        "port": os.getenv("PORT", "8082"),
        "proxy_api_key": proxy_api_key,
        "key_was_generated": key_was_generated,
        "model": os.getenv("MODEL", "z-ai/glm5"),
        "nvidia_nim_api_key": os.getenv("NVIDIA_NIM_API_KEY", ""),
    }

    return config


def check_prerequisites() -> bool:
    """Check if required prerequisites are installed."""
    # Check for venv
    venv_path = Path(__file__).parent / "venv"
    if venv_path.exists():
        print("✓ Found: venv directory")
        return True

    print("Error: 'venv' directory not found.")
    print("Please create a venv: python -m venv venv")
    return False


def print_connection_instructions(config: dict) -> None:
    """Print connection instructions for all platforms."""
    host = config["host"]
    port = config["port"]
    api_key = config["proxy_api_key"]
    base_url = f"http://{host}:{port}"

    print()
    print("=" * 60)
    print("TO CONNECT CLAUDE CODE TO THIS SERVER:")
    print("=" * 60)
    print()

    # Show session API key if auto-generated
    if config.get("key_was_generated"):
        print("SESSION API KEY (auto-generated for this session):")
        print("-" * 40)
        print(f"  {api_key}")
        print()
        print("Note: Generate a new key by setting PROXY_API_KEY in .env")
        print()

    # Linux / macOS (bash/zsh)
    print("Linux/macOS (bash/zsh):")
    print("-" * 40)
    print(f'ANTHROPIC_AUTH_TOKEN="{api_key}" ANTHROPIC_BASE_URL="{base_url}" claude')
    print()
    print("With skip permissions:")
    print(
        f'ANTHROPIC_AUTH_TOKEN="{api_key}" ANTHROPIC_BASE_URL="{base_url}" claude --dangerously-skip-permissions'
    )
    print()

    # Windows CMD
    print("Windows CMD:")
    print("-" * 40)
    print(
        f"set ANTHROPIC_AUTH_TOKEN={api_key} && set ANTHROPIC_BASE_URL={base_url} && claude"
    )
    print()
    print("With skip permissions:")
    print(
        f"set ANTHROPIC_AUTH_TOKEN={api_key} && set ANTHROPIC_BASE_URL={base_url} && claude --dangerously-skip-permissions"
    )
    print()

    # Windows PowerShell
    print("Windows PowerShell:")
    print("-" * 40)
    print(
        f'$env:ANTHROPIC_AUTH_TOKEN="{api_key}"; $env:ANTHROPIC_BASE_URL="{base_url}"; claude'
    )
    print()
    print("With skip permissions:")
    print(
        f'$env:ANTHROPIC_AUTH_TOKEN="{api_key}"; $env:ANTHROPIC_BASE_URL="{base_url}"; claude --dangerously-skip-permissions'
    )
    print()

    print("=" * 60)
    print()


def start_server(config: dict) -> None:
    """Start the uvicorn server."""
    host = config["host"]
    port = config["port"]
    script_dir = Path(__file__).parent.resolve()

    # Use venv python
    venv_python = script_dir / "venv" / "Scripts" / "python.exe"
    if not venv_python.exists():
        venv_python = script_dir / "venv" / "bin" / "python"

    if not venv_python.exists():
        print("Error: No venv found. Please run: python -m venv venv")
        sys.exit(1)

    cmd = [
        str(venv_python),
        "-m",
        "uvicorn",
        "server:app",
        "--host",
        host,
        "--port",
        port,
        "--timeout-graceful-shutdown",
        "5",
    ]
    print(f"Starting server with venv: {' '.join(cmd)}")

    print()
    try:
        subprocess.run(cmd, cwd=script_dir)
    except KeyboardInterrupt:
        print("\nServer stopped.")


CLAUDE_TIERS_3 = [
    ("Default / Sonnet 4.6", "1M context"),
    ("Opus 4.7", "1M context"),
    ("Haiku 4.5", "200k context"),
]


def print_model_mapping(model_raw: str) -> None:
    """Display which Claude tier maps to which NIM model."""
    models = [m.strip() for m in model_raw.split(",") if m.strip()]
    count = len(models)

    print()
    print(f"Claude Model Mapping ({count} model{'s' if count != 1 else ''}):")
    print("-" * 60)

    if count == 0:
        print("  No models configured.")
        print("  Use --model or set MODEL environment variable.")
    elif count == 1:
        print("  All Claude tiers (Default, Sonnet, Opus, Haiku)")
        print(f"    → {models[0]}")
    elif count == 2:
        print("  Default / Sonnet 4.6 + Opus 4.7 (1M context)")
        print(f"    → {models[0]}")
        print("  Haiku 4.5 (200k context)")
        print(f"    → {models[1]}")
    else:
        for i, tier_info in enumerate(CLAUDE_TIERS_3):
            name, ctx = tier_info
            model = models[i] if i < count else models[-1]
            print(f"  {name} ({ctx})")
            print(f"    → {model}")


def run_mcp_server():
    """Run the MCP server with stdio transport."""
    from mcp_server import mcp

    print("Starting NIMbus MCP Server (stdio transport)...", file=sys.stderr)
    mcp.run(transport="stdio")


def run_swapper_mode():
    """Enable model swapper mode by setting env var."""
    os.environ["SWAPPER_ENABLED"] = "true"
    print(
        "Model Swapper enabled - use <modelswap:model-name> in chat to switch models",
        file=sys.stderr,
    )


def main():
    """Main entry point."""
    # Load configuration FIRST so environment variables are available for MCP server
    config = load_env_config()

    # Check for --mcp / --mcpwebsearch flag
    if "--mcpwebsearch" in sys.argv or "--mcp" in sys.argv:
        run_mcp_server()
        return

    # Check for --swapper flag
    if "--swapper" in sys.argv:
        run_swapper_mode()

    print()
    print("=" * 60)
    print("Claude Code Proxy Server Launcher")
    print("=" * 60)

    print(f"Host: {config['host']}")
    print(f"Port: {config['port']}")
    print(f"Model: {config['model']}")
    print_model_mapping(config["model"])
    print(
        f"API Key: {'(auto-generated)' if config.get('key_was_generated') else '(set)'}"
    )
    print(
        f"NVIDIA Key: {'(set)' if config['nvidia_nim_api_key'] else '(NOT SET - REQUIRED!)'}"
    )

    if (
        not config["nvidia_nim_api_key"]
        or config["nvidia_nim_api_key"] == "<replaceme>"
    ):
        print()
        print("Warning: NVIDIA_NIM_API_KEY is not configured!")
        print("Please set it in your .env file.")
        print("Get your API key at: https://build.nvidia.com/settings/api-keys")

    # Check prerequisites
    if not check_prerequisites():
        sys.exit(1)

    # Print connection instructions
    print_connection_instructions(config)

    # Start the server
    start_server(config)


if __name__ == "__main__":
    main()
