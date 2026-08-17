# NIMbus Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---
## v2.0.14 - Date: 2026-08-16

### Fixed
- **Thinking block initialization in streaming mode** - Fixed an issue where thinking blocks were not being started at the beginning of streaming responses when `NIM_THINKING` was enabled, causing thinking content to only appear after the stream completed. The fix correctly initializes a thinking block at the start of streaming when thinking is enabled and no other block has been started yet, allowing thinking to stream incrementally in Claude Code.
- **Setup wizard improvements**:
  - Added prompt for resource_exhausted retries configuration in the setup wizard.
  - Fixed .env writing to use user-selected values for resource_exhausted_retries, nim_reasoning_budget, nim_enable_thinking, show_nim_reply, and other settings instead of hardcoded defaults.


### Files Touched
- **`providers/provider.py`** - Added import of `get_settings` from `config.settings` and corrected thinking block initialization to use `settings.nim.thinking` and `sse.blocks`.
- **setup_wizard.py** - Added prompt for resource_exhausted retries configuration and fixed .env writing to use user-selected values instead of hardcoded defaults.

---
## v2.0.13 - Date: 2026-07-20

### Added

#### Token Limit Enforcement & Reasoning Budget Validation
- **NIM_MAX_TOKENS default reduced** from 202000 to 32000 for more reasonable token limits
- **Token upgrade logic** — When `NIM_MAX_TOKENS` is set higher than what Claude sends (e.g., 32000 vs 256000), the proxy now **upgrades** the request's `max_tokens` to the configured value instead of capping down
- **Reasoning budget validation** — If `reasoning_budget >= max_tokens` (after upgrade), sets `reasoning_budget=-1` (unlimited) and relies on `max_tokens` as the effective cap to prevent NVIDIA rejection
- **Explicit logging** — Logs both token upgrades and budget adjustments for audit trail

#### Empty NIM Stream Handling & Provider Resilience
- **Empty stream detection** — NvidiaNimProvider now detects empty/malformed NVIDIA NIM streaming responses (stream finishes with no `finish_reason` and no content: text, reasoning, or tool states) and raises `StreamTruncatedError` to trigger retry logic
- **Request queue attribute on BaseProvider** — Added default `_request_queue = None` on `BaseProvider` to enable queue stats in `api/routes.queue_status` with proper typing
- **Rate limit slot release on pre-response failures** — Buffered requests that fail before reaching the server now properly release rate-limit slots via `release_last_slot()`
- **Expanded retryable errors** — `execute_with_rate_limit` now treats `httpx.TimeoutException`, `APIConnectionError`, `APITimeoutError`, and 5xx server errors as retryable; releases slots, logs, and applies exponential backoff

#### Discord Bot Stability & DM Handling
- **DM/channel guards** — Added defensive checks for `interaction.channel` being `None` or not a messageable channel; returns early instead of crashing
- **Missing channel/category guards** — In `discord_bot/views.py`, validates resolved category is a `discord.CategoryChannel` before use; returns clear ephemeral error if not
- **Fallback channel/guild names** — Backup/backup commands now use `getattr(channel, 'name', 'dm')` and `getattr(channel.guild, 'name', 'Unknown Guild')` for DM-friendly filenames and messages
- **Conversation wrapper ergonomics** — `add_message` now calls `add_message_with_user` with explicit keyword arguments for `tools_used` and `tool_results`

#### Type Safety, Null Checks & Pylance Compliance
- Added type: ignore comments for mypy/pyright compliance (sys._MEIPASS, termios, attribute access on unions)
- Defensive null checks for `model_name`, `detail`, `extracted`, `new_budget` variables
- Fixed bugs: removed malformed duplicate code in `_rebuild_without_key()`, corrected `_has_thinking_params()` removal
- Simplified tiktoken cache logic by removing fallback handling
- Fixed priority enum casting in `request_queue.py` (`RequestPriority(priority)` → `RequestPriority(priority)`)
- Fixed pyright directive in `config/nim.py` (changed to `# pyright: ignore[reportCallIssue]`)

#### Discord Bot & Views Resilience
- **CategoryChannel validation** — `CreateChannelModal` and `list_channels` button now verify `interaction.guild.get_channel()` returns a `CategoryChannel` before use
- **DM/guild guards** — Commands requiring guild channels now explicitly check `interaction.guild` first and return early for DMs
- `Member` vs `User` typing — `CompactConfirmView` and related classes use `discord.abc.User` to accept both `User` and `Member`

#### Pylance/Type Checking Fixes
- Fixed pyright directive in `config/nim.py` (invalid `disable=` syntax → `ignore[...]`)
- Added `# type: ignore[union-attr]` and `# type: ignore[attr-defined]` suppressions for union attribute access patterns
- Added `cast(Literal["user", "assistant", "system"], ...)` for message role literal types
- Fixed `reportIndexIssue` for `Message` indexing in `bot.py`

#### Discord Bot DM/Channel Safety
- All command handlers now guard with `if not interaction.guild: return` before using `interaction.channel`
- `interaction.channel` accesses wrapped with `if channel is None: return`
- Channel name access uses `getattr(channel, 'name', 'dm')` for DM friendliness
- `isinstance(category, discord.CategoryChannel)` check before using as category

