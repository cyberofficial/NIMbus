"""Interactive setup wizard for NIMbus standalone executable.

Run with: nimbus.exe --init
Linux:    nimbus --init linux
Restore:  nimbus.exe --init restore | nimbus --init linux restore
"""

import contextlib
import json
import os
import random
import shutil
import string
import sys
from datetime import datetime
from pathlib import Path

import httpx

_NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"

_MODELS_CACHE: list[str] | None = None

_PRESET_MODELS = [
    "deepseek-ai/deepseek-v4-flash",
    "deepseek-ai/deepseek-v4-pro",
    "qwen/qwen3-coder-480b-a35b-instruct",
    "minimaxai/minimax-m2.7",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "moonshotai/kimi-k2.6",
    "Enter custom model",
]

_SETTINGS_ENV_KEYS = [
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
]


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------


def _prompt(msg: str, default: str = "", *, password: bool = False) -> str:
    """Prompt for text input, with optional default and masking."""
    prompt_str = f"{msg} [{default}]: " if default else f"{msg}: "
    while True:
        value = _masked_input(prompt_str) if password else input(prompt_str).strip()
        if value:
            return value
        if default:
            return default


def _masked_input(prompt: str) -> str:
    """Read a line from stdin, printing * for each character.
    Cross-platform: uses msvcrt on Windows, termios/tty on Unix.
    """
    import sys

    try:
        import msvcrt

        sys.stdout.write(prompt)
        sys.stdout.flush()
        chars: list[str] = []
        while True:
            ch = msvcrt.getch()
            if ch in (b"\r", b"\n"):
                sys.stdout.write("\n")
                break
            if ch == b"\x08":  # Backspace
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
            elif ch == b"\x03":  # Ctrl+C
                raise KeyboardInterrupt
            else:
                try:
                    decoded = ch.decode("utf-8")
                    chars.append(decoded)
                    sys.stdout.write("*")
                except UnicodeDecodeError:
                    pass
            sys.stdout.flush()
        return "".join(chars)
    except ImportError:
        return _unix_masked_input(prompt)


def _unix_masked_input(prompt: str) -> str:
    """Unix masked input using termios/tty (fallback when msvcrt is unavailable).

    Falls back to getpass.getpass() on non-TTY stdin or when termios fails.
    """
    import termios
    import tty

    if not sys.stdin.isatty():
        import getpass

        return getpass.getpass(prompt)

    sys.stdout.write(prompt)
    sys.stdout.flush()
    fd = sys.stdin.fileno()
    try:
        old = termios.tcgetattr(fd)
    except termios.error:
        import getpass

        return getpass.getpass(prompt)
    chars: list[str] = []
    try:
        tty.setraw(fd)
        while True:
            ch = sys.stdin.read(1)
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                break
            if ch == "\x7f":  # DEL (Unix backspace)
                if chars:
                    chars.pop()
                    sys.stdout.write("\b \b")
            elif ch == "\x03":  # Ctrl+C
                raise KeyboardInterrupt
            else:
                chars.append(ch)
                sys.stdout.write("*")
            sys.stdout.flush()
    finally:
        with contextlib.suppress(termios.error):
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    return "".join(chars)


def _prompt_yes_no(msg: str, default: bool = True) -> bool:
    """Prompt for yes/no, return bool."""
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        resp = input(f"{msg}{suffix}: ").strip().lower()
        if not resp:
            return default
        if resp in ("y", "yes"):
            return True
        if resp in ("n", "no"):
            return False
        print("  Please enter 'y' or 'n'.")


def _prompt_choices(msg: str, options: list[str], default: int = 0) -> int:
    """Prompt for numbered choice. Returns selected index."""
    print(msg)
    for i, opt in enumerate(options, 1):
        marker = " [default]" if i - 1 == default else ""
        print(f"  {i}) {opt}{marker}")
    while True:
        resp = input(f"Choice [{default + 1}]: ").strip()
        if not resp:
            return default
        try:
            idx = int(resp) - 1
            if 0 <= idx < len(options):
                return idx
        except ValueError:
            pass
        print(f"  Enter a number 1-{len(options)}.")


def _select_update_sections() -> list[str]:
    """Show menu and return list of selected sections to update."""
    print()
    print("Existing .env detected. What would you like to update?")
    print("-" * 50)
    sections = [
        ("nvidia_key", "NVIDIA API Key"),
        ("proxy_key", "Proxy API Key"),
        ("models", "Models (Sonnet/Opus/Haiku)"),
        ("port_mode", "Server Port & Mode"),
        ("optimizations", "Optimization Settings"),
        ("mcp", "MCP Server Settings"),
        ("discord", "Discord Bot Settings"),
        ("all", "All (run full wizard)"),
    ]

    for i, (key, desc) in enumerate(sections, 1):
        print(f"  {i}. {desc}")

    print()
    choice = _prompt(
        "Enter section numbers (comma-separated, e.g. '1,3') or 'all'",
        default="all"
    )

    if choice.lower() == "all":
        return [s[0] for s in sections[:-1]]  # All except 'all'

    selected = []
    for part in choice.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(sections) - 1:
                selected.append(sections[idx][0])

    return selected if selected else [s[0] for s in sections[:-1]]


# ---------------------------------------------------------------------------
# API testing
# ---------------------------------------------------------------------------


def _test_model(api_key: str, model: str) -> tuple[bool, str]:
    """Test a model with a minimal chat completion. Returns (ok, message)."""
    try:
        resp = httpx.post(
            f"{_NVIDIA_BASE}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": ""}],
                "max_tokens": 1,
            },
            timeout=60,
        )
        if resp.status_code == 200:
            return True, f"{model} responded OK"
        try:
            detail = resp.json()
            msg = detail.get("error", {}).get("message", resp.text)
        except Exception:
            msg = resp.text[:200]
        return False, f"HTTP {resp.status_code}: {msg}"
    except httpx.TimeoutException:
        return False, f"{model} timed out after 60s"
    except httpx.RequestError as e:
        return False, f"Connection error: {e}"


