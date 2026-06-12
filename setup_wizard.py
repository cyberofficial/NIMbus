"""Interactive setup wizard for NIMbus standalone executable.

Run with: nimbus.exe --init
Restore settings: nimbus.exe --init restore
"""

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
        if password:
            value = _masked_input(prompt_str)
        else:
            value = input(prompt_str).strip()
        if value:
            return value
        if default:
            return default


def _masked_input(prompt: str) -> str:
    """Read a line from stdin, printing * for each character (Windows)."""
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
                all_models = [m["id"] for m in data.get("data", []) if "/" in m.get("id", "")]
                # Filter to chat completion models (skip embeddings, vision-only, safety)
                _MODELS_CACHE = [
                    m for m in all_models
                    if not any(skip in m.lower() for skip in [
                        "embed", "safety", "guard", "fuyu", "neva", "vila", "deplot",
                        "kosmos", "nv-embed", "nv-clip", "nemoretriever",
                    ])
                ]
        models = _MODELS_CACHE[:] if _MODELS_CACHE else []
    except Exception as e:
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

    return False, "", f"Tried {tested} random models, none responded - key may be invalid."


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_api_key() -> str:
    """Generate random 32-char key in 16chars.16chars format."""
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=16)) + "." + "".join(random.choices(chars, k=16))


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


def _restore_settings() -> None:
    """Restore most recent backup of settings.json."""
    settings_dir = Path.home() / ".claude"
    backups = sorted(settings_dir.glob("settings.json.nimbus-backup-*"))
    if not backups:
        print("No backup found to restore.")
        return
    latest = backups[-1]
    target = settings_dir / "settings.json"
    shutil.copy2(str(latest), str(target))
    print(f"Restored settings.json from: {latest.name}")


def _write_dotenv(path: Path, params: dict) -> None:
    """Write .env with all collected values."""
    content = f"""# NIMbus configuration - generated by --init
NVIDIA_NIM_API_KEY="{params["nvidia_key"]}"
PORT={params["port"]}
MODEL=windows:settings.json
PROXY_API_KEY="{params["proxy_key"]}"
NIM_THINKING={"true" if params["thinking"] else "false"}
SERVER_TYPE={params.get("server_type", "stream")}
PROVIDER_MAX_WAIT_TIME={params.get("provider_max_wait", 30)}
PROVIDER_RETRY_ON_TRUNCATION={params.get("provider_retry_on_truncation", 3)}
PROVIDER_RETRY_DELAY={params.get("provider_retry_delay", 1.0)}
"""
    path.write_text(content, encoding="utf-8")
    print(f"  .env -> {path}")


def _write_settings_json(path: Path, params: dict) -> None:
    """Merge the 7 env keys into settings.json. Leaves everything else untouched."""
    existing = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            existing = {}

    current_env = existing.get("env", {})
    current_env.update({
        "ANTHROPIC_BASE_URL": params["base_url"],
        "ANTHROPIC_AUTH_TOKEN": params["proxy_key"],
        "ANTHROPIC_MODEL": params["model_full"],
        "ANTHROPIC_DEFAULT_OPUS_MODEL": params["model_opus"],
        "ANTHROPIC_DEFAULT_SONNET_MODEL": params["model_sonnet"],
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": params["model_haiku"],
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    })
    existing["env"] = current_env

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"  settings.json -> {path}")


def _print_summary(params: dict) -> None:
    """Print post-setup summary."""
    print()
    print("=" * 60)
    print("  Setup Complete!")
    print("=" * 60)
    print()
    print(f"  Proxy URL:    {params['base_url']}")
    print(f"  Proxy Key:    {params['proxy_key'][:20]}...")
    print(f"  Sonnet:       {params['model_sonnet']}")
    print(f"  Opus:         {params['model_opus']}")
    print(f"  Haiku:        {params['model_haiku']}")
    print(f"  Port:         {params['port']}")
    print(f"  Discord Bot:  Disabled")
    print()
    print("  TO CONNECT CLAUDE CODE:")
    print("  " + "-" * 40)
    print(f'  set ANTHROPIC_AUTH_TOKEN={params["proxy_key"]}')
    print(f'  set ANTHROPIC_BASE_URL={params["base_url"]}')
    print("  claude")
    print()
    print("  Press Ctrl+C to stop the server.")
    print("=" * 60)
    print()