#### Conversation API Ergonomics
- `add_message` now calls `add_message_with_user` with keyword arguments for `tools_used` and `tool_results` instead of positional

### Changed

#### NIM_MAX_TOKENS Behavior
- **Default reduced** from 202000 to 32000 in code and `.env.example`
- **Upgrade logic added** — if request `max_tokens` < `NIM_MAX_TOKENS` env value, proxy upgrades to env value (log: "max_tokens X upgraded to NIM_MAX_TOKENS: Y")
- Removed fallback to `nim.max_tokens` when request doesn't provide `max_tokens`; lets NIM model use its own defaults

#### .env.example Reorganization
- Full restructure with section headers (CORE SERVER, NVIDIA NIM, PROVIDER RATE LIMITING, REQUEST QUEUE, MCP, DISCORD BOT, COMMAND TOGGLES)
- Removed duplicated entries and improved examples/comments
- `NIM_MAX_TOKENS=32000` default shown

#### Rate Limiter Improvements
- **Method renamed** — `reset_adaptive_backoff()` → `reset_reactive_block()`
- **UI messaging** — "Resets in" → "Next Slot free in" for clarity
- **New `release_last_slot()`** — Explicit slot release for pre-response failures
- **Retriable error expansion** — Now handles `httpx.TimeoutException`, `openai.APIConnectionError`, `openai.APITimeoutError`, 5xx; releases slots, logs, applies exponential backoff
- **`cast(MessagesResponse, response)`** — Type-safe casting in optimization handler

#### Discord Bot DM/Channel Safety
- All command handlers now guard with `if not interaction.guild: return` before using `interaction.channel`
- `interaction.channel` accesses wrapped with `if channel is None: return`
- Channel name access uses `getattr(channel, 'name', 'dm')` for DM friendliness
- `isinstance(category, discord.CategoryChannel)` check before using as category

#### Conversation API Ergonomics
- `add_message` now calls `add_message_with_user` with keyword arguments for `tools_used` and `tool_results` instead of positional

### Fixed

- Various Pylance/pyright type checking issues across codebase
- Rate limit status display: "Resets in" → "Next Slot free in" for clarity
- `config/nim.py` pyright directive syntax (invalid `disable=` → `ignore[...]`)
- Empty/malformed NIM stream handling (no `finish_reason`, no content → retry)
- `BaseProvider` missing `_request_queue` attribute
- Discord DM crashes (missing channel/category/category type)
- Rate limit slot leaks on pre-response failures
- `Conversation.add_message` positional arg mismatch for `tools_used`/`tool_results`
- Priority enum casting in `request_queue.py` (`RequestPriority(priority)` → `RequestPriority(priority)`)

### Removed

- Fallback to `nim.max_tokens` when request doesn't provide `max_tokens` (lets NIM use its own defaults)

### Files Touched

- **`.env.example`** — Full restructure with clarified defaults, removed duplicates, improved comments; `NIM_MAX_TOKENS=32000` default
- **`config/nim.py`** — `max_tokens` default from 202000 → 32000; pyright directive fix
- **`providers/request.py`** — Token upgrade logic, reasoning budget validation, logging
- **`api/app.py`** — Rate limit status message update
- **`api/routes.py`** — Queue status endpoint with `_request_queue` typing comment; DM/guild guards
- **`api/optimization_handlers.py`** — Type-safe casting
- **`providers/request.py`** — Token upgrade logic, reasoning budget validation, logging
- **`providers/request_queue.py`** — Priority enum casting fix
- **`providers/rate_limit.py`** — `release_last_slot()`, expanded retryable errors, `reset_reactive_block()`, exponential backoff
- **`providers/provider.py`** — Empty stream detection, `StreamTruncatedError`, rate limit slot release, `BaseProvider._request_queue`
- **`providers/base.py`** — `_request_queue = None`, `_client = None`
- **`discord_bot/bot.py`** — Type ignores for union attributes, DM message guards, `getattr` for channel names
- **`discord_bot/cog.py`** — `getattr` for channel/guild names, `isinstance` CategoryChannel checks, `interaction.channel` None guards, `discord.abc.User` typing, keyword args in `add_message`
- **`discord_bot/views.py`** — CategoryChannel validation, `client.settings` type ignores
- **`discord_bot/conversation.py`** — Keyword args for `add_message_with_user`
- **`config/nim.py`** — Pyright directive fix
- **`config/settings.py`** — (minor)
- **`providers/base.py`** — `_request_queue = None`, `_client = None`
- **`providers/request.py`** — Token upgrade, budget validation
- **`providers/rate_limit.py`** — `release_last_slot()`, retryable error expansion, `reset_reactive_block()`, backoff
- **`packaged_entry.py`** - (typing)
- **`api/optimization_handlers.py`** — `cast(MessagesResponse, response)`
- **`tests/providers/test_nvidia_nim_request.py`** — Updated tests for upgrade behavior
- **`tests/providers/test_nvidia_nim.py`** — Updated test expectations
- **`tests/api/test_dependencies.py`** — Mock settings fix

---

## v2.0.12 - Date: 2026-07-18

### Added

