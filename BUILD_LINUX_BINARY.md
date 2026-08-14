# Building NIMbus as a Linux Standalone Binary

> **Purpose:** Step-by-step guide for packaging NIMbus into a single Linux executable using PyInstaller.  
> **Target platform:** `linux-aarch64` (ARM64) — tested on **Proot Debian** running inside **Termux** (Linux 6.2.1-PRoot-Distro-aarch64).  
> **Document created:** after troubleshooting a real build session where statically-linked system Python blocked PyInstaller.  
>
> ⚠️ **Important context:** This environment is a Proot userland (no root privileges, no FUSE, chroot-based system calls). Many standard Linux assumptions (e.g., `lib` paths in `/usr/local`, systemd) do not apply. If you're replicating this on a native Linux distro or in Docker, some steps (especially the `LD_LIBRARY_PATH` workarounds for a portable Python build) may be unnecessary.

---

## Table of Contents

1. [Environment Overview](#1-environment-overview)
2. [The Core Problem](#2-the-core-problem)
3. [The Solution (High Level)](#3-the-solution-high-level)
4. [Detailed Build Instructions](#4-detailed-build-instructions)
5. [Spec File Changes](#5-spec-file-changes)
6. [Running the Binary](#6-running-the-binary)
7. [Troubleshooting](#7-troubleshooting)
8. [What Was Left Behind](#8-what-was-left-behind)

---

## 1. Environment Overview

| Component | Version / Path |
|-----------|----------------|
| **Host app** | Termux (Android terminal emulator) |
| **Linux userland** | Proot Debian (chroot-based, no root, no FUSE) |
| **Kernel** | Linux 6.2.1-PRoot-Distro-aarch64 |
| **Architecture** | ARM aarch64 |
| **Project root** | `/home/cyber/github/NIMbus` |
| **Python (system)** | 3.14.3 at `/home/cyber/.local/bin/python3` — **statically linked** |
| **Python (shared-lib backup)** | 3.14.3 at `python-3.14/bin/python3.14` — **built with `--enable-shared`** |
| **Build tool** | PyInstaller 6.22.0 |
| **Spec file** | `nimbus_linux.spec` |
| **Entry point** | `packaged_entry.py` |

**Key discovery:**
```bash
# System Python is static — PyInstaller will REFUSE it
python3 -c "import sysconfig; print(sysconfig.get_config_var('Py_ENABLE_SHARED'))"
# → 0

# Shared-lib Python exists but needs LD_LIBRARY_PATH because it's not installed system-wide
ls python-3.14/lib/libpython3.14.so.1.0
# → exists
```

---

## 2. The Core Problem

PyInstaller **requires** a Python shared library (`libpython*.so`). When run against the system Python, it errors immediately:

```
ERROR: Python was built without a shared library, which is required by PyInstaller.
If you built Python from source, rebuild it with the `--enable-shared` option.
```

Attempting to use the pre-built shared-lib Python directly also fails because the dynamic linker cannot find `libpython3.14.so.1.0` at runtime (the library lives in `python-3.14/lib/`, not a standard system path like `/usr/lib`).

---

## 3. The Solution (High Level)

1. **Install deps into a temp venv** that points to the shared-lib Python binary.
2. **Set `LD_LIBRARY_PATH`** so the shared-lib Python can bootstrap itself.
3. **Run PyInstaller from that venv** with `LD_LIBRARY_PATH` exported.
4. **Expand `hiddenimports`** in `nimbus_linux.spec` so the frozen app can resolve all internal packages (`api.*`, `providers.*`, `discord_bot.*`, etc.).

---

## 4. Detailed Build Instructions

### Step 4.1 — Verify the shared-lib Python

```bash
cd /home/cyber/github/NIMbus

# Confirm the shared library exists
ls python-3.14/lib/libpython*    # should show .so files

# Test that it works when we tell it where its own library lives
export LD_LIBRARY_PATH=/home/cyber/github/NIMbus/python-3.14/lib:$LD_LIBRARY_PATH
python-3.14/bin/python3.14 --version   # → Python 3.14.3
```

> If this fails with `libpython3.14.so.1.0: cannot open shared object file`, you must keep `LD_LIBRARY_PATH` set for every subsequent command.

### Step 4.2 — Create a temporary build venv

Do **not** use the project’s existing `venv/` (it was created from the static system Python).

```bash
export LD_LIBRARY_PATH=/home/cyber/github/NIMbus/python-3.14/lib:$LD_LIBRARY_PATH

# Create a throw-away venv backed by shared-lib python
python-3.14/bin/python3.14 -m venv /tmp/nimbus_build_venv
```

Verify it points to the right interpreter:

```bash
cat /tmp/nimbus_build_venv/pyvenv.cfg
# executable = /home/cyber/github/NIMbus/python-3.14/bin/python3.14
```

### Step 4.3 — Install build dependencies

```bash
export LD_LIBRARY_PATH=/home/cyber/github/NIMbus/python-3.14/lib:$LD_LIBRARY_PATH

# PyInstaller
/tmp/nimbus_build_venv/bin/pip install pyinstaller

# Runtime dependencies (install the package's required libs)
# NOTE: requirements.txt contains `pywin32` which has no Linux wheel.
# Skip that and install the rest manually, or use the project's normal deps list.
/tmp/nimbus_build_venv/bin/pip install \
  fastapi uvicorn httpx pydantic python-dotenv \
  tiktoken discord.py pydantic-settings openai \
  loguru mcp lxml
```

> If you need `mcp==1.27.2` specifically (as the project does), pin it after the bulk install:
> ```bash
> /tmp/nimbus_build_venv/bin/pip install "mcp==1.27.2"
> ```
>
> The default unpinned install may pull in `mcp 2.x`, which moves `FastMCP` from `mcp.server.fastmcp` to `mcp.server`. Pinning ensures the frozen code matches the project’s import paths.

### Step 4.4 — Download / verify tiktoken cache

The spec bundles a tiktoken encoding cache so the binary works offline.

```bash
export LD_LIBRARY_PATH=/home/cyber/github/NIMbus/python-3.14/lib:$LD_LIBRARY_PATH
/tmp/nimbus_build_venv/bin/python build_exe_download_tiktoken.py
```

Expected output:
```
Already cached: /home/cyber/github/NIMbus/build_resources/tiktoken_cache/9b5ad71b2ce5302211f9c61530b329a4922fc6a4
```

### Step 4.5 — Update `nimbus_linux.spec`

The existing `nimbus_linux.spec` had a very short `hiddenimports` list. Because the project is not a single-package installable wheel, PyInstaller cannot automatically discover namespace-level imports like `api.app`, `providers.provider`, etc. You **must** list them explicitly.

Add the following expanded `hiddenimports` block to the spec file (already present in the repo after the build session):

```python
    hiddenimports=[
        # uvicorn internals
        'uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.loops.asyncio',
        'uvicorn.protocols.http.auto', 'uvicorn.protocols.http.h11_impl',
        'uvicorn.protocols.http.httptools_impl', 'uvicorn.protocols.http.flow_control',
        'uvicorn.protocols.websockets.auto', 'uvicorn.protocols.websockets.websockets_impl',
        'uvicorn.middleware.asgi2', 'uvicorn.middleware.wsgi', 'uvicorn.middleware.proxy_headers',
        # tiktoken
        'tiktoken_ext.openai_public',
        # mcp
        'mcp', 'mcp.server', 'mcp.server.fastmcp',
        # external libs
        'lxml', 'lxml.etree',
        # api
        'api', 'api.app', 'api.routes', 'api.dependencies', 'api.middleware',
        'api.models', 'api.models.anthropic', 'api.models.responses',
        'api.bot_protection', 'api.optimization_handlers', 'api.request_utils',
        'api.command_utils', 'api.detection', 'api.effort_store',
        # config
        'config', 'config.settings', 'config.nim', 'config.logging_config',
        # cli
        'cli', 'cli.manager', 'cli.session', 'cli.process_registry',
        # discord_bot
        'discord_bot', 'discord_bot.bot', 'discord_bot.cog', 'discord_bot.conversation',
        'discord_bot.persistence', 'discord_bot.rate_limit', 'discord_bot.views',
        'discord_bot.user_blocking', 'discord_bot.tools', 'discord_bot.tools.web_search',
        # providers
        'providers', 'providers.provider', 'providers.base', 'providers.text',
        'providers.rate_limit', 'providers.request_queue', 'providers.logging_utils',
        'providers.error_mapping', 'providers.sse_builder', 'providers.header_capture',
        'providers.heuristic_tool_parser', 'providers.request', 'providers.tiktoken_cache',
        'providers.think_parser', 'providers.message_converter', 'providers.exceptions',
        'providers.utils',
        # websearch
        'websearch', 'websearch.duckduckgo_html',
        # pydantic-settings
        'pydantic_settings',
    ],
```

> **Removed from spec:** `primp`, `duckduckgo_search` — these were leftover browser/search imports that are not used by the Linux build and were causing warnings.

### Step 4.6 — Run PyInstaller

```bash
cd /home/cyber/github/NIMbus
export LD_LIBRARY_PATH=/home/cyber/github/NIMbus/python-3.14/lib:$LD_LIBRARY_PATH

/tmp/nimbus_build_venv/bin/pyinstaller --clean --distpath dist_linux nimbus_linux.spec
```

**Expected final lines:**
```
187290 INFO: Build complete! The results are available in: /home/cyber/github/NIMbus/dist_linux
```

> The `“Could not find platform dependent libraries <exec_prefix>”` noise during analysis is **harmless** — it comes from the portable Python build lacking a standard `exec_prefix` layout. It does not affect the final binary.

### Step 4.7 — Verify the binary

```bash
# Size and format
ls -lh dist_linux/nimbus          # → ~58 MB
file dist_linux/nimbus             # → ELF 64-bit LSB executable, ARM aarch64

# Quick smoke tests (set LD_LIBRARY_PATH if python-3.14 lib is not in system linker paths)
export LD_LIBRARY_PATH=/home/cyber/github/NIMbus/python-3.14/lib:$LD_LIBRARY_PATH

# MCP mode
timeout 5 ./dist_linux/nimbus --mcp 2>&1 | head -5
# → "Starting NIMbus MCP Server (stdio transport)..."

# Init wizard (needs interactive TTY; piping "n" tests it briefly)
echo "n" | timeout 3 ./dist_linux/nimbus --init 2>&1 | head -5
```

---

## 5. Spec File Changes

`git diff` of `nimbus_linux.spec` after the build session:

| Before | After |
|--------|-------|
| Short `hiddenimports` list: `uvicorn.*`, `tiktoken_ext.openai_public`, `mcp`, `mcp.server`, `mcp.server.fastmcp`, `duckduckgo_search`, `lxml`, `primp` | Expanded list covering **all** internal packages (`api.*`, `config.*`, `providers.*`, `discord_bot.*`, `cli.*`, `websearch`) plus `pydantic_settings` and `lxml.etree` |
| `excludes` same as before | Unchanged — still excludes heavy libs like `torch`, `grpcio`, `PIL`, etc. |

The `datas` entry (tiktoken cache) was **not** changed — it already pointed to the correct file under `build_resources/tiktoken_cache/`.

---

## 6. Running the Binary

Because the Python build is portable (not installed to `/usr`), the `libpython3.14.so.1.0` library is bundled **inside** the PyInstaller archive but may still be dynamically loaded at startup.

### Working directory
The binary self-extracts to a temp dir then changes CWD to wherever the `.exe` lives. Put your `.env` in the same directory before running.

### Required environment
If you get an error about missing `libpython3.14.so.1.0` when launching, export this **before** running:

```bash
export LD_LIBRARY_PATH=/home/cyber/github/NIMbus/python-3.14/lib:$LD_LIBRARY_PATH
./dist_linux/nimbus          # starts proxy server
./dist_linux/nimbus --mcp     # starts MCP server (stdio)
./dist_linux/nimbus --init    # interactive setup wizard
```

### First run
If no `.env` exists in the binary’s directory, `packaged_entry.py` will:
1. Print a first-run banner.
2. Create a `.env` template from the embedded `_ENV_TEMPLATE` string.
3. Pause for the user to edit it.

---

## 7. Troubleshooting

### `Python was built without a shared library`
**Cause:** You’re running PyInstaller from the system Python or from the project’s `venv/` (which was created from the system Python).  
**Fix:** Use the shared-lib Python to create a **new** temporary venv as shown in Step 4.2.

### `libpython3.14.so.1.0: cannot open shared object file`
**Cause:** LD_LIBRARY_PATH is not set when running the Python binary (or the built executable).  
**Fix:**
```bash
export LD_LIBRARY_PATH=/home/cyber/github/NIMbus/python-3.14/lib:$LD_LIBRARY_PATH
```

### `ModuleNotFoundError: No module named 'mcp.server.fastmcp'` inside the frozen binary
**Cause:** The build venv installed `mcp 2.x`, which reorganized the package layout. The project source was written for `mcp 1.27.2`.  
**Fix:** Pin `mcp==1.27.2` in the build venv and rebuild.

### `ModuleNotFoundError: No module named 'api.app'` (or any internal package)
**Cause:** `hiddenimports` in the `.spec` file is missing that package path.  
**Fix:** Add the missing module string to the `hiddenimports` list, then rebuild with `--clean`.

### `Could not find platform dependent libraries <exec_prefix>`
**Cause:** The portable Python build was compiled without a standard install prefix.  
**Fix:** Harmless warning — ignore it. If it bothers you, suppress it by setting `PYTHONHOME` to the python-3.14 prefix, though it is unnecessary.

---

## 8. What Was Left Behind

| Artifact | Location | Disposition |
|----------|----------|-------------|
| **Final binary** | `dist_linux/nimbus` | **Keep** — 58 MB ELF aarch64 executable |
| **Spec update** | `nimbus_linux.spec` | **Keep or restore** — tracked change with expanded `hiddenimports` |
| **Temp build venv** | `/tmp/nimbus_build_venv` | **Deleted** after build to save space |
| **Build cache** | `build/nimbus_linux/` | Can be deleted with `pyinstaller --clean` |
| **Warnings log** | `build/nimbus_linux/warn-nimbus_linux.txt` | Optional reading if you want to audit missing modules |
| **Old failed jobs** | `bash-1`, `bash-2` (background) | Already failed; cleaned up by the shell |

---

## Quick Reference: One-Shot Build Command

If all prerequisites (shared-lib Python, tiktoken cache, spec updated) are already in place:

```bash
cd /home/cyber/github/NIMbus
export LD_LIBRARY_PATH=/home/cyber/github/NIMbus/python-3.14/lib:$LD_LIBRARY_PATH
python3.14 -m venv /tmp/nimbus_build_venv
/tmp/nimbus_build_venv/bin/pip install pyinstaller fastapi uvicorn httpx pydantic python-dotenv tiktoken discord.py pydantic-settings openai loguru "mcp==1.27.2" lxml
/tmp/nimbus_build_venv/bin/python build_exe_download_tiktoken.py
/tmp/nimbus_build_venv/bin/pyinstaller --clean --distpath dist_linux nimbus_linux.spec
rm -rf /tmp/nimbus_build_venv
```

---

*End of document.*