def _find_working_model(api_key: str) -> tuple[bool, str, str]:
    """Fetch available models from NVIDIA and test random ones until one works."""
    global _MODELS_CACHE
    print("  Fetching available models from NVIDIA...")
    try:
        if _MODELS_CACHE is None:
            resp = httpx.get(f"{_NVIDIA_BASE}/models", timeout=30)
            if resp.status_code == 200:
                data = resp.json()
                all_models = [
                    m["id"] for m in data.get("data", []) if "/" in m.get("id", "")
                ]
                # Filter to chat completion models (skip embeddings, vision-only, safety)
                _MODELS_CACHE = [
                    m
                    for m in all_models
                    if not any(
                        skip in m.lower()
                        for skip in [
                            "embed",
                            "safety",
                            "guard",
                            "fuyu",
                            "neva",
                            "vila",
                            "deplot",
                            "kosmos",
                            "nv-embed",
                            "nv-clip",
                            "nemoretriever",
                        ]
                    )
                ]
        models = _MODELS_CACHE[:] if _MODELS_CACHE else []
    except Exception:
        models = []

    if not models:
        print("  Could not fetch model list, using built-in defaults.")
        models = [
            "deepseek-ai/deepseek-v4-flash",
            "deepseek-ai/deepseek-v4-pro",
            "qwen/qwen3-coder-480b-a35b-instruct",
            "meta/llama-4-maverick-17b-128e-instruct",
        ]

    random.shuffle(models)
    print(f"  Got {len(models)} models, testing random ones...")

    tested = 0
    for model in models:
        if tested >= 6:
            break
        print(f"    Trying {model}...", end=" ", flush=True)
        ok, msg = _test_model(api_key, model)
        print("OK" if ok else "FAILED")
        tested += 1
        if ok:
            return True, model, msg

    return (
        False,
        "",
        f"Tried {tested} random models, none responded - key may be invalid.",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _generate_api_key() -> str:
    """Generate random 32-char key in 16chars.16chars format."""
    chars = string.ascii_letters + string.digits
    return (
        "".join(random.choices(chars, k=16))
        + "."
        + "".join(random.choices(chars, k=16))
    )


def _get_short_name(full: str) -> str:
    """Strip org prefix and [1m] suffix from a model name.
    'deepseek-ai/deepseek-v4-flash[1m]' -> 'deepseek-v4-flash'
    """
    name = full
    if "/" in name:
        name = name.split("/", 1)[1]
    if name.endswith("[1m]"):
        name = name[:-4]
    return name


def _backup_settings(path: Path) -> Path | None:
    """Backup settings.json with timestamp. Returns backup path or None."""
    if not path.exists():
        return None
    ts = datetime.now().strftime("%Y%m%d%H%M%S")
    backup = path.with_name(f"settings.json.nimbus-backup-{ts}")
    shutil.copy2(str(path), str(backup))
    return backup


def _restore_settings(settings_path: Path | None = None) -> None:
    """Restore most recent backup of settings.json.

    Args:
        settings_path: If provided, searches for backups in the same directory.
                       If None, uses Path.home() / ".claude" (Windows default).
    """
    if settings_path is not None:
        search_dir = settings_path.parent
        target = settings_path
    else:
        search_dir = Path.home() / ".claude"
        target = search_dir / "settings.json"
    backups = sorted(search_dir.glob("settings.json.nimbus-backup-*"))
    if not backups:
        print("No backup found to restore.")
        return
    latest = backups[-1]
    shutil.copy2(str(latest), str(target))
    print(f"Restored settings.json from: {latest.name}")


def _write_dotenv(path: Path, params: dict, is_linux: bool = False) -> None:
    """Write .env with all collected values.

    Args:
        path: Path to the .env file.
        params: Configuration parameters.
        is_linux: If True, writes actual model names instead of windows:settings.json sentinel.
    """
    # Always use windows:settings.json sentinel - reads models from Claude Code settings.json
    model_line = "MODEL=windows:settings.json"

    prompt_raw = params.get("discord_system_prompt", "")
    prompt_escaped = prompt_raw.replace("\\", "\\\\").replace('"', '\\"')

    lines = [
        f'NVIDIA_NIM_API_KEY="{params["nvidia_key"]}"',
        f"PORT={params['port']}",
        model_line,
        f'PROXY_API_KEY="{params["proxy_key"]}"',
        f"NIM_THINKING={'true' if params['thinking'] else 'false'}",
        f"SERVER_TYPE={params.get('server_type', 'stream')}",
        f"PROVIDER_MAX_WAIT_TIME={params.get('provider_max_wait', 30)}",
        f"PROVIDER_RETRY_ON_TRUNCATION={params.get('provider_retry_on_truncation', 3)}",
        f"PROVIDER_RETRY_DELAY={params.get('provider_retry_delay', 1.0)}",
        f"ENABLE_RECAP_SKIP={params.get('enable_recap_skip', 'true')}",
        f"ENABLE_NETWORK_PROBE_MOCK={params.get('enable_network_probe_mock', 'true')}",
        f"ENABLE_TITLE_GENERATION_SKIP={params.get('enable_title_generation_skip', 'true')}",
        f"ENABLE_SUGGESTION_MODE_SKIP={params.get('enable_suggestion_mode_skip', 'true')}",
        f"ENABLE_FILEPATH_EXTRACTION_MOCK={params.get('enable_filepath_extraction_mock', 'true')}",
        # MCP Server settings
        f"WEB_SEARCH_FETCH_TIMEOUT={params.get('mcp_fetch_timeout', 10.0)}",
        # Discord Bot settings
        f"DISCORD_ENABLED={str(params.get('configure_discord', False)).lower()}",
        f'DISCORD_BOT_TOKEN="{params.get("discord_token", "")}"',
        f"DISCORD_GUILD_ID={params.get('discord_guild_id', 0)}",
        f"DISCORD_CONTROL_CHANNEL_ID={params.get('discord_control_channel_id', 0)}",
        f"DISCORD_CONVERSATION_CATEGORY_ID={params.get('discord_conversation_category_id', 0)}",
        f'DISCORD_CONVERSATION_CHANNEL_ID="{params.get("discord_conversation_channel_id", "")}"',
        f"DISCORD_OWNER_ID={params.get('discord_owner_id', 0)}",
        f"DISCORD_OWNER_ONLY={str(params.get('discord_owner_only', True)).lower()}",
        f"DISCORD_MAX_TOKENS={params.get('discord_max_tokens', 202000)}",
        f"DISCORD_COMPACT_THRESHOLD={params.get('discord_compact_threshold', 0.8)}",
        f"DISCORD_USER_COOLDOWN={params.get('discord_user_cooldown', 10)}",
        f"DISCORD_SERVER_LIMIT={params.get('discord_server_limit', 20)}",
        f"DISCORD_SERVER_WINDOW={params.get('discord_server_window', 60)}",
        f'DISCORD_SYSTEM_PROMPT="{prompt_escaped}"',
        f"DISCORD_SKIP_FILES={str(params.get('discord_skip_files', True)).lower()}",
        f"DISCORD_SPLIT_THRESHOLD={params.get('discord_split_threshold', 1900)}",
        f'DISCORD_MODEL="{params.get("discord_model", "")}"',
        f"DISCORD_AUTO_COMPACT={str(params.get('discord_auto_compact', True)).lower()}",
        f"DISCORD_CMD_ASK={str(params.get('discord_cmd_ask', True)).lower()}",
        f"DISCORD_CMD_COMPACT={str(params.get('discord_cmd_compact', True)).lower()}",
        f"DISCORD_CMD_NEW={str(params.get('discord_cmd_new', True)).lower()}",
        f"DISCORD_CMD_STATUS={str(params.get('discord_cmd_status', True)).lower()}",
        f"DISCORD_CMD_DOWNLOAD={str(params.get('discord_cmd_download', True)).lower()}",
        f"DISCORD_CMD_BLOCK={str(params.get('discord_cmd_block', True)).lower()}",
        f"DISCORD_CMD_UNBLOCK={str(params.get('discord_cmd_unblock', True)).lower()}",
        f"DISCORD_CMD_BLOCKED={str(params.get('discord_cmd_blocked', True)).lower()}",
        f"DISCORD_CMD_NEWCHANNEL={str(params.get('discord_cmd_newchannel', True)).lower()}",
        f"DISCORD_COMMAND_PREFIX={params.get('discord_command_prefix', '!!')}",
        f"DISCORD_REQUIRE_MENTION={str(params.get('discord_require_mention', True)).lower()}",
        f"DISCORD_CMD_PREFIX_ASK={str(params.get('discord_cmd_prefix_ask', True)).lower()}",
        f"DISCORD_CMD_PREFIX_COMPACT={str(params.get('discord_cmd_prefix_compact', True)).lower()}",
        f"DISCORD_CMD_PREFIX_NEW={str(params.get('discord_cmd_prefix_new', True)).lower()}",
        f"DISCORD_CMD_PREFIX_STATUS={str(params.get('discord_cmd_prefix_status', True)).lower()}",
    ]
    content = (
        "# NIMbus configuration -- generated by --init\n" + "\n".join(lines) + "\n"
    )
    path.write_text(content, encoding="utf-8")
    print(f"  .env -> {path}")


def _load_existing_env(exe_dir: Path) -> dict:
    """Load and parse existing .env file into a dict."""
    env_path = exe_dir / ".env"
    if not env_path.exists():
        return {}

    config = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            # Strip quotes
            value = value.strip().strip('"').strip("'")
            config[key] = value

    return config


def _run_section(
    section: str,
    existing: dict,
    nvidia_key: str,
    is_linux: bool,
    exe_dir: Path,
    tested_models: set[str] | None = None,
    extra_models: list[str] | None = None,
) -> dict:
    """Run a specific wizard section and return updated params."""
    if tested_models is None:
        tested_models = set()
    if extra_models is None:
        extra_models = []

    updates = {}

    if section == "nvidia_key":
        # Step 1: NVIDIA API Key
        print()
        print("Step 1: NVIDIA NIM API Key")
        print("-" * 40)
        new_key = _prompt("Enter your NVIDIA NIM API key", password=True)
        while not new_key or new_key == "<replaceme>":
            print("  API key cannot be empty.")
            new_key = _prompt("Enter your NVIDIA NIM API key", password=True)

        print("  Testing API key...")
        found, working_model, msg = _find_working_model(new_key)
        if not found:
            print(f"  {msg}")
            print("  No models responded - check your key and try again later.")
            return {}

        print(f"  Key is valid! (tested with {working_model})")
        updates["nvidia_key"] = new_key
        # Re-detect thinking based on the model from test
        updates["thinking"] = "deepseek" in working_model.lower()

    elif section == "proxy_key":
        # Step 2: Proxy API Key
        print()
        print("Step 2: Proxy API Key")
        print("-" * 40)
        print("  This key protects your proxy. Claude Code will use it to authenticate.")
        choices = ["Auto-generate a random key (recommended)", "Enter my own key"]
        idx = _prompt_choices("  Choose an option:", choices, default=0)
        if idx == 0:
            proxy_key = _generate_api_key()
            masked = proxy_key[:8] + "..." + proxy_key[-4:]
            print(f"  Generated: {masked}")
        else:
            proxy_key = _prompt("Enter your proxy API key")
            while not proxy_key:
                print("  Proxy API key cannot be empty.")
                proxy_key = _prompt("Enter your proxy API key")
        updates["proxy_key"] = proxy_key

    elif section == "models":
        # Step 3: Models
        print()
        print("Step 3: Default Model")
        print("-" * 40)
        # Reuse existing tested models set
        model_sonnet, model_sonnet_full = _pick_and_test_model(
            nvidia_key, tested_models, extra_models
        )

        if _prompt_yes_no(
            "  Use the same model for all Claude tiers (Sonnet/Opus/Haiku)?",
            default=True,
        ):
            model_opus = model_sonnet
            model_haiku = model_sonnet
            model_opus_full = model_sonnet_full
            model_haiku_full = model_sonnet_full
        else:
            print()
            print("  Model for Sonnet (Default tier, 1M context):")
            model_sonnet, model_sonnet_full = _pick_and_test_model(
                nvidia_key, tested_models, extra_models
            )
            print()
            print("  Model for Opus tier (1M context):")
            model_opus, model_opus_full = _pick_and_test_model(
                nvidia_key, tested_models, extra_models
            )
            print()
            print("  Model for Haiku tier (200k context):")
            model_haiku, model_haiku_full = _pick_and_test_model(
                nvidia_key, tested_models, extra_models
            )

        _save_custom_models(exe_dir, extra_models)
        updates.update({
            "model_sonnet": model_sonnet,
            "model_opus": model_opus,
            "model_haiku": model_haiku,
            "model_sonnet_full": model_sonnet_full,
            "model_opus_full": model_opus_full,
            "model_haiku_full": model_haiku_full,
        })

    elif section == "port_mode":
        # Step 4: Port
        print()
        print("Step 4: Server Port")
        print("-" * 40)
        port_str = _prompt("Port for the proxy server", default=str(existing.get("PORT", 8082)))
        while True:
            try:
                port = int(port_str)
                if 1024 <= port <= 65535:
                    break
                print("  Port must be 1024-65535.")
            except ValueError:
                print("  Port must be a number.")
            port_str = _prompt("Port", default="8082")
        updates["port"] = port

        # Step 5: Server type
        print()
        print("Step 5: Server Mode")
        print("-" * 40)
        print("  stream  → Tokens arrive live as they're generated (like ChatGPT).")
        print("            May fail mid-stream if the Nvidia backend cuts out.")
        print("  buffer  → Waits for the full Nvidia response, retries on failure,")
        print("            then sends the complete result. More reliable but slower.")
        print()
        server_type_choices = [
            "stream (live, may fail mid-stream)",
            "buffer (reliable, slower)",
        ]
        st_idx = _prompt_choices(
            "  Choose server mode:", server_type_choices, default=0
        )
        server_type = "stream" if st_idx == 0 else "buffer"
        updates["server_type"] = server_type

        # Buffer-specific options
        provider_max_wait = 30
        provider_retry_on_truncation = 3
        provider_retry_delay = 1.0
        if server_type == "buffer":
            print()
            print("  Buffer Mode Settings:")
            print("  These only apply in buffer mode. Stream mode ignores them.")
            print()
            print("  Max Wait Time: How many seconds to wait for Nvidia to start")
            print("  responding before retrying. Default: 30s")
            wait_str = _prompt("  Max wait time (seconds)", default="30")
            try:
                provider_max_wait = float(wait_str)
            except ValueError:
                provider_max_wait = 30.0
            print()
            print("  Retry on Truncation: How many times to retry if the response")
            print("  is truncated/incomplete. 0 = keep retrying forever.")
            retry_str = _prompt("  Retry attempts", default="3")
            try:
                provider_retry_on_truncation = int(retry_str)
            except ValueError:
                provider_retry_on_truncation = 3
            print()
            print("  Retry Delay: Base seconds to wait between retries.")
            print("  (exponential backoff is applied on top of this)")
            delay_str = _prompt("  Retry delay (seconds)", default="1.0")
            try:
                provider_retry_delay = float(delay_str)
            except ValueError:
                provider_retry_delay = 1.0
        updates.update({
            "provider_max_wait": provider_max_wait,
            "provider_retry_on_truncation": provider_retry_on_truncation,
            "provider_retry_delay": provider_retry_delay,
        })

    elif section == "optimizations":
        # Step 6: Optimization Settings
        print()
        print("Step 6: Optimization Settings")
        print("-" * 40)
        print("  NIMbus can skip unnecessary requests from Claude Code")
        print("  to save API calls. Each option is enabled by default.")
        print()
        print("  NOTE: Suggestion mode skip and recap skip are DISABLED by default")
        print("  because their detection logic currently produces false positives.")
        print("  You can enable them here, but they may cause issues.")
        print()
        opts = {}
        opt_defs = [
            ("enable_recap_skip", "Skip recap requests when you return after stepping away (disabled by default)"),
            ("enable_network_probe_mock", "Mock quota/network probe requests"),
            ("enable_title_generation_skip", "Skip conversation title generation"),
            ("enable_suggestion_mode_skip", "Skip suggestion mode requests (disabled by default)"),
            ("enable_filepath_extraction_mock", "Mock filepath extraction (speeds up file searching)"),
        ]
        for key, desc in opt_defs:
            # Default to False for the two disabled optimizations
            default = False if key in ("enable_recap_skip", "enable_suggestion_mode_skip") else True
            yn = _prompt_yes_no(f"  {desc}?", default=default)
            opts[key] = "true" if yn else "false"
            print()
        updates.update(opts)

    elif section == "mcp":
        # Step 7: MCP Server Configuration
        print()
        print("Step 7: MCP Server Configuration")
        print("-" * 40)
        print("  NIMbus can run as an MCP (Model Context Protocol) server")
        print("  exposing web search and page fetch tools to Claude Code.")
        print()
        configure_mcp = _prompt_yes_no(
            "  Configure MCP server (web search tools)?", default=True
        )
        mcp_fetch_timeout = 10.0
        if configure_mcp:
            print()
            mcp_fetch_timeout_str = _prompt(
                "  Fetch timeout (seconds)", default="10.0"
            )
            try:
                mcp_fetch_timeout = float(mcp_fetch_timeout_str)
            except ValueError:
                mcp_fetch_timeout = 10.0
        updates.update({
            "configure_mcp": configure_mcp,
            "mcp_fetch_timeout": mcp_fetch_timeout,
        })

    elif section == "discord":
        # Step 8: Discord Bot Configuration
        print()
        print("Step 8: Discord Bot Configuration")
        print("-" * 40)
        print("  NIMbus can run as a Discord bot, allowing users to chat")
        print("  with NVIDIA NIM models directly from Discord channels.")
        print("  (No file access - pure LLM chat)")
        print()
        configure_discord = _prompt_yes_no(
            "  Enable Discord bot integration?", default=False
        )
        # Defaults (used when Discord is disabled or user skips optional prompts)
        discord_token = ""
        discord_guild_id = ""
        discord_control_channel_id = "0"
        discord_conversation_category_id = "0"
        discord_conversation_channel_id = ""
        discord_owner_id = "0"
        discord_owner_only = True
        discord_max_tokens = 202000
        discord_compact_threshold = 0.8
        discord_user_cooldown = 10
        discord_server_limit = 20
        discord_server_window = 60
        discord_system_prompt = "You are a helpful Discord bot. Be friendly, casual, and conversational. Talk like a normal person - don't use formal analysis headers, bullet points, or structured formatting unless specifically asked. Keep responses natural and direct."
        discord_skip_files = True
        discord_split_threshold = 1900
        discord_auto_compact = True
        discord_cmd_ask = True
        discord_cmd_compact = True
        discord_cmd_new = True
        discord_cmd_status = True
        discord_cmd_download = True
        discord_cmd_block = True
        discord_cmd_unblock = True
        discord_cmd_blocked = True
        discord_cmd_newchannel = True
        if configure_discord:
            print()
            print("  The bot token is the secret token from your Discord Application.")
            print("  Get it at: https://discord.com/developers/applications")
            discord_token = _prompt("  Discord bot token", password=True)
            while not discord_token:
                print("  Bot token cannot be empty.")
                discord_token = _prompt("  Discord bot token", password=True)
            print()
            print("  The guild/server ID locks the bot to specific servers.")
            print("  You can enter multiple IDs separated by commas.")
            print("  Right-click a server -> Copy Server ID to get it.")
            print("  (Requires Developer Mode in Discord settings.)")
            discord_guild_id = _prompt(
                "  Guild/Server IDs (comma-separated for multiple)",
                default=str(existing.get("DISCORD_GUILD_ID", "0")),
            )
            print()
            print("  Control panel channels are where users can use /ask commands")
            print("  without creating dedicated conversation channels.")
            print("  Enter channel IDs (comma-separated for multiple).")
            print("  Right-click a channel -> Copy Channel ID to get it.")
            discord_control_channel_id = _prompt(
                "  Control Panel Channel IDs (comma-separated)",
                default=str(existing.get("DISCORD_CONTROL_CHANNEL_ID", "0")),
            )
            print()
            print("  When a user sends /newchannel, a new conversation channel")
            print("  is created under this category. Leave as 0 to use the")
            print("  control channel only (no dedicated channels).")
            discord_conversation_category_id = _prompt(
                "  Conversation Category ID (bot creates channels here)",
                default=str(existing.get("DISCORD_CONVERSATION_CATEGORY_ID", "0")),
            )
            print()
            print("  Specific channels where the bot responds (alternative to")
            print("  category-based channels). The bot will listen in these")
            print("  channels OR channels under the category above.")
            print("  Comma-separate multiple channel IDs. Leave blank to")
            print("  rely on categories/control channels only.")
            discord_conversation_channel_id = _prompt(
                "  Specific Conversation Channel IDs (comma-separated, or blank)",
                default=str(existing.get("DISCORD_CONVERSATION_CHANNEL_ID", "")),
            )
            print()
            print("  Your Discord user ID grants you owner privileges such as")
            print("  blocking users, unblocking users, and managing the bot.")
            print("  Right-click yourself -> Copy User ID to get it.")
            discord_owner_id = _prompt(
                "  Your Discord User ID (for owner privileges)",
                default=str(existing.get("DISCORD_OWNER_ID", "0")),
            )
            print()
            print("  When enabled, only the owner can use the bot.")
            print("  When disabled, anyone in the server can chat with it.")
            discord_owner_only = _prompt_yes_no(
                "  Restrict bot to owner only?", default=True
            )
            print()
            print("  Token Management:")
            print("  These settings control when conversations are compacted")
            print("  (summarized) to free up token space.")
            print("  Max tokens per conversation before compaction triggers.")
            print("  Must match or be less than your NIM model's output limit.")
            print("  Default: 202000 (matches NIM_MAX_TOKENS).")
            discord_max_tokens_str = _prompt(
                "  Max tokens per conversation",
                default=str(existing.get("DISCORD_MAX_TOKENS", "202000")),
            )
            try:
                discord_max_tokens = int(discord_max_tokens_str)
            except ValueError:
                discord_max_tokens = 202000
            print()
            print("  At what fraction of max_tokens should compaction trigger?")
            print("  E.g., 0.8 = compact when conversation reaches 80% of max_tokens.")
            print("  Range: 0.0 to 1.0. Higher = compacts later.")
            discord_compact_threshold_str = _prompt(
                "  Compact threshold (0.0-1.0)",
                default=str(existing.get("DISCORD_COMPACT_THRESHOLD", "0.8")),
            )
            try:
                discord_compact_threshold = float(discord_compact_threshold_str)
            except ValueError:
                discord_compact_threshold = 0.8
            print()
            print("  When auto-compact is enabled, the bot automatically summarizes")
            print("  conversations when the token threshold is reached.")
            print("  When disabled, users must manually use /compact.")
            discord_auto_compact = _prompt_yes_no(
                "  Auto-compact when threshold reached?", default=True
            )
            print()
            print("  Rate Limiting:")
            print("  These settings prevent spam and abuse.")
            print("  User cooldown: Minimum seconds a user must wait between")
            print("  sending messages to the bot. Default: 10 seconds.")
            discord_user_cooldown_str = _prompt(
                "  User cooldown (seconds between messages)",
                default=str(existing.get("DISCORD_USER_COOLDOWN", "10")),
            )
            try:
                discord_user_cooldown = int(discord_user_cooldown_str)
            except ValueError:
                discord_user_cooldown = 10
            print()
            print("  Server rate limit: Maximum number of requests the entire")
            print("  server can make within the rate window. Default: 20.")
            discord_server_limit_str = _prompt(
                "  Server rate limit (max requests per window)",
                default=str(existing.get("DISCORD_SERVER_LIMIT", "20")),
            )
            try:
                discord_server_limit = int(discord_server_limit_str)
            except ValueError:
                discord_server_limit = 20
            print()
            print("  Server rate window: The time window in seconds for the")
            print("  server rate limit. Default: 60 seconds.")
            print("  E.g., 20 requests per 60 seconds = 20 req/min per server.")
            discord_server_window_str = _prompt(
                "  Server rate window (seconds)",
                default=str(existing.get("DISCORD_SERVER_WINDOW", "60")),
            )
            try:
                discord_server_window = int(discord_server_window_str)
            except ValueError:
                discord_server_window = 60
            print()
            print("  Message Handling:")
            print("  When enabled, the bot ignores messages that contain file")
            print("  attachments (images, documents, etc.).")
            discord_skip_files = _prompt_yes_no(
                "  Skip messages with file attachments?", default=True
            )
            print()
            print("  Discord has a 2000 character limit per message. The bot")
            print("  will split long LLM responses into multiple messages.")
            print("  Default: 1900 chars (leaves room for formatting).")
            discord_split_threshold_str = _prompt(
                "  Message split threshold (max chars before splitting)",
                default=str(existing.get("DISCORD_SPLIT_THRESHOLD", "1900")),
            )
            try:
                discord_split_threshold = int(discord_split_threshold_str)
            except ValueError:
                discord_split_threshold = 1900
            print()
            print("  Discord Model (optional, separate from main MODEL):")
            print("  Use a different model for Discord bot vs the main API proxy.")
            print("  Leave empty to use the MODEL setting (or windows:settings.json picks Opus > Sonnet > Haiku).")
            discord_model = _prompt(
                "  Discord model (owner/model-name, or empty to use MODEL)",
                default="",
            )
            print()
            print("  Command Toggles:")
            print("  You can disable any of the following slash commands.")
            print("  All commands default to enabled (true).")
            print()
            print("  /ask - Ask the AI a question with conversation history")
            discord_cmd_ask = _prompt_yes_no("  Enable /ask command?", default=True)
            print()
            print("  /compact - Summarize the conversation and start fresh")
            discord_cmd_compact = _prompt_yes_no("  Enable /compact command?", default=True)
            print()
            print("  /new - Clear conversation without saving a summary")
            discord_cmd_new = _prompt_yes_no("  Enable /new command?", default=True)
            print()
            print("  /status - Show bot status, rate limits, and stats")
            discord_cmd_status = _prompt_yes_no("  Enable /status command?", default=True)
            print()
            print("  /download - Download conversation history as markdown")
            discord_cmd_download = _prompt_yes_no("  Enable /download command?", default=True)
            print()
            print("  /block - Block a user from using the bot (owner only)")
            discord_cmd_block = _prompt_yes_no("  Enable /block command?", default=True)
            print()
            print("  /unblock - Unblock a previously blocked user (owner only)")
            discord_cmd_unblock = _prompt_yes_no("  Enable /unblock command?", default=True)
            print()
            print("  /blocked - List all blocked users (owner only)")
            discord_cmd_blocked = _prompt_yes_no("  Enable /blocked command?", default=True)
            print()
            print("  /newchannel - Create a new dedicated conversation channel")
            discord_cmd_newchannel = _prompt_yes_no("  Enable /newchannel command?", default=True)
            print()
            print("  Live Conversation Behavior:")
            print("  ----")
            print()
            print("  The bot can respond in two modes:")
            print("  - Mention mode (@bot required): Bot only replies when @mentioned")
            print("  - Prefix mode (!! commands): Bot also responds to !!ask, !!compact, etc.")
            print()
            discord_require_mention = _prompt_yes_no(
                "  Require @mention to trigger responses?",
                default=existing.get("DISCORD_REQUIRE_MENTION", "true").lower() == "true",
            )
            print()
            print("  Command Prefix:")
            print("  When using prefix commands, the bot listens for messages")
            print("  starting with this string. You can still also @mention the bot.")
            print("  Default is '!!'. Set to empty string to disable prefix commands.")
            discord_command_prefix = _prompt(
                "  Command prefix (e.g. '!!', '?', '!')",
                default=existing.get("DISCORD_COMMAND_PREFIX", "!!"),
            )
            print()
            print("  Discord Model (optional, separate from main MODEL):")
            print("  Use a different model for Discord bot vs the main API proxy.")
            print("  Leave empty to use the MODEL setting (or windows:settings.json picks Opus > Sonnet > Haiku).")
            discord_model = _prompt(
                "  Discord model (owner/model-name, or empty to use MODEL)",
                default=existing.get("DISCORD_MODEL", ""),
            )
            print()
            print("  Prefix Command Toggles:")
            print("  Control which commands are available via the text prefix.")
            print("  Slash commands are controlled separately above.")
            print()
            discord_cmd_prefix_ask = _prompt_yes_no("  Enable !!ask command?", default=True)
            discord_cmd_prefix_compact = _prompt_yes_no("  Enable !!compact command?", default=True)
            discord_cmd_prefix_new = _prompt_yes_no("  Enable !!new command?", default=True)
            discord_cmd_prefix_status = _prompt_yes_no("  Enable !!status command?", default=True)
        updates.update({
            "configure_discord": configure_discord,
            "discord_token": discord_token,
            "discord_model": discord_model,
            "discord_guild_id": discord_guild_id,
            "discord_control_channel_id": discord_control_channel_id,
            "discord_conversation_category_id": discord_conversation_category_id,
            "discord_conversation_channel_id": discord_conversation_channel_id,
            "discord_owner_id": discord_owner_id,
            "discord_owner_only": discord_owner_only,
            "discord_max_tokens": discord_max_tokens,
            "discord_compact_threshold": discord_compact_threshold,
            "discord_user_cooldown": discord_user_cooldown,
            "discord_server_limit": discord_server_limit,
            "discord_server_window": discord_server_window,
            "discord_system_prompt": discord_system_prompt,
            "discord_skip_files": discord_skip_files,
            "discord_split_threshold": discord_split_threshold,
            "discord_auto_compact": discord_auto_compact,
            "discord_cmd_ask": discord_cmd_ask,
            "discord_cmd_compact": discord_cmd_compact,
            "discord_cmd_new": discord_cmd_new,
            "discord_cmd_status": discord_cmd_status,
            "discord_cmd_download": discord_cmd_download,
            "discord_cmd_block": discord_cmd_block,
            "discord_cmd_unblock": discord_cmd_unblock,
            "discord_cmd_blocked": discord_cmd_blocked,
            "discord_cmd_newchannel": discord_cmd_newchannel,
            "discord_require_mention": discord_require_mention,
            "discord_command_prefix": discord_command_prefix,
            "discord_cmd_prefix_ask": discord_cmd_prefix_ask,
            "discord_cmd_prefix_compact": discord_cmd_prefix_compact,
            "discord_cmd_prefix_new": discord_cmd_prefix_new,
            "discord_cmd_prefix_status": discord_cmd_prefix_status,
        })

    return updates


def _write_settings_json(path: Path, params: dict) -> None:
    """Merge the 7 env keys into settings.json. Leaves everything else untouched."""
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError, OSError:
            existing = {}

    current_env = existing.get("env", {})
    current_env.update(
        {
            "ANTHROPIC_BASE_URL": params["base_url"],
            "ANTHROPIC_AUTH_TOKEN": params["proxy_key"],
            "ANTHROPIC_MODEL": params["model_full"],
            "ANTHROPIC_DEFAULT_OPUS_MODEL": params["model_opus"],
            "ANTHROPIC_DEFAULT_SONNET_MODEL": params["model_sonnet"],
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": params["model_haiku"],
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    )
    existing["env"] = current_env

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"  settings.json -> {path}")


def _build_model_list_str(*full_names: str) -> str:
    """Deduplicate and comma-join full NIM model IDs for the MODEL env var."""
    seen: list[str] = []
    for name in full_names:
        if name and name not in seen:
            seen.append(name)
    return ",".join(seen) if seen else "deepseek-ai/deepseek-v4-flash"


def _print_summary(params: dict, update_mode: bool = False, updated_sections: list | None = None) -> None:
    """Print post-setup summary."""
    print()
    print("=" * 60)
    if update_mode:
        print("  Configuration Updated!")
        if updated_sections:
            print(f"  Updated: {', '.join(updated_sections)}")
    else:
        print("  Setup Complete!")
    print("=" * 60)
    print()
    print(f"  Proxy URL:    {params['base_url']}")
    print(f"  Proxy Key:    {params['proxy_key'][:20]}...")
    print(f"  Sonnet:       {params['model_sonnet']}")
    print(f"  Opus:         {params['model_opus']}")
    print(f"  Haiku:        {params['model_haiku']}")
    print(f"  Port:         {params['port']}")
    discord_status = "Enabled" if params.get("configure_discord") else "Disabled"
    print(f"  Discord Bot:  {discord_status}")

    # MCP Server info
    if params.get("configure_mcp"):
        print()
        print("  MCP Server:    Enabled")
        print(f"  MCP Fetch Timeout: {params.get('mcp_fetch_timeout', 10.0)}s")
        print()
        print("  TO ADD MCP SERVER TO CLAUDE CODE:")
        print("  " + "-" * 40)

        # Determine frozen status
        frozen = getattr(sys, "frozen", False)
        mcp_cmd = _get_mcp_add_command(
            params.get("is_linux", False), params.get("exe_dir", Path.cwd()), frozen
        )
        print(f"  {mcp_cmd}")
    print()
    print("  TO CONNECT CLAUDE CODE (Proxy Mode):")
    print("  " + "-" * 40)
    print(f"  set ANTHROPIC_AUTH_TOKEN={params['proxy_key']}")
    print(f"  set ANTHROPIC_BASE_URL={params['base_url']}")
    print("  claude")
    print()
    print("  Press Ctrl+C to stop the server.")
    print("=" * 60)
    print()


def _pick_and_test_model(
    api_key: str,
    tested_models: set[str] | None = None,
    extra_models: list[str] | None = None,
) -> tuple[str, str]:
    """Let user pick a model, test it (unless already-tested), choose context window.

    Returns:
        Tuple of (short_name_with_suffix, full_nim_id).
        short_name_with_suffix: e.g. "deepseek-v4-flash[1m]" - for settings.json keys.
        full_nim_id: e.g. "deepseek-ai/deepseek-v4-flash" - for .env MODEL on Linux.
    """
    if tested_models is None:
        tested_models = set()
    if extra_models is None:
        extra_models = []

    model_choices = _PRESET_MODELS[:-1] + extra_models + [_PRESET_MODELS[-1]]
    model_idx = _prompt_choices("  Select a model:", model_choices, default=0)
    if model_idx == len(model_choices) - 1:
        full_model = _prompt("Enter model name (org/name format)")
        while "/" not in full_model:
            print("  Must be in 'org/name' format (e.g. deepseek-ai/deepseek-v4-flash)")
            full_model = _prompt("Enter model name")
        # Track custom models so they appear in subsequent tier prompts
        _add_unique(extra_models, full_model)
    else:
        full_model = model_choices[model_idx]

    # Skip test if this model was already confirmed working
    if full_model in tested_models:
        print(f"  {full_model} already tested - OK")
    else:
        while True:
            print(f"  Testing {full_model}...")
            ok, model_msg = _test_model(api_key, full_model)
            if ok:
                print(f"  {full_model} is available!")
                tested_models.add(full_model)
                break

            print(f"  {model_msg}")
            print(f"  {full_model} is not responding right now.")
            retry_choices = [
                "Try this model again",
                "Pick a different model",
                "Proceed anyway",
            ]
            choice = _prompt_choices(
                "  What would you like to do?", retry_choices, default=0
            )
            if choice == 0:
                continue
            elif choice == 1:
                model_idx = _prompt_choices(
                    "  Select a model:", model_choices, default=0
                )
                if model_idx == len(model_choices) - 1:
                    full_model = _prompt("Enter model name (org/name format)")
                    while "/" not in full_model:
                        print(
                            "  Must be in 'org/name' format (e.g. deepseek-ai/deepseek-v4-flash)"
                        )
                        full_model = _prompt("Enter model name")
                else:
                    full_model = model_choices[model_idx]
                continue
            else:
                print("  Proceeding with unavailable model.")
                break

    # Choose context window for this model
    print()
    ctx_choices = ["200k tokens (default)", "1 million tokens [1m]"]
    ctx_idx = _prompt_choices("  Context window:", ctx_choices, default=0)
    suffix = "[1m]" if ctx_idx == 1 else ""
    short = _get_short_name(full_model)
    return (f"{short}{suffix}", full_model)


def _add_unique(lst: list[str], item: str) -> None:
    """Append item to list if not already present."""
    if item not in lst:
        lst.append(item)


def _load_custom_models(exe_dir: Path) -> list[str]:
    """Load previously-saved custom model names from exe directory."""
    models_file = exe_dir / ".nimbus_models"
    if not models_file.exists():
        return []
    try:
        data = json.loads(models_file.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError, OSError:
        return []


def _save_custom_models(exe_dir: Path, models: list[str]) -> None:
    """Save custom model names so they appear in future --init runs."""
    if models:
        models_file = exe_dir / ".nimbus_models"
        models_file.write_text(json.dumps(models, indent=2), encoding="utf-8")


def _get_mcp_add_command(is_linux: bool, exe_dir: Path, frozen: bool) -> str:
    """Get the correct claude mcp add command for websearch based on execution mode.

    Args:
        is_linux: Running on Linux
        exe_dir: Directory containing the executable or script
        frozen: Running as PyInstaller frozen exe

    Returns:
        The claude mcp add command string.
    """
    if frozen:
        # Running as standalone exe
        if is_linux:
            exe_path = exe_dir / "nimbus"
        else:
            exe_path = exe_dir / "nimbus.exe"
        return f'claude mcp add websearch -- "{exe_path}" --mcp'
    else:
        # Running as python source - use venv python
        # Check for venv in common locations
        venv_python = exe_dir / "venv" / "bin" / "python"
        if not venv_python.exists():
            venv_python = exe_dir / "venv" / "Scripts" / "python.exe"
        if not venv_python.exists():
            venv_python = exe_dir / ".venv" / "bin" / "python"
        if not venv_python.exists():
            venv_python = exe_dir / ".venv" / "Scripts" / "python.exe"

        if venv_python.exists():
            python_path = str(venv_python)
        else:
            # Fallback to system python
            python_path = "python"

        start_server = exe_dir / "start_server.py"
        return f'claude mcp add websearch -- "{python_path}" "{start_server}" --mcp'


# ---------------------------------------------------------------------------
# Settings path discovery (Linux)
# ---------------------------------------------------------------------------

_LINUX_SETTINGS_PATHS: list[str] = [
    # $CLAUDE_CONFIG_DIR overrides the base dir entirely
    # $XDG_CONFIG_HOME/claude/ is used when the env var is set
    # ~/.config/claude/ is the XDG default
    # ~/.claude/ is the legacy default (same as Windows)
    "CLAUDE_CONFIG_DIR",
    "XDG_CONFIG_HOME",
    "~/.config/claude",
    "~/.claude",
]


def _find_claude_settings_path() -> Path | None:
    """Search common Linux paths for Claude Code's settings.json.

    Search order:
      1. $CLAUDE_CONFIG_DIR/settings.json
      2. $XDG_CONFIG_HOME/claude/settings.json
      3. ~/.config/claude/settings.json
      4. ~/.claude/settings.json

    Returns the first existing path, or None if none found.
    """
    # 1. CLAUDE_CONFIG_DIR env var
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "")
    if config_dir:
        candidate = Path(config_dir) / "settings.json"
        if candidate.exists():
            return candidate

    # 2. XDG_CONFIG_HOME env var
    xdg_config = os.environ.get("XDG_CONFIG_HOME", "")
    if xdg_config:
        candidate = Path(xdg_config) / "claude" / "settings.json"
        if candidate.exists():
            return candidate

    # 3. XDG default: ~/.config/claude/settings.json
    candidate = Path.home() / ".config" / "claude" / "settings.json"
    if candidate.exists():
        return candidate

    # 4. Legacy default: ~/.claude/settings.json
    candidate = Path.home() / ".claude" / "settings.json"
    if candidate.exists():
        return candidate

    return None


def _load_saved_settings_path(exe_dir: Path) -> Path | None:
    """Load a previously-saved settings.json path from the exe directory."""
    path_file = exe_dir / ".nimbus_settings_path"
    if not path_file.exists():
        return None
    try:
        data = json.loads(path_file.read_text(encoding="utf-8"))
        if isinstance(data, str):
            p = Path(data)
            if p.exists():
                return p
    except json.JSONDecodeError, OSError:
        pass
    return None


def _save_settings_path(exe_dir: Path, path: Path) -> None:
    """Save a settings.json path so --init linux restore can find it later."""
    path_file = exe_dir / ".nimbus_settings_path"
    path_file.write_text(json.dumps(str(path)), encoding="utf-8")


def _prompt_for_settings_path() -> Path:
    """Ask the user to enter the path to their settings.json.

    Prints the list of locations already searched, then prompts.
    If the entered path doesn't exist, offers to create it.
    Re-prompts until a valid path is given.
    """
    print()
    print("  Could not find Claude Code settings.json in any of these locations:")
    for loc in _LINUX_SETTINGS_PATHS:
        print(f"    - {loc}")
    print()
    while True:
        raw = input("  Enter path to Claude Code settings.json: ").strip()
        if not raw:
            print("  Path cannot be empty.")
            continue
        path = Path(raw).expanduser().resolve()
        if path.exists():
            return path
        # Offer to create the file (and parent dirs) with an empty JSON object
        if _prompt_yes_no("  Path does not exist. Create it?", default=True):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}\n", encoding="utf-8")
            print(f"  Created: {path}")
            return path
        print("  Enter a different path.")