#### Rate Limiting (Adaptive Cap on 429)
- **Local adaptive RPM cap** - On 429, caps effective RPM to the current sliding-window rate (actual throughput). After `NIM_RPM_RESET` (default **`NIM_RPM_RESET=5`**) successful requests, restores +1 RPM incrementally until reaching initial RPM.
- **Progressive decrement at cap** - If 429 occurs again while capped, decrements RPM by 1 (floor = 1). Recovery follows same gradual restore.
- **Server-header-driven proactive limit** - `x-ratelimit-limit` updates current RPM when not locally capped; `x-ratelimit-reset` updates rate window.
- **Reactive block from `retry-after`** - 429 responses trigger blocking via `retry-after` header; `<nimrpm:reset>` clears block and cap immediately.
- **Auto-restore on success streak** - Success counter resets on 429; after N successes without 429, recovers 1 RPM (when capped) or clears cap (when recovered).

#### Thinking / Effort Fallback for Nemotron Models
- **Effort-flag fallback** - Models that accept `enable_thinking` but reject `*_effort` flags (low/medium/high_effort) now auto-retry with effort flags stripped, keeping `enable_thinking=true`. Model added to runtime cache on first error.
- **Reasoning_budget handling** - `reasoning_budget` errors detected and handled via existing thinking-param fallback path.
- **Log buffering for streaming** - `THINKING`/`REPLY` log lines buffered (120 chars) to reduce log spam under high throughput; flushed on finish_reason or stream end.

#### Inline Commands: Session ID for Effort Tracking
- **Per-session effort via `x-claude-code-session-id`** - `<nimeffort:level>` and `<nimeffort>` now key off the `x-claude-code-session-id` header (falls back to API key). Fixes effort persistence across API key rotations.

### Changed

#### Rate Limiter Redesign (`providers/rate_limit.py`)
- **Removed legacy adaptive params** - `NIM_RPM_INITIAL`, `NIM_RPM_DROP`, `NIM_RPM_MIN`, `NIM_RPM_HOLD_INITIAL`, `NIM_RPM_HOLD_MAX` deleted from code, config, `.env.example`, wizard.
- **`NIM_RPM_RESET` semantics changed** - Was: auto-restore after N *seconds* without 429 (default 300s). Now: auto-restore after N *successful requests* without 429 (default **`NIM_RPM_RESET=5`**).
- **Single RPM tracker** - `_current_rpm` replaces `_effective_rpm` + `_initial_rpm` + `_hold_delay` + `_drop_count`. Source: server header or fallback.
- **New state** - `_capped_rpm` (int | None) tracks local adaptive cap; `_success_count` tracks consecutive non-429 requests.
- **API changes** - `on_rate_limit_hit()` → `on_rate_limited()`, `reset_adaptive_backoff()` → `reset_reactive_block()`, added `on_success()`, `current_rpm()` property.
- **`get_status()` output changed** - Now returns `current`, `max`, `initial_max`, `remaining`, `reset_in_seconds`, `is_blocked`, `blocked_seconds_remaining`, `worker_limit`, `worker_available`, `capped_rpm`, `initial_rpm`. Removed `effective_rpm`, `hold_delay`, `drop_count`.

#### Environment & Config
- **New `HOST` variable** - **`HOST=0.0.0.0`** (bind address) added to `.env.example`.
- **`NIM_ENABLE_THINKING=true`** - Global thinking toggle added to `.env.example` (read directly by provider, not via Pydantic settings).
- **`FABLE_OVERRIDE=""`** - Uncommented in `.env.example` (was commented).
- **`NIM_RPM_RESET=5`** - Default changed from 300 (seconds) to 5 (requests).
- **Direct env vars section** - Added `TIKTOKEN_CACHE_DIR`, `CLAUDE_CONFIG_DIR`, `XDG_CONFIG_HOME`, `UV_PROJECT_ENVIRONMENT`, **`WEB_SEARCH_DEBUG=false`**, `PLAYWRIGHT_BROWSERS_PATH` to `.env.example` (read directly by components, not via settings).
- **Optimization flags use `Field(validation_alias=...)`** - `fast_prefix_detection`, `enable_network_probe_mock`, `enable_title_generation_skip`, `enable_suggestion_mode_skip`, `enable_filepath_extraction_mock`, `enable_recap_skip` now explicitly declare env aliases.

#### Model Override & Request Building
- **`model_override` passed to `build_request_body()`** - Ensures thinking params match the swapped model, not the original Claude tier model.
- **Session ID from request header** - `_handle_nimeffort` and `_handle_nimeffort_status` use `request_data.session_id` (from `x-claude-code-session-id`) instead of API key.

#### Documentation Updates (README.md)
- **Adaptive rate limiting section rewritten** - Now describes server-header-driven proactive limit + reactive retry-after block + auto-restore on success streak.
- **Env var table updated** - Removed legacy `NIM_RPM_*` vars; added `NIM_RPM_RESET`, `PROVIDER_RATE_LIMIT`, `PROVIDER_RATE_WINDOW`.
- **`<nimrpm:reset>` description updated** - "Clear reactive rate limit block immediately (clears retry-after block)".