def _pick_and_test_model(api_key: str, tested_models: set[str] | None = None,
                          extra_models: list[str] | None = None) -> str:
    """Let user pick a model, test it (unless already-tested), choose context window.
    Returns the model name with context suffix (e.g. deepseek-v4-flash[1m]).
    tested_models: set of full NIM names already confirmed working - skip retest.
    extra_models: growing list of full NIM names from custom entries - shown as options
                  in subsequent calls. Modified in-place."""
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
            retry_choices = ["Try this model again", "Pick a different model", "Proceed anyway"]
            choice = _prompt_choices("  What would you like to do?", retry_choices, default=0)
            if choice == 0:
                continue
            elif choice == 1:
                model_idx = _prompt_choices("  Select a model:", model_choices, default=0)
                if model_idx == len(model_choices) - 1:
                    full_model = _prompt("Enter model name (org/name format)")
                    while "/" not in full_model:
                        print("  Must be in 'org/name' format (e.g. deepseek-ai/deepseek-v4-flash)")
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
    return f"{short}{suffix}"


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
    except (json.JSONDecodeError, OSError):
        return []


def _save_custom_models(exe_dir: Path, models: list[str]) -> None:
    """Save custom model names so they appear in future --init runs."""
    if models:
        models_file = exe_dir / ".nimbus_models"
        models_file.write_text(json.dumps(models, indent=2), encoding="utf-8")


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
    """Run the interactive setup wizard (called from packaged_entry.py)."""
    if "restore" in argv:
        _restore_settings()
        return

    try:
        _print_banner()

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

        # ---- Step 3: Model ----
        print()
        print("Step 3: Default Model")
        print("-" * 40)
        tested_models: set[str] = set()
        extra_models: list[str] = _load_custom_models(exe_dir)
        full_model = _pick_and_test_model(nvidia_key, tested_models, extra_models)

        # Ask about per-tier models
        print()
        if _prompt_yes_no("  Use the same model for all Claude tiers (Sonnet/Opus/Haiku)?", default=True):
            model_sonnet = full_model
            model_opus = full_model
            model_haiku = full_model
        else:
            print()
            print("  Model for Sonnet (Default tier, 1M context):")
            model_sonnet = _pick_and_test_model(nvidia_key, tested_models, extra_models)
            print()
            print("  Model for Opus tier (1M context):")
            model_opus = _pick_and_test_model(nvidia_key, tested_models, extra_models)
            print()
            print("  Model for Haiku tier (200k context):")
            model_haiku = _pick_and_test_model(nvidia_key, tested_models, extra_models)

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
        server_type_choices = ["stream (live, may fail mid-stream)", "buffer (reliable, slower)"]
        st_idx = _prompt_choices("  Choose server mode:", server_type_choices, default=0)
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

        # ---- Step 6: Claude Code settings ----
        print()
        print("Step 6: Claude Code Configuration")
        print("-" * 40)
        settings_path = Path.home() / ".claude" / "settings.json"
        print(f"  Target: {settings_path}")

        backup_path = None
        if settings_path.exists():
            if _prompt_yes_no("  Backup existing settings before modifying?", default=True):
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

        # ---- Step 7: .env ----
        print()
        print("Step 7: Writing .env")
        print("-" * 40)
        env_path = exe_dir / ".env"
        _write_dotenv(env_path, {
            "nvidia_key": nvidia_key,
            "port": port,
            "proxy_key": proxy_key,
            "thinking": "deepseek" in full_model.lower(),
            "server_type": server_type,
            "provider_max_wait": provider_max_wait,
            "provider_retry_on_truncation": provider_retry_on_truncation,
            "provider_retry_delay": provider_retry_delay,
        })
        print("  .env written with MODEL=windows:settings.json")

        # ---- Summary ----
        _print_summary({
            "base_url": base_url,
            "proxy_key": proxy_key,
            "model_sonnet": model_sonnet,
            "model_opus": model_opus,
            "model_haiku": model_haiku,
            "port": port,
        })

    except KeyboardInterrupt:
        print("\n\nSetup cancelled. No files were modified.")
    except Exception as e:
        print(f"\nError during setup: {e}")
        import traceback
        traceback.print_exc()