def _determine_settings_path(exe_dir: Path, is_linux: bool) -> Path:
    """Resolve the settings.json path for the current platform.

    On Windows: returns Path.home() / ".claude" / "settings.json" (unchanged).
    On Linux: tries saved path, then search, then prompts the user.
    """
    if not is_linux:
        return Path.home() / ".claude" / "settings.json"

    # 1. Try saved path first
    saved = _load_saved_settings_path(exe_dir)
    if saved is not None:
        return saved

    # 2. Search common locations
    found = _find_claude_settings_path()
    if found is not None:
        _save_settings_path(exe_dir, found)
        return found

    # 3. Ask the user
    user_path = _prompt_for_settings_path()
    _save_settings_path(exe_dir, user_path)
    return user_path


def _print_banner() -> None:
    """Print welcome banner."""
    print()
    print("=" * 60)
    print("  NIMbus -- Claude Code / NVIDIA NIM Proxy")
    print("  Version 2.0.0")
    print("  Interactive Setup Wizard")
    print("=" * 60)
    print()
    print("  This wizard will:")
    print("  1. Configure your NVIDIA NIM API key")
    print("  2. Set up a proxy API key")
    print("  3. Pick a default AI model")
    print("  4. Configure Claude Code to use this proxy")
    print()
    print("  Press Ctrl+C at any time to exit.")
    print()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_wizard(exe_dir: Path, argv: list[str]) -> None:
    """Run the interactive setup wizard (called from packaged_entry.py).

    Platform dispatch:
      --init                Windows mode (default, unchanged)
      --init restore        Restore from ~/.claude/ (Windows)
      --init linux          Linux mode (searches for settings.json)
      --init linux restore  Restore from saved Linux settings path
    """
    is_linux = "linux" in argv
    is_restore = "restore" in argv

    if is_restore:
        if is_linux:
            settings_path = _determine_settings_path(exe_dir, is_linux=True)
            _restore_settings(settings_path=settings_path)
        else:
            _restore_settings()
        return

    # Check for existing .env and offer update mode
    env_path = exe_dir / ".env"
    is_update = False
    update_sections = None
    existing = {}

    if env_path.exists() and not is_restore:
        if _prompt_yes_no("Existing .env found. Run in update mode (select sections)?", default=True):
            is_update = True
            update_sections = _select_update_sections()
            if "all" in update_sections or not update_sections:
                is_update = False  # Will run full wizard
            else:
                existing = _load_existing_env(exe_dir)
        else:
            if not _prompt_yes_no("Overwrite existing .env with full wizard?", default=False):
                print("Setup cancelled.")
                return

    # Resolve settings.json path for this platform (used in Steps 7-8)
    settings_path = _determine_settings_path(exe_dir, is_linux)

    try:
        _print_banner()

        if is_update:
            # ------ UPDATE MODE: Run only selected sections ------
            updates = {}
            tested_models: set[str] = set()
            extra_models: list[str] = _load_custom_models(exe_dir)
            nvidia_key = existing.get("NVIDIA_NIM_API_KEY", "")

            for section in update_sections:
                section_updates = _run_section(
                    section, existing, nvidia_key, is_linux, exe_dir,
                    tested_models, extra_models
                )
                updates.update(section_updates)

            # Merge with existing values
            merged = {**existing, **updates}

            # Write updated .env
            env_path = exe_dir / ".env"
            _write_dotenv(
                env_path,
                {
                    "nvidia_key": merged.get("NVIDIA_NIM_API_KEY", merged.get("nvidia_key", "")),
                    "port": int(merged.get("PORT", 8082)),
                    "proxy_key": merged.get("PROXY_API_KEY", merged.get("proxy_key", "")),
                    "thinking": merged.get("NIM_THINKING", "true") == "true",
                    "server_type": merged.get("SERVER_TYPE", "stream"),
                    "provider_max_wait": float(merged.get("PROVIDER_MAX_WAIT_TIME", 30)),
                    "provider_retry_on_truncation": int(merged.get("PROVIDER_RETRY_ON_TRUNCATION", 3)),
                    "provider_retry_delay": float(merged.get("PROVIDER_RETRY_DELAY", 1.0)),
                    "enable_recap_skip": merged.get("ENABLE_RECAP_SKIP", "true"),
                    "enable_network_probe_mock": merged.get("ENABLE_NETWORK_PROBE_MOCK", "true"),
                    "enable_title_generation_skip": merged.get("ENABLE_TITLE_GENERATION_SKIP", "true"),
                    "enable_suggestion_mode_skip": merged.get("ENABLE_SUGGESTION_MODE_SKIP", "true"),
                    "enable_filepath_extraction_mock": merged.get("ENABLE_FILEPATH_EXTRACTION_MOCK", "true"),
                    "model_sonnet_full": merged.get("model_sonnet_full", "deepseek-ai/deepseek-v4-flash"),
                    "model_opus_full": merged.get("model_opus_full", "deepseek-ai/deepseek-v4-flash"),
                    "model_haiku_full": merged.get("model_haiku_full", "deepseek-ai/deepseek-v4-flash"),
                    "mcp_fetch_timeout": float(merged.get("WEB_SEARCH_FETCH_TIMEOUT", 10.0)),
                    # Discord Bot settings - prefer new-style keys (from updates) over old DISCORD_* keys (from existing)
                    "configure_discord": updates.get("configure_discord", merged.get("DISCORD_ENABLED", "false") == "true"),
                    "discord_token": updates.get("discord_token", merged.get("DISCORD_BOT_TOKEN", "")),
                    "discord_model": updates.get("discord_model", merged.get("DISCORD_MODEL", "")),
                    "discord_guild_id": updates.get("discord_guild_id", merged.get("DISCORD_GUILD_ID", 0)),
                    "discord_control_channel_id": updates.get("discord_control_channel_id", merged.get("DISCORD_CONTROL_CHANNEL_ID", 0)),
                    "discord_conversation_category_id": updates.get("discord_conversation_category_id", merged.get("DISCORD_CONVERSATION_CATEGORY_ID", 0)),
                    "discord_conversation_channel_id": updates.get("discord_conversation_channel_id", merged.get("DISCORD_CONVERSATION_CHANNEL_ID", "")),
                    "discord_owner_id": updates.get("discord_owner_id", merged.get("DISCORD_OWNER_ID", 0)),
                    "discord_owner_only": updates.get("discord_owner_only", merged.get("DISCORD_OWNER_ONLY", "true") == "true"),
                    "discord_max_tokens": updates.get("discord_max_tokens", int(merged.get("DISCORD_MAX_TOKENS", 202000))),
                    "discord_compact_threshold": updates.get("discord_compact_threshold", float(merged.get("DISCORD_COMPACT_THRESHOLD", 0.8))),
                    "discord_user_cooldown": updates.get("discord_user_cooldown", int(merged.get("DISCORD_USER_COOLDOWN", 10))),
                    "discord_server_limit": updates.get("discord_server_limit", int(merged.get("DISCORD_SERVER_LIMIT", 20))),
                    "discord_server_window": updates.get("discord_server_window", int(merged.get("DISCORD_SERVER_WINDOW", 60))),
                    "discord_system_prompt": updates.get("discord_system_prompt", merged.get("DISCORD_SYSTEM_PROMPT", "")),
                    "discord_skip_files": updates.get("discord_skip_files", merged.get("DISCORD_SKIP_FILES", "true") == "true"),
                    "discord_split_threshold": updates.get("discord_split_threshold", int(merged.get("DISCORD_SPLIT_THRESHOLD", 1900))),
                    "discord_auto_compact": updates.get("discord_auto_compact", merged.get("DISCORD_AUTO_COMPACT", "true") == "true"),
                    "discord_cmd_ask": updates.get("discord_cmd_ask", merged.get("DISCORD_CMD_ASK", "true") == "true"),
                    "discord_cmd_compact": updates.get("discord_cmd_compact", merged.get("DISCORD_CMD_COMPACT", "true") == "true"),
                    "discord_cmd_new": updates.get("discord_cmd_new", merged.get("DISCORD_CMD_NEW", "true") == "true"),
                    "discord_cmd_status": updates.get("discord_cmd_status", merged.get("DISCORD_CMD_STATUS", "true") == "true"),
                    "discord_cmd_download": updates.get("discord_cmd_download", merged.get("DISCORD_CMD_DOWNLOAD", "true") == "true"),
                    "discord_cmd_block": updates.get("discord_cmd_block", merged.get("DISCORD_CMD_BLOCK", "true") == "true"),
                    "discord_cmd_unblock": updates.get("discord_cmd_unblock", merged.get("DISCORD_CMD_UNBLOCK", "true") == "true"),
                    "discord_cmd_blocked": updates.get("discord_cmd_blocked", merged.get("DISCORD_CMD_BLOCKED", "true") == "true"),
                    "discord_cmd_newchannel": updates.get("discord_cmd_newchannel", merged.get("DISCORD_CMD_NEWCHANNEL", "true") == "true"),
                },
                is_linux=is_linux,
            )

            # Update settings.json if models or proxy_key changed
            if "models" in update_sections or "proxy_key" in update_sections:
                base_url = merged.get("ANTHROPIC_BASE_URL", f"http://localhost:{merged.get('PORT', 8082)}")
                proxy_key = merged.get("PROXY_API_KEY", merged.get("proxy_key", ""))
                model_sonnet = merged.get("model_sonnet", "deepseek-v4-flash")
                model_opus = merged.get("model_opus", model_sonnet)
                model_haiku = merged.get("model_haiku", model_sonnet)

                settings_params = {
                    "base_url": base_url,
                    "proxy_key": proxy_key,
                    "model_full": model_sonnet,
                    "model_sonnet": model_sonnet,
                    "model_opus": model_opus,
                    "model_haiku": model_haiku,
                }
                _write_settings_json(settings_path, settings_params)
                print("  7 env keys written to settings.json.")

            model_display = (
                _build_model_list_str(
                    merged.get("model_sonnet_full", "deepseek-ai/deepseek-v4-flash"),
                    merged.get("model_opus_full", "deepseek-ai/deepseek-v4-flash"),
                    merged.get("model_haiku_full", "deepseek-ai/deepseek-v4-flash"),
                )
                if is_linux
                else "windows:settings.json"
            )
            print(f"  .env written with MODEL={model_display}")

            # ---- Summary ----
            # Derive short model names from full names for summary display
            def _short(full: str) -> str:
                name = full.split("/")[-1]
                return name.split("[")[0]  # Remove any [1m] suffix

            _print_summary(
                {
                    "base_url": merged.get("ANTHROPIC_BASE_URL", f"http://localhost:{merged.get('PORT', 8082)}"),
                    "proxy_key": merged.get("PROXY_API_KEY", merged.get("proxy_key", "")),
                    "model_sonnet": _short(merged.get("model_sonnet_full", "deepseek-ai/deepseek-v4-flash")),
                    "model_opus": _short(merged.get("model_opus_full", "deepseek-ai/deepseek-v4-flash")),
                    "model_haiku": _short(merged.get("model_haiku_full", "deepseek-ai/deepseek-v4-flash")),
                    "port": merged.get("PORT", 8082),
                    "configure_mcp": merged.get("configure_mcp", True),
                    "configure_discord": merged.get("configure_discord", False),
                    "mcp_fetch_timeout": merged.get("mcp_fetch_timeout", 10.0),
                    "is_linux": is_linux,
                    "exe_dir": exe_dir,
                },
                update_mode=True,
                updated_sections=update_sections,
            )
            return
        else:
            # ------ FULL WIZARD MODE ------
            # ---- Step 1: NVIDIA API Key ----
            print()
            print("Step 1: NVIDIA NIM API Key")
            print("-" * 40)
            nvidia_key = _prompt("Enter your NVIDIA NIM API key", password=True)
            while not nvidia_key or nvidia_key == "<replaceme>":
                print("  API key cannot be empty.")
                nvidia_key = _prompt("Enter your NVIDIA NIM API key", password=True)

            print("  Testing API key...")
            found, working_model, msg = _find_working_model(nvidia_key)
            if not found:
                print(f"  {msg}")
                print("  No models responded - check your key and try again later.")
                return

            print(f"  Key is valid! (tested with {working_model})")

            # ---- Step 2: Proxy API Key ----
            print()
            print("Step 2: Proxy API Key")
            print("-" * 40)
            print(
                "  This key protects your proxy. Claude Code will use it to authenticate."
            )
            choices = ["Auto-generate a random key (recommended)", "Enter my own key"]
            idx = _prompt_choices("  Choose an option:", choices, default=0)
            if idx == 0:
                proxy_key = _generate_api_key()
                masked = proxy_key[:8] + "..." + proxy_key[-4:]
                print(f"  Generated: {masked}")
            else:
                proxy_key = _prompt("Enter your proxy API key")
                while not proxy_key:
                    print("  Proxy API key cannot be empty.")
                    proxy_key = _prompt("Enter your proxy API key")

        # ---- Step 3: Model ----
        print()
        print("Step 3: Default Model")
        print("-" * 40)
        tested_models: set[str] = set()
        extra_models: list[str] = _load_custom_models(exe_dir)
        # Unpack tuple: (short_name_with_suffix, full_nim_id)
        model_sonnet, model_sonnet_full = _pick_and_test_model(
            nvidia_key, tested_models, extra_models
        )

        # Ask about per-tier models
        print()
        if _prompt_yes_no(
            "  Use the same model for all Claude tiers (Sonnet/Opus/Haiku)?",
            default=True,
        ):
            model_opus = model_sonnet
            model_haiku = model_sonnet
            model_opus_full = model_sonnet_full
            model_haiku_full = model_sonnet_full
        else:
            print()
            print("  Model for Sonnet (Default tier, 1M context):")
            model_sonnet, model_sonnet_full = _pick_and_test_model(
                nvidia_key, tested_models, extra_models
            )
            print()
            print("  Model for Opus tier (1M context):")
            model_opus, model_opus_full = _pick_and_test_model(
                nvidia_key, tested_models, extra_models
            )
            print()
            print("  Model for Haiku tier (200k context):")
            model_haiku, model_haiku_full = _pick_and_test_model(
                nvidia_key, tested_models, extra_models
            )

        _save_custom_models(exe_dir, extra_models)

        # ---- Step 4: Port ----
        print()
        print("Step 4: Server Port")
        print("-" * 40)
        port_str = _prompt("Port for the proxy server", default="8082")
        while True:
            try:
                port = int(port_str)
                if 1024 <= port <= 65535:
                    break
                print("  Port must be 1024-65535.")
            except ValueError:
                print("  Port must be a number.")
            port_str = _prompt("Port", default="8082")

        base_url = f"http://localhost:{port}"

        # ---- Step 5: Server type ----
        print()
        print("Step 5: Server Mode")
        print("-" * 40)
        print("  stream  → Tokens arrive live as they're generated (like ChatGPT).")
        print("            May fail mid-stream if the Nvidia backend cuts out.")
        print("  buffer  → Waits for the full Nvidia response, retries on failure,")
        print("            then sends the complete result. More reliable but slower.")
        print()
        server_type_choices = [
            "stream (live, may fail mid-stream)",
            "buffer (reliable, slower)",
        ]
        st_idx = _prompt_choices(
            "  Choose server mode:", server_type_choices, default=0
        )
        server_type = "stream" if st_idx == 0 else "buffer"

        # Buffer-specific options
        provider_max_wait = 30
        provider_retry_on_truncation = 3
        provider_retry_delay = 1.0
        if server_type == "buffer":
            print()
            print("  Buffer Mode Settings:")
            print("  These only apply in buffer mode. Stream mode ignores them.")
            print()
            print("  Max Wait Time: How many seconds to wait for Nvidia to start")
            print("  responding before retrying. Default: 30s")
            wait_str = _prompt("  Max wait time (seconds)", default="30")
            try:
                provider_max_wait = float(wait_str)
            except ValueError:
                provider_max_wait = 30.0
            print()
            print("  Retry on Truncation: How many times to retry if the response")
            print("  is truncated/incomplete. 0 = keep retrying forever.")
            retry_str = _prompt("  Retry attempts", default="3")
            try:
                provider_retry_on_truncation = int(retry_str)
            except ValueError:
                provider_retry_on_truncation = 3
            print()
            print("  Retry Delay: Base seconds to wait between retries.")
            print("  (exponential backoff is applied on top of this)")
            delay_str = _prompt("  Retry delay (seconds)", default="1.0")
            try:
                provider_retry_delay = float(delay_str)
            except ValueError:
                provider_retry_delay = 1.0

        # ---- Step 6: Optimization Settings ----
        print()
        print("Step 6: Optimization Settings")
        print("-" * 40)
        print("  NIMbus can skip unnecessary requests from Claude Code")
        print("  to save API calls. Each option is enabled by default.")
        print()
        print("  NOTE: Suggestion mode skip and recap skip are DISABLED by default")
        print("  because their detection logic currently produces false positives.")
        print("  You can enable them here, but they may cause issues.")
        print()
        opts = {}
        opt_defs = [
            (
                "enable_recap_skip",
                "Skip recap requests when you return after stepping away (disabled by default)",
            ),
            ("enable_network_probe_mock", "Mock quota/network probe requests"),
            ("enable_title_generation_skip", "Skip conversation title generation"),
            ("enable_suggestion_mode_skip", "Skip suggestion mode requests (disabled by default)"),
            (
                "enable_filepath_extraction_mock",
                "Mock filepath extraction (speeds up file searching)",
            ),
        ]
        for key, desc in opt_defs:
            # Default to False for the two disabled optimizations
            default = False if key in ("enable_recap_skip", "enable_suggestion_mode_skip") else True
            yn = _prompt_yes_no(f"  {desc}?", default=default)
            opts[key] = "true" if yn else "false"
            print()
        enable_recap_skip = opts["enable_recap_skip"]
        enable_network_probe_mock = opts["enable_network_probe_mock"]
        enable_title_generation_skip = opts["enable_title_generation_skip"]
        enable_suggestion_mode_skip = opts["enable_suggestion_mode_skip"]
        enable_filepath_extraction_mock = opts["enable_filepath_extraction_mock"]

        # ---- Step 7: MCP Server Configuration ----
        print()
        print("Step 7: MCP Server Configuration")
        print("-" * 40)
        print("  NIMbus can run as an MCP (Model Context Protocol) server")
        print("  exposing web search and page fetch tools to Claude Code.")
        print()
        configure_mcp = _prompt_yes_no(
            "  Configure MCP server (web search tools)?", default=True
        )
        mcp_fetch_timeout = 10.0
        if configure_mcp:
            print()
            mcp_fetch_timeout_str = _prompt(
                "  Fetch timeout (seconds)", default="10.0"
            )
            try:
                mcp_fetch_timeout = float(mcp_fetch_timeout_str)
            except ValueError:
                mcp_fetch_timeout = 10.0

        # ---- Step 8: Discord Bot Configuration ----
        print()
        print("Step 8: Discord Bot Configuration")
        print("-" * 40)
        print("  NIMbus can run as a Discord bot, allowing users to chat")
        print("  with NIM models from Discord channels.")
        print()
        configure_discord = _prompt_yes_no(
            "  Enable Discord bot integration?", default=False
        )
        discord_token = ""
        discord_guild_id = ""
        discord_control_channel_id = "0"
        discord_conversation_category_id = "0"
        discord_conversation_channel_id = ""
        discord_owner_id = "0"
        discord_owner_only = True
        discord_max_tokens = 202000
        discord_compact_threshold = 0.8
        discord_user_cooldown = 10
        discord_server_limit = 20
        discord_server_window = 60
        discord_system_prompt = "You are a helpful Discord bot. Be friendly, casual, and conversational. Talk like a normal person - don't use formal analysis headers, bullet points, or structured formatting unless specifically asked. Keep responses natural and direct."
        discord_skip_files = True
        discord_split_threshold = 1900
        discord_auto_compact = True
        discord_cmd_ask = True
        discord_cmd_compact = True
        discord_cmd_new = True
        discord_cmd_status = True
        discord_cmd_download = True
        discord_cmd_block = True
        discord_cmd_unblock = True
        discord_cmd_blocked = True
        discord_cmd_newchannel = True
        if configure_discord:
            print()
            print("  The bot token is the secret token from your Discord Application.")
            print("  Get it at: https://discord.com/developers/applications")
            discord_token = _prompt("  Discord bot token", password=True)
            while not discord_token:
                print("  Bot token cannot be empty.")
                discord_token = _prompt("  Discord bot token", password=True)
            print()
            print("  The guild/server ID locks the bot to specific servers.")
            print("  You can enter multiple IDs separated by commas.")
            print("  Right-click a server -> Copy Server ID to get it.")
            print("  (Requires Developer Mode in Discord settings.)")
            discord_guild_id = _prompt(
                "  Guild/Server IDs (comma-separated for multiple)",
                default="0",
            )
            print()
            print("  Control panel channels are where users can use /ask commands")
            print("  without creating dedicated conversation channels.")
            print("  Enter channel IDs (comma-separated for multiple).")
            print("  Right-click a channel -> Copy Channel ID to get it.")
            discord_control_channel_id = _prompt(
                "  Control Panel Channel IDs (comma-separated)",
                default="0",
            )
            print()
            print("  When a user sends /newchannel, a new conversation channel")
            print("  is created under this category. Leave as 0 to use the")
            print("  control channel only (no dedicated channels).")
            discord_conversation_category_id = _prompt(
                "  Conversation Category ID (bot creates channels here)",
                default="0",
            )
            print()
            print("  Specific channels where the bot responds (alternative to")
            print("  category-based channels). The bot will listen in these")
            print("  channels OR channels under the category above.")
            print("  Comma-separate multiple channel IDs. Leave blank to")
            print("  rely on categories/control channels only.")
            discord_conversation_channel_id = _prompt(
                "  Specific Conversation Channel IDs (comma-separated, or blank)",
                default="",
            )
            print()
            print("  Your Discord user ID grants you owner privileges such as")
            print("  blocking users, unblocking users, and managing the bot.")
            print("  Right-click yourself -> Copy User ID to get it.")
            discord_owner_id = _prompt(
                "  Your Discord User ID (for owner privileges)",
                default="0",
            )
            print()
            print("  When enabled, only the owner can use the bot.")
            print("  When disabled, anyone in the server can chat with it.")
            discord_owner_only = _prompt_yes_no(
                "  Restrict bot to owner only?", default=True
            )
            print()
            print("  Token Management:")
            print("  These settings control when conversations are compacted")
            print("  (summarized) to free up token space.")
            print("  Max tokens per conversation before compaction triggers.")
            print("  Must match or be less than your NIM model's output limit.")
            print("  Default: 202000 (matches NIM_MAX_TOKENS).")
            discord_max_tokens_str = _prompt(
                "  Max tokens per conversation",
                default="202000",
            )
            try:
                discord_max_tokens = int(discord_max_tokens_str)
            except ValueError:
                discord_max_tokens = 202000
            print()
            print("  At what fraction of max_tokens should compaction trigger?")
            print("  E.g., 0.8 = compact when conversation reaches 80% of max_tokens.")
            print("  Range: 0.0 to 1.0. Higher = compacts later.")
            discord_compact_threshold_str = _prompt(
                "  Compact threshold (0.0-1.0)",
                default="0.8",
            )
            try:
                discord_compact_threshold = float(discord_compact_threshold_str)
            except ValueError:
                discord_compact_threshold = 0.8
            print()
            print("  When auto-compact is enabled, the bot automatically summarizes")
            print("  conversations when the token threshold is reached.")
            print("  When disabled, users must manually use /compact.")
            discord_auto_compact = _prompt_yes_no(
                "  Auto-compact when threshold reached?", default=True
            )
            print()
            print("  Rate Limiting:")
            print("  These settings prevent spam and abuse.")
            print("  User cooldown: Minimum seconds a user must wait between")
            print("  sending messages to the bot. Default: 10 seconds.")
            discord_user_cooldown_str = _prompt(
                "  User cooldown (seconds between messages)",
                default="10",
            )
            try:
                discord_user_cooldown = int(discord_user_cooldown_str)
            except ValueError:
                discord_user_cooldown = 10
            print()
            print("  Server rate limit: Maximum number of requests the entire")
            print("  server can make within the rate window. Default: 20.")
            discord_server_limit_str = _prompt(
                "  Server rate limit (max requests per window)",
                default="20",
            )
            try:
                discord_server_limit = int(discord_server_limit_str)
            except ValueError:
                discord_server_limit = 20
            print()
            print("  Server rate window: The time window in seconds for the")
            print("  server rate limit. Default: 60 seconds.")
            print("  E.g., 20 requests per 60 seconds = 20 req/min per server.")
            discord_server_window_str = _prompt(
                "  Server rate window (seconds)",
                default="60",
            )
            try:
                discord_server_window = int(discord_server_window_str)
            except ValueError:
                discord_server_window = 60
            print()
            print("  Message Handling:")
            print("  When enabled, the bot ignores messages that contain file")
            print("  attachments (images, documents, etc.).")
            discord_skip_files = _prompt_yes_no(
                "  Skip messages with file attachments?", default=True
            )
            print()
            print("  Discord has a 2000 character limit per message. The bot")
            print("  will split long LLM responses into multiple messages.")
            print("  Default: 1900 chars (leaves room for formatting).")
            discord_split_threshold_str = _prompt(
                "  Message split threshold (max chars before splitting)",
                default="1900",
            )
            try:
                discord_split_threshold = int(discord_split_threshold_str)
            except ValueError:
                discord_split_threshold = 1900
            print()
            print("  Discord Model (optional, separate from main MODEL):")
            print("  Use a different model for Discord bot vs the main API proxy.")
            print("  Leave empty to use the MODEL setting (or windows:settings.json picks Opus > Sonnet > Haiku).")
            discord_model = _prompt(
                "  Discord model (owner/model-name, or empty to use MODEL)",
                default="",
            )
            print()
            print("  Command Toggles:")
            print("  You can disable any of the following slash commands.")
            print("  All commands default to enabled (true).")
            print()
            print("  /ask - Ask the AI a question with conversation history")
            discord_cmd_ask = _prompt_yes_no("  Enable /ask command?", default=True)
            print()
            print("  /compact - Summarize the conversation and start fresh")
            discord_cmd_compact = _prompt_yes_no("  Enable /compact command?", default=True)
            print()
            print("  /new - Clear conversation without saving a summary")
            discord_cmd_new = _prompt_yes_no("  Enable /new command?", default=True)
            print()
            print("  /status - Show bot status, rate limits, and stats")
            discord_cmd_status = _prompt_yes_no("  Enable /status command?", default=True)
            print()
            print("  /download - Download conversation history as markdown")
            discord_cmd_download = _prompt_yes_no("  Enable /download command?", default=True)
            print()
            print("  /block - Block a user from using the bot (owner only)")
            discord_cmd_block = _prompt_yes_no("  Enable /block command?", default=True)
            print()
            print("  /unblock - Unblock a previously blocked user (owner only)")
            discord_cmd_unblock = _prompt_yes_no("  Enable /unblock command?", default=True)
            print()
            print("  /blocked - List all blocked users (owner only)")
            discord_cmd_blocked = _prompt_yes_no("  Enable /blocked command?", default=True)
            print()
            print("  /newchannel - Create a new dedicated conversation channel")
            discord_cmd_newchannel = _prompt_yes_no("  Enable /newchannel command?", default=True)

        # ---- Step 9: Claude Code settings ----
        print()
        print("Step 8: Claude Code Configuration")
        print("-" * 40)
        print(f"  Target: {settings_path}")

        backup_path = None
        if settings_path.exists() and _prompt_yes_no(
            "  Backup existing settings before modifying?", default=True
        ):
            backup_path = _backup_settings(settings_path)
            if backup_path:
                print(f"  Backup saved to: {backup_path.name}")

        settings_params = {
            "base_url": base_url,
            "proxy_key": proxy_key,
            "model_full": model_sonnet,
            "model_sonnet": model_sonnet,
            "model_opus": model_opus,
            "model_haiku": model_haiku,
        }
        _write_settings_json(settings_path, settings_params)
        print("  7 env keys written to settings.json.")

        # ---- Step 9: .env ----
        print()
        print("Step 9: Writing .env")
        print("-" * 40)
        env_path = exe_dir / ".env"
        _write_dotenv(
            env_path,
            {
                "nvidia_key": nvidia_key,
                "port": port,
                "proxy_key": proxy_key,
                "thinking": "deepseek" in model_sonnet.lower(),
                "server_type": server_type,
                "provider_max_wait": provider_max_wait,
                "provider_retry_on_truncation": provider_retry_on_truncation,
                "provider_retry_delay": provider_retry_delay,
                "enable_recap_skip": enable_recap_skip,
                "enable_network_probe_mock": enable_network_probe_mock,
                "enable_title_generation_skip": enable_title_generation_skip,
                "enable_suggestion_mode_skip": enable_suggestion_mode_skip,
                "enable_filepath_extraction_mock": enable_filepath_extraction_mock,
                # Full NIM model IDs for Linux .env MODEL line
                "model_sonnet_full": model_sonnet_full,
                "model_opus_full": model_opus_full,
                "model_haiku_full": model_haiku_full,
                # MCP Server settings
                "mcp_fetch_timeout": mcp_fetch_timeout,
                # Discord Bot settings
                "configure_discord": configure_discord,
                "discord_token": discord_token,
                "discord_model": discord_model,
                "discord_guild_id": discord_guild_id,
                "discord_control_channel_id": discord_control_channel_id,
                "discord_conversation_category_id": discord_conversation_category_id,
                "discord_conversation_channel_id": discord_conversation_channel_id,
                "discord_owner_id": discord_owner_id,
                "discord_owner_only": discord_owner_only,
                "discord_max_tokens": discord_max_tokens,
                "discord_compact_threshold": discord_compact_threshold,
                "discord_user_cooldown": discord_user_cooldown,
                "discord_server_limit": discord_server_limit,
                "discord_server_window": discord_server_window,
                "discord_system_prompt": discord_system_prompt,
                "discord_skip_files": discord_skip_files,
                "discord_split_threshold": discord_split_threshold,
                "discord_auto_compact": discord_auto_compact,
                "discord_cmd_ask": discord_cmd_ask,
                "discord_cmd_compact": discord_cmd_compact,
                "discord_cmd_new": discord_cmd_new,
                "discord_cmd_status": discord_cmd_status,
                "discord_cmd_download": discord_cmd_download,
                "discord_cmd_block": discord_cmd_block,
                "discord_cmd_unblock": discord_cmd_unblock,
                "discord_cmd_blocked": discord_cmd_blocked,
                "discord_cmd_newchannel": discord_cmd_newchannel,
            },
            is_linux=is_linux,
        )

        model_display = (
            _build_model_list_str(model_sonnet_full, model_opus_full, model_haiku_full)
            if is_linux
            else "windows:settings.json"
        )
        print(f"  .env written with MODEL={model_display}")

        # ---- Summary ----
        _print_summary(
            {
                "base_url": base_url,
                "proxy_key": proxy_key,
                "model_sonnet": model_sonnet,
                "model_opus": model_opus,
                "model_haiku": model_haiku,
                "port": port,
                "configure_mcp": configure_mcp,
                "mcp_fetch_timeout": mcp_fetch_timeout,
                "configure_discord": configure_discord,
                "is_linux": is_linux,
                "exe_dir": exe_dir,
            }
        )

    except KeyboardInterrupt:
        print("\n\nSetup cancelled. No files were modified.")
    except Exception as e:
        print(f"\nError during setup: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    """Allow running the wizard directly (without packaged_entry.py)."""
    exe_dir = Path(__file__).parent.resolve()
    run_wizard(exe_dir, sys.argv)