### Fixed
- **Effort tracking tied to API key** - Fixed by using `x-claude-code-session-id` header for per-session effort storage (commit `a485eee`).
- **Model override ignored thinking params** - `model_override` now passed to `build_request_body()` so swapped models get correct thinking config (commit `df7c5cc`).
- **Rate limiter used hardcoded defaults** - Fixed by removing legacy params; now reads only `PROVIDER_RATE_LIMIT`, `PROVIDER_RATE_WINDOW`, `NIM_RPM_RESET` from settings (commit `15ed3ed`).
- **Stale rate limit on header updates** - `x-ratelimit-limit` now updates `_current_rpm` only when not locally capped; `x-ratelimit-reset` updates window dynamically (commit `236d67f`).

### Removed
- **Legacy adaptive rate limit env vars** - `NIM_RPM_INITIAL`, `NIM_RPM_DROP`, `NIM_RPM_MIN`, `NIM_RPM_HOLD_INITIAL`, `NIM_RPM_HOLD_MAX` removed from codebase, `.env.example`, settings, wizard, README.
- **ProviderConfig legacy fields** - `rpm_drop`, `rpm_min`, `hold_initial`, `hold_max` removed from `providers/base.py`.
- **Settings legacy fields** - `nim_rpm_initial`, `nim_rpm_drop`, `nim_rpm_min`, `nim_rpm_hold_initial`, `nim_rpm_hold_max` removed from `config/settings.py`.
- **Test-compat `_rate_limit` property** - Kept temporarily in `rate_limit.py` for test compatibility (deprecated).

### Files Touched

#### Core Rate Limiting
- `providers/rate_limit.py` - Complete rewrite: adaptive cap, success-streak restore, header-driven limits, reactive retry-after block
- `providers/base.py` - Removed legacy `ProviderConfig` fields; `rpm_reset` now `int` (requests)
- `config/settings.py` - Removed legacy `nim_rpm_*` fields; added `HOST`, `NIM_ENABLE_THINKING`; optimization flags use `Field(validation_alias=...)`
- `api/app.py`, `api/dependencies.py` - Removed legacy params from `GlobalRateLimiter.get_instance()` calls
- `setup_wizard.py` - Removed legacy RPM prompts; added `NIM_RPM_RESET` (requests) prompt
- `.env.example` - Added `HOST`, `NIM_ENABLE_THINKING`, `FABLE_OVERRIDE`, `NIM_RPM_RESET`; removed legacy vars; added direct env vars section
- `README.md` - Rewrote adaptive rate limiting section; updated env var table; updated `<nimrpm:reset>` description

#### Provider & Request Building
- `providers/provider.py` - Effort-flag fallback cache/logic; `_rebuild_without_effort_flags()`; log buffering (120 chars); `on_success()`/`on_rate_limited()` calls in stream/buffered paths
- `providers/request.py` - `model_override` passed to `build_request_body()`
- `providers/thinking_effort_unsupported_cache` (module-level cache added in provider.py)

#### Inline Commands / Sessions
- `api/routes.py` - `_handle_nimeffort`, `_handle_nimeffort_status` use `request_data.session_id` (header `x-claude-code-session-id`) with API key fallback; removed blank lines
- `api/models/anthropic.py` - `session_id` field on request model (already existed, now used)

#### Config & Documentation
- `reasoning_config.json` - Added `reasoning_budget` entries (diff shows +12 lines)

---

## v2.0.10 - Date: 2026-07-11

### Fixed
- **Expired/Invalid API Keys** - 403 Forbidden errors now show a clear message with a direct link to verify the NVIDIA API key: `"Invalid or expired API key. Verify your key at https://build.nvidia.com/settings/api-keys"`
- **<nimrpm:reset> False Positives** - The adaptive rate limit reset tag now only triggers when it's the entire message content (optional whitespace allowed), preventing false positives from embedded content in file outputs, system prompts, or documentation
- **Thinking Parameter Compatibility** - Models that don't support thinking/reasoning parameters (e.g., `z-ai/glm-5.2`) now automatically retry without thinking parameters on 500 errors; the model is cached as unsupported to skip thinking params on subsequent requests
- **Log Rotation on Windows** - Fixed `PermissionError: [WinError 32] The process cannot access the file because it is being used by another process` during log rotation by switching to timestamped log files per session (no mid-session rotation). Each server start creates a new `server.YYYY-MM-DD_HH-MM-SS_XXXXXX.log` file; no `server.log` file is created anymore.
- **Windows Log Rotation TypeError** - Fixed `TypeError: argument should be a str or os.PathLike object, not 'TextIOWrapper'` during log rotation by removing custom rotator that received file object instead of path

### Added
- **Fable Model Support (FABLE Model Override)** - New `FABLE_OVERRIDE` environment variable to configure the NIM model for the new Claude "Fable" model tier. Defaults to the Opus NIM model but can be overridden with any NIM model ID (supports `[1m]` context suffix like other models)

### Changed
- **execute_with_retry Retry Logic** - `max_retries=0` now correctly means "infinite retries" (previously was treated as 1 retry); useful for critical background operations that should never give up
- **Logging Configuration** - Moved logging initialization to entry points (`server.py`, `packaged_entry.py`, `start_server.py`) before importing `api.app` so log file path is set correctly for each run

### Files Touched
- `api/swapper/parser.py` - Fixed `<nimrpm:reset>` regex to require exact message match
- `config/logging_config.py` - Timestamped log files per session, no rotation during runtime; removed custom rotator
- `config/settings.py` - Added Fable model detection in `get_model_for_claude()`; added FABLE_OVERRIDE field with `[1m]` suffix stripping
- `providers/error_mapping.py` - Added PermissionDeniedError handling with NVIDIA key URL
- `providers/provider.py` - Added thinking parameter fallback on 500 errors; model caching for unsupported models
- `providers/rate_limit.py` - Fixed `max_retries=0` to mean infinite retries
- `server.py` - Configure logging at entry point before importing app
- `.env.example` - Added FABLE_OVERRIDE documentation and example

---

## v2.0.9 - Date: 2026-06-28

### Added
- **Portable Build Browser Bundling** - Playwright Chromium pre-downloaded at build time and embedded via `--add-data` into single-file exe (~25 MB); no external `ms-playwright` folder needed; runs on any Windows machine without `playwright install`
- **Discord Bot Role Mention Support** - Bot now responds when mentioned via role (`@&role_id`) in addition to direct user mentions; checks if any mentioned role is assigned to the bot

### Fixed
- **MCP Server Headless Mode** - `start_server.py` now loads `.env` before starting MCP server, so `MCP_BROWSER_HEADLESS=false` and `DISCORD_BROWSER_HEADLESS=false` are properly applied
- **Tool Choice Conversion** - Anthropic `tool_choice` (any/auto/none/tool) correctly converted to OpenAI format in `message_converter.py`
- **Streaming 429 Retry Logic** - Streaming requests now honor `PROVIDER_RETRY_ON_TRUNCATION` setting (was hardcoded to 3 retries). Buffered mode already respected this setting.
- **Swapper Model Validation Resilience** - Added exponential backoff retry logic to model swapper validation (`api/swapper/validator.py`). Transient failures (rate limits, network errors, 5xx) during model catalog fetch or test request are now retried up to 3 times before reporting failure.
- **Error Mapping** - Updated httpx error handling for better error messages.
- **MCP Server Entry Point** - `server.py --mcp` now correctly starts MCP server (stdio transport) instead of proxy server.

### Changed
- **Single-File Portable Build** - Browsers extracted to `_MEIPASS` temp dir at runtime via updated `runtime-playwright.py` hook; source runs use system Playwright install
- **DuckDuckGo Search Robustness** - Uses only `lite.duckduckgo.com` endpoint via Playwright for reliable results; removes fragile HTML endpoint parsing
- **Debug Logging** - Final buffered response logged for Discord web search troubleshooting
- **Documentation** - Fixed Request Queue version reference in README (was v2.1, now correctly v2.0.7).

### Files Touched
- `build_exe.bat` - Pre-download browsers, copy to build_resources, bundle via --add-data
- `build_hooks/runtime-playwright.py` - Use _MEIPASS for embedded browsers
- `discord_bot/bot.py` - Role mention detection, debug logging
- `websearch/duckduckgo_html.py` - Source vs portable browser path logic, error handling
- `start_server.py` - Load .env before MCP server launch
- `providers/message_converter.py` - tool_choice conversion logic
- `requirements.txt` - Added playwright, playwright-stealth, pywin32, dotenv
- `nimbus.spec` - Full PyInstaller spec with browser datas and hidden imports
- `providers/provider.py` - Pass `max_retries=config.retry_on_truncation` to streaming `execute_with_retry` calls
- `api/swapper/validator.py` - Added `_execute_with_retry` helper with exponential backoff for catalog validation and model test requests
- `providers/error_mapping.py` - Updated httpx error handling
- `server.py` - Handle `--mcp`/`--mcpwebsearch` flag to start MCP server

---

## v2.0.8 - Date: 2026-06-27

### Added
- **Playwright-Backed DuckDuckGo Search** - Web search now uses Playwright with stealth mode, persistent storage state (cookies), multi-endpoint fallback (html + lite), and auto-detection of result format
- **DuckDuckGo Lite Parser** - Robust parsing for `lite.duckduckgo.com` table-based results with URL cleaning (removes DDG redirect wrapper)
- **DuckDuckGo Pagination Resilience** - Failed pagination pages (e.g., rate limit 202) stop gracefully while preserving collected results
- **Discord Web Search Failure Tracking** - Consecutive `web_search` failures (2+) automatically disable the tool, keeping only `fetch_page` available
- **Final Buffered Fallback** - When tool calls produce no text after max iterations, sends final buffered request without tools to force text response
- **Tool Use Logging** - Detects and logs `tool_use` start events with name and ID
- **Tool Input/Result Robustness** - Fixed tool input parsing (handles string vs dict), tool result construction with proper error messages

### Fixed
- **Pylance Type-Checker Errors** - Resolved across 11 files: added missing imports (Path, Callable, Awaitable, TypeVar), fixed coro_factory type hints, corrected keyword-only arg calls, added RuntimeError guard for uninitialized queue, aligned abstract method signatures, fixed return type annotations for modelswap/nimserver, fixed Message construction with union types, added meta None guards, added isinstance checks for CategoryChannel, validated reasoning_effort env var values, fixed _generate_env_alias to always return str
- **extract_text_from_content** - Now robust to dicts, objects with .text attribute, and strings

### Changed
- **FETCH_PAGE Tool Schema** - Added offset/limit/chunked reading docs, search param capped at 5 matches with 200 char context
- **Request Queue Priority** - Added `priority` parameter to `stream_response` and `buffered_request` abstract methods in `BaseProvider`
- **System Role Fallback Logging** - Clearer warning when model rejects system role
- **Model Warmup at Startup** - NVIDIA model cache pre-warmed in `api/app.py` lifespan to avoid first-request latency
- **Shared tiktoken Cache** - New `providers/tiktoken_cache.py` eliminates 3 redundant encoder loads across sse_builder, request_utils, conversation
- **Auto-Generated Validation Aliases** - `alias_generator` in Settings eliminates ~60 manual validation_alias fields
- **Shared Retry Logic** - `_execute_with_retry` helper in provider.py consolidates buffered/streaming retry logic

### Files Touched
- `build_hooks/runtime-playwright.py` - New: PyInstaller runtime hook for browser path
- `discord_bot/bot.py` - Web search robustness, failure tracking, final fallback, tool logging
- `discord_bot/tools/web_search.py` - FETCH_PAGE schema, match/context limits, error messages
- `providers/text.py` - extract_text_from_content handles dicts
- `websearch/duckduckgo_html.py` - Complete rewrite: Playwright, persistent context, stealth, multi-endpoint, pagination, lite parser
- `providers/tiktoken_cache.py` - New: shared encoder cache
- `providers/provider.py` - Shared retry logic, type fixes, queue initialization guard
- `providers/base.py` - Added priority param to abstract methods
- `config/settings.py` - alias_generator, startup_model_cache
- `api/app.py` - NVIDIA model cache warmup
- `api/optimization_handlers.py` - stop_reason fallback
- `api/routes.py` - Return type annotations
- `config/nim.py` - reasoning_effort validation
- `discord_bot/cog.py` - discord_model fallback, bot user check
- `discord_bot/conversation.py` - tools_used type fix
- `discord_bot/rate_limit.py` - (user_id, channel_id) tuple keys
- `discord_bot/views.py` - CategoryChannel isinstance check
- `mcp_server.py` - meta None guard

---

## v2.0.7 - Date: 2026-06-26

### Added
- **Built-in Request Queue** - New priority-based request queue to prevent NVIDIA NIM "Worker local total request limit reached (33/32)" errors. Handles up to 32 concurrent requests (configurable) with FIFO ordering, priority lanes (HIGH for Discord, NORMAL for API), and background workers.
- **Two-Phase Timeout** - Queue timeout split into: (1) wait for worker pickup (configurable, default 300s), then (2) wait indefinitely for processing to complete. Prevents cancelling requests that have already started long-running operations (streaming, retries).
- **NVIDIA Worker Slot Accounting** - Track per-worker concurrent request limits (32) via `worker_limit` and `worker_available` fields. Buffered and streaming requests now acquire worker slots for their full lifecycle.
- **Resource Exhaustion Retry** - Detect NVIDIA "ResourceExhausted" (429/rate limit) responses and automatically participate in retry logic with exponential backoff.
- **Worker Status Logging** - Periodic logging of worker slot utilization (current/available/limit) at DEBUG level; included in `/status` endpoint.
- **Setup Wizard Queue Configuration** - Interactive wizard now prompts for request queue settings (enabled, max concurrent, max size, timeout, workers, Discord/API priority).
- **User Reasoning Effort Mapping** - New `user_reasoning_effort_mapping.json` for custom effort level mappings per user.
- **Adaptive Rate Limiting** - Runtime NVIDIA RPM backoff: `NIM_RPM_INITIAL` (starting RPM), `NIM_RPM_DROP` (RPM drop per 429), `NIM_RPM_MIN` (floor RPM), `NIM_RPM_HOLD_INITIAL`/`_MAX` (hold delays). Reset with `<nimrpm:reset>` chat tag.
- **HTTP Client Timeouts** - Configurable `HTTP_READ_TIMEOUT`, `HTTP_WRITE_TIMEOUT`, `HTTP_CONNECT_TIMEOUT` for provider API requests.
- **NIM Core Settings** - `NIM_MAX_TOKENS`, `NIM_REASONING_EFFORT` (low/medium/high), `NIM_REASONING_EFFORT_MAPPINGS` (JSON mapping for effort levels).
- **Model Swapper** - Enable dynamic model switching via `SWAPPER_ENABLED` with `SWAPPER_TEST_PROMPT` and `SWAPPER_TEST_TIMEOUT` for validation.
- **MCP Cache TTL** - `MCP_CACHE_TTL` for MCP server web page fetch caching (default 600s, max 3600, 0 = disabled).
- **Discord Web Search** - `DISCORD_ENABLE_WEB_SEARCH`, `DISCORD_WEB_SEARCH_MAX_RESULTS`, `DISCORD_WEB_SEARCH_MAX_ITERATIONS` (enabled by default).
- **Fast Prefix Detection** - `FAST_PREFIX_DETECTION` optimization (enabled by default).
- **Complete Setup Wizard Coverage** - New wizard sections: Provider Rate Limiting & Adaptive, HTTP Timeouts, NIM Core Settings, Model Swapper, MCP Cache, Discord Web Search. Update mode includes all sections. All new .env keys written through full and update wizard flows.

### Fixed
- **Retry Logic Improvements** - Broader exception handling (includes `APIError`); better handling of model capability errors (system-role rejection, thinking-parameter rejection) by rebuilding requests and retrying.
- **Exception Tuple Bug** - Fixed `except` tuple for `JSONDecodeError`/`OSError` which was not catching both exceptions.
- **Streaming Error Emission** - Improved SSE error event emission on terminal failures.
- **Queue Timeout Stats** - Properly increment timeout stats when future is done but timed out waiting for `processing_started`.

### Changed
- **Default Model** - Switched from `deepseek-ai/deepseek-v4-flash` to `nvidia/nemotron-3-super-120b-a12b` in `.env.example` and setup wizard.
- **Discord Web Search Enabled by Default** - Added `DISCORD_ENABLE_WEB_SEARCH=true`, `DISCORD_WEB_SEARCH_MAX_RESULTS=10`, `DISCORD_WEB_SEARCH_MAX_ITERATIONS=10`.
- **Discord Reply Integration** - Bot now uses native Discord `message.reply()` for responses; captures replied-to bot message content when users use Discord reply feature; responds to replies to bot messages even when `require_mention` is enabled.
- **Conversation History** - User messages now saved unconditionally in `on_message` event (not just when bot replies), preventing missing history.
- **Discord Message Processing Retry** - Added up to 3 retries with exponential backoff (1s base) for transient failures when processing messages from the queue.
- **Web Search Pagination** - DuckDuckGo HTML search now supports multi-page results with deduping across pages.
- **Request Payload Validation** - Force non-empty content in requests (required for Nemotron tool calls).

### Files Touched
- `providers/request_queue.py` - New file: priority queue implementation with worker pool
- `providers/rate_limit.py` - Worker slot tracking, adaptive backoff, status logging
- `providers/provider.py` - Worker slot acquisition, retry logic, error handling
- `providers/base.py` - New `worker_limit` / `worker_available` properties
- `config/settings.py` - Queue settings, Discord web search settings, adaptive rate limiting, HTTP timeouts, NIM settings, model swapper, MCP cache
- `api/routes.py` - Queue stats endpoint (`/queue/status`), priority handling
- `api/dependencies.py` - Provider accessor for queue
- `discord_bot/bot.py` - Reply handling, web search integration, retry logic, channel checks
- `discord_bot/cog.py` - Reply support, command handling
- `discord_bot/conversation.py` - Unconditional message saving, reply_message field
- `discord_bot/persistence.py` - Reply serialization
- `discord_bot/tools/web_search.py` - Web search + fetch_page tools with caching
- `setup_wizard.py` - Queue config prompts, web search config, adaptive rate limiting, HTTP timeouts, NIM settings, model swapper, MCP cache, Discord web search
- `websearch/duckduckgo_html.py` - Pagination, deduping, offset-aware fetch
- `providers/request.py` - Non-empty content enforcement
- `user_reasoning_effort_mapping.json` - New: per-user effort mappings

---

## v2.0.7 - Date: 2026-06-23

### Added
- **DISCORD_MODEL environment variable**  -  Configure a separate model for the Discord bot independently from the main API proxy's MODEL setting. Resolution order: DISCORD_MODEL > MODEL > windows:settings.json (Opus > Sonnet > Haiku from Claude settings). If no model can be resolved, the Discord bot will skip startup.

### Changed
- Discord bot now uses `settings.discord_model` instead of `model_name` for all requests
- Discord bot logs warning and skips startup if no model can be resolved (prevents running with no model)
- Setup wizard now prompts for Discord Model in both full wizard and update modes

### Files Touched
- `config/settings.py`  -  added `discord_model_raw` field and `discord_model` property
- `discord_bot/bot.py`  -  uses `discord_model`, skips startup if None
- `discord_bot/cog.py`  -  uses `discord_model` for all requests
- `setup_wizard.py`  -  prompts for Discord Model in full wizard and update modes
- `.env.example`  -  added DISCORD_MODEL documentation
- `README.md`  -  added DISCORD_MODEL to config table and features list

---

## v2.0.6 - Date: 2026-06-20

Discord-focused release fixing interaction timeouts, mass-ping risks, and typing indicator rate limits.

### Fixed
- **Discord typing cooldown per channel**  -  Added `_typing_locks` dictionary to track last typing indicator trigger per channel; only shows typing if ≥5 seconds have passed since last trigger, preventing Discord 429 rate limit errors. Core streaming, global rate limiting, and conversation storage logic no longer nested inside typing context.
- **Accidental mass pings**  -  `@everyone` / `@here` mentions sanitized with zero-width spaces in user input, bot output, and stored conversation history. Removed FIFO message queue for immediate async processing per message.
- **Prefix command support & explicit bot enable toggle**  -  Added configurable text prefix (default `!!`) for `ask`, `compact`, `new`, `status` commands alongside slash commands. Added `DISCORD_ENABLED` toggle to explicitly disable bot regardless of token/guild configuration.

### Changed
- **Discord Adjustments**  -  Slash commands (`/compact`, `/new`, `/status`) now use `interaction.response.defer()` + `followup.send()` to avoid 3-second interaction timeout. Conversation history now only prefixes username for user messages (not assistant), preventing model from learning to prefix its own responses with `NIM:`.

---

## v2.0.5 - Date: 2026-06-16

### Added
- **Discord Bot Configuration in Setup Wizard**  -  The interactive setup wizard (`nimbus.exe --init`) now includes Discord bot configuration options:
  - Prompts for: Bot Token, Guild ID, Control Channel ID, Conversation Category ID, Owner ID, Owner-only mode, Auto-compact setting
  - Added Discord settings to update mode for partial configuration updates
  - All Discord env vars now written to `.env` automatically

---

## v2.0.4 - Date: 2026-06-15

### Added
- **Dynamic Model Swapping**  -  Switch between configured models during chat sessions using `<modelswap:model-name>` tag. Short names resolved against NVIDIA's live catalog; test prompt validates swap-in.
- **Model Similarity Suggestions**  -  When model validation fails, suggests similar models from the same organization.

### Improved
- **Retry on 5xx server errors** (500, 502, 503, 504)
- Separate retry logic for connection/timeout vs server errors
- Non-retryable errors (4xx) raised immediately without exhausting retries
- `UV_PROJECT_ENVIRONMENT` now set at runtime in `start_server.py`

---

## v2.0.3 - Date: 2026-06-13

### Fixed
- **Disabled Suggestion Mode Skip and Recap Skip Optimizations**  -  Both were causing false positives. Suggestion detection was too broad and recap skip blocked legitimate requests.
- Optimization responses now returned as proper SSE events on streaming endpoint

### Changed
- `README.md` updated with correct defaults for disabled optimizations
- `setup_wizard.py` defaults disabled optimizations to `false`
- Added `dist_linux/`, `python-3.14/`, `python-build/` to `.gitignore`
- **Native DuckDuckGo HTML search** with clean formatting (replaced duckduckgo-search dependency)
- **MCP configuration step** and partial update mode added to setup wizard
- **Page caching with chunked reading** added to MCP `fetch_page` tool
- **Search within page** support added to `fetch_page` via `search` parameter
- Removed `max_results` param from `web_search` tool
- Added MCP dependencies to `requirements.txt` (mcp>=1.27.2, duckduckgo-search>=6.0.0)
- Added `build_exe.bat` for Windows standalone executable builds
- Handle model-specific capability errors with auto-retry and caching
- Removed unused dependencies and voice configuration from pyproject.toml

---

## v2.0.2 - Date: 2026-06-12

### Added
- **MCP Server Mode** with web search and cache search tools:
  - `web_search`  -  Search the web using DuckDuckGo HTML
  - `fetch_page`  -  Fetch and extract text from webpages with chunked reading (supports `offset`, `limit`, `refresh`, `search` parameters)
  - `search_cache`  -  Search all cached pages for keywords/phrase
  - `search_cache_snippet`  -  Search with surrounding context snippets and smart line boundary detection
- **Cross-platform masked input** in setup wizard (Windows + Unix)
- Fixed model mapping when `MODEL=windows:settings.json`  -  NIM model names now correctly matched

---

## v2.0.1 - Date: 2026-06-12

### Added
- **Recap skip optimization**  -  Skip redundant "recap" requests from Claude Code
- **Interactive setup wizard with section selection**  -  Users can choose which configuration sections to run

---

## v2.0.0 - Date: 2026-06-12

### Major Release  -  General Overhaul

**Standalone .exe**  -  NIMbus is now a single portable executable on Windows  -  no Python, no pip, no venv needed.
- `nimbus.exe --init`: Interactive setup wizard with live API key validation, model selection, Claude Code auto-config
- `nimbus.exe --init restore`: Restores backed-up settings.json
- Auto-creates `.env` from embedded template on first run
- Single `--onefile` PyInstaller build (~25 MB)

**Dynamic model resolution:** `MODEL=windows:settings.json` reads models from Claude Code's settings.json  -  no duplication. Model names resolved dynamically against NVIDIA's catalog.

**Error recovery:**
- Auto-detects models that reject `system` role and retries with system→user conversion
- Detailed error logging with full causal chain
- Tiktoken special token handling (`<|endoftext|>`, `<|fim_prefix|>`, etc.)
- Fixed HTTP transport request attribution (OpenAI SDK retry compatibility)

**Per-tier model config:** Sonnet/Opus/Haiku each get their own model, mapped from Claude Code settings.json

**Buffered server mode** with configurable retry for NVIDIA NIM (stream vs buffer modes)

**Discord bot integration foundation:**
- Multi-server support via comma-separated Guild IDs
- Channel/category-based conversation routing
- User blocking system (`/block`, `/unblock`, `/blocked`)
- Compaction warning and backup/download features
- `/newchannel` slash command for creating AI conversation channels
- Word boundary splitting for long messages
- `DISCORD_SKIP_FILES` toggle to ignore messages with attachments
- Discord message reply handling
- `DISCORD_ENABLED` toggle and message dropping when auto-compact disabled
- Discord command toggles and auto-compact disable option
- `DISCORD_CONVERSATION_CHANNEL_ID` for specific channel configuration
- Discord bot documentation in README
