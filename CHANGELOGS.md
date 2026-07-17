# NIMbus Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---
## v2.0.11 - Date: 2026-07-17

### Added

#### Inline Commands & Reasoning Control
- **`<nimhelp>` Inline Command** - Send exactly `<nimhelp>` for a formatted list of all inline commands. Works in both streaming/buffered endpoints; not gated by `SWAPPER_ENABLED`.
- **`<nimeffort:level>` Tag Handler** - Override reasoning effort per-message: `low`, `medium`, `high`, `xhigh`, `max`, `ultracode`. Stores per session (`x-claude-code-session-id`), returns mock response (no NVIDIA API call). Whitespace handling matches `<nimrpm:reset>`.
- **`<nimeffort>` / `<nimeffort:status>`** - Show current session's stored effort level, mapped effort, and custom budget.
- **Custom Reasoning Budget** - `<nimeffort:NNN>` accepts `-1` to `1,000,000` (`-1` = unlimited). New `api/effort_store.py` tracks per-session effort + budget separately. Priority: exact tag > request > session > config.
- **`-1` Budget = Unlimited** - Validator accepts `-1`, rejects `< -1`. Passed through to NVIDIA API per spec (disables reasoning token limit).
- **Ultracode Budget** - `reasoning_config.json` sets `ultracode` budget = `-1` (unlimited) for all Nemotron models.

#### Discord Bot Enhancements
- **Web Search Persistence** - Tool results saved in conversation history (`tool_results` field). New `get_history_with_tool_results()` injects prior results as context. Config: `DISCORD_WEB_SEARCH_MAX_RESULT_SIZE` (5000), `DISCORD_WEB_SEARCH_INCLUDE_IN_HISTORY` (true).
- **Playwright Browser Fetch** - When `DISCORD_BROWSER_HEADLESS=false`, forces browser for ALL fetches (30s timeout vs 10s HTTP). Reuses DuckDuckGo browser/context (cookies, stealth).
- **Embedded JSON Tool Call Parsing** - Detects `{"tool": "search", "query": "..."}` in text streams (Nemotron 3 Ultra) → converts to `tool_use` events.
- **Empty Reply Fix** - Final fallback retries 3× when model returns `tool_use` instead of text; adds "provide text answer only" prompt each retry; rejects `tool_use` in final response.
- **Smart Message Splitting** - `_split_at_word_boundary` prefers newlines then spaces (preserves URLs); uses `DISCORD_SPLIT_THRESHOLD` (default 1900).
- **Web Search Resilience** - "No results found" = valid result (not failure), reducing false tool removal. Buffered retries 3→5. Fallback synthesis when retries fail but tool results exist.

#### MCP Server
- **Browser Support for `fetch_page`** - New `MCP_BROWSER_HEADLESS` (default `true` = HTTP only). `use_browser` param forces Playwright when headless=true. Reuses DuckDuckGo browser instance. Logs fetch mode.

#### Setup Wizard & Configuration
- **Extended Wizard Coverage** - New prompts: request queue priorities (Discord HIGH / API NORMAL), Fable model override (`FABLE_OVERRIDE`), browser headless modes (MCP/Discord), web search debug logging (`WEB_SEARCH_DEBUG`), Discord web search settings.
- **Model-Specific Thinking Styles** - New `thinking_style` in `reasoning_config.json`:
  - **Nemotron**: `chat_template_kwargs.enable_thinking` + `reasoning_budget`
  - **DeepSeek**: `chat_template_kwargs.enable_thinking` (boolean)
  - **Minimax**: `chat_template_kwargs.thinking_mode` = `enabled`/`adaptive`/`disabled`
  - **Default**: effort flags + `reasoning_budget` (backward compat)
  - Models NOT in config (or `supports_thinking: false`) send NO thinking params - NVIDIA defaults apply.
- **`nim:` Prefix Model Lookup** - `nim:minimax-m3` bypasses tier mapping, resolves via NVIDIA catalog.
- **`SHOW_NIM_REPLY` Toggle** - Echo raw NVIDIA reply live to console (timestamped; `THINKING` = reasoning_content, `REPLY` = generated text). Both stream/buffer modes.
- **Discord Web Search Result Size** - `DISCORD_WEB_SEARCH_MAX_RESULT_SIZE` (5000), `DISCORD_WEB_SEARCH_INCLUDE_IN_HISTORY` (true).

#### Nemotron Thinking Support
- `reasoning_budget` - Max thinking tokens (model caps, max 32768)
- `chat_template_kwargs` - `enable_thinking` + `low_effort`/`medium_effort`/`high_effort` flags
- Effort→Budget: max/high/xhigh → max, medium → half, low → 2048
- Effort→Flags: xhigh/high/max → top, medium → medium, low → low
- Verified: Nemotron 3 Ultra (500B) & Nemotron 3 Super (120B) return reasoning content

#### Per-Session Reasoning Effort Tracking
- Captures `x-claude-code-session-id`; in-memory per-session effort cache
- Caches explicit request efforts; helper to clear session effort
- Added "ultracode" level + Nemotron mappings/budgets

#### Rate Limiter Auto-Restore
- New `NIM_RPM_RESET` (default `300s`, `0` = disabled). After last 429, if no new 429 for duration, auto-restores initial RPM + clears hold delay. Complements manual `<nimrpm:reset>`.

#### THINKING Log Line
- New `INFO` log: `THINKING: <model> using style=<style> effort=<raw> -> <mapped> [budget=<N>]`. Logs when thinking disabled or model not in config.

---

### Changed
- **Reasoning Config Consolidation** - Single `reasoning_config.json` replaces 3 files (`reasoning_budgets.yaml`, `reasoning_effort_presets.json`, `user_reasoning_effort_mapping.json`). Includes `thinking_style`, `effort_mapping`, `budget_per_effort` with glob patterns. **Legacy files deleted**.
- **Model Resolution for Config Lookup** - Uses resolved NIM model ID (from `get_model_for_claude`) for reasoning config lookup. Fixes `nim:minimax-m3` → `minimaxai/minimax-m3`.
- **Rate Limiter Initialization** - `GlobalRateLimiter` initialized early in FastAPI lifespan with `.env` settings before sidecars trigger singleton.
- **Removed `NIM_ENABLE_THINKING`** - Redundant flag removed; thinking now solely controlled by `NIM_THINKING`. Simplifies `build_request_body`.
- **Inline Command Detection (Multi-Block)** - `extract_last_text_content()` checks only last content block (user's command), avoiding false positives from system prompts/context. All 5 handlers updated. Buffered endpoint mock responses use `.model_dump()`.
- **Buffered Endpoint Inline Commands** - `<nimhelp>` and `<nimrpm:reset>` now work in buffered endpoint, positioned BEFORE `wait_if_blocked()` to bypass rate limiting during backoff.
- **SSE Dispatch Consolidation** - 3 copy-pasted SSE blocks → single `_sse_response()` helper. All 4 command dispatches now one-liners. Net -30 lines.
- **Default Model** - `deepseek-ai/deepseek-v4-flash` → `nvidia/nemotron-3-super-120b-a12b` in `.env.example` and setup wizard.

---

### Fixed
- **Rate Limiter Defaults** - Was using hardcoded defaults (`drop=10`, `min=20`) instead of `.env` values (`NIM_RPM_DROP=2`, `NIM_RPM_MIN=2`). Now reads from settings at startup.
- **Minimax Effort Mapping** - Efforts now map directly to `thinking_mode` values (`disabled`/`adaptive`/`enabled`) instead of budget numbers.
- **Streaming Idle Timeout** - Disabled idle read timeout (set to `None`) to prevent timeout errors on sporadic chunk arrival.
- **Inline Command False Positives** - Detection now checks only last content block, enabling inline commands with Claude Code's concatenated format (system reminder + user command blocks).
- **Tiktoken Special Token Crash** - Added `_safe_encode()` helper in `providers/sse_builder.py` that catches `ValueError` from disallowed special tokens (e.g., `<|endoftext|>` from Nemotron models) and retries with `disallowed_special=()` to encode them as regular text. Prevents crash in `estimate_output_tokens()` during streaming.

---

### Files Touched (Key Changes)
- `api/routes.py` - Inline command handlers, `_sse_response()`, buffered commands, multi-block detection
- `api/swapper/parser.py` - `<nimeffort:level>`, `<nimhelp>` parsers; `<nimrpm:reset>` exact-match
- `api/effort_store.py` - **New**: Per-session effort + custom budget tracking
- `api/dependencies.py` - `GlobalRateLimiter.get_instance()` with settings fallback; `SHOW_NIM_REPLY`
- `api/app.py` - Early `GlobalRateLimiter` init in lifespan
- `api/middleware.py` - Session ID capture
- `api/models/anthropic.py` - Session ID field
- `providers/request.py` - Thinking param rewrite (thinking_style-based); model resolution via NIM ID; `-1` budget pass-through; THINKING log; removed session cache
- `providers/provider.py` - `SHOW_NIM_REPLY` live logging; Nemotron thinking errors; streaming timeout; Discord log cleanup
- `providers/base.py` - Adaptive rate limit fields; `SHOW_NIM_REPLY` in config
- `providers/rate_limit.py` - `rpm_reset`, `_last_429_time`, auto-restore; `_rate_limit` property (test compat)
- `providers/sse_builder.py` - `_safe_encode()` helper for special tokens; `estimate_output_tokens()` uses safe encoding
- `providers/text.py` - `extract_last_text_content()` for multi-block detection
- `config/settings.py` - `nim_rpm_reset`, `SHOW_NIM_REPLY`, `ReasoningConfig` + `thinking_style`, glob matching, Discord web search, wizard additions
- `config/nim.py` - `-1` budget validator; reasoning effort validation
- `reasoning_config.json` - **New**: Consolidated config with `thinking_style` for all models; Minimax→`thinking_mode`; ultracode=-1 for Nemotron; glob patterns
- `discord_bot/bot.py` - Web search persistence, Playwright fetch, JSON tool parsing, empty reply fix, smart splitting, resilience
- `discord_bot/conversation.py` - `tool_results` field, `get_history_with_tool_results()`
- `discord_bot/persistence.py` - Tool results serialization
- `discord_bot/tools/web_search.py` - Fetch page schema, result size/history config
- `mcp_server.py` - Browser support for `fetch_page`
- `setup_wizard.py` - Extended config sections
- `.env.example` - All new env var docs
- `config/logging_config.py` - Timestamped log files per session
- `providers/error_mapping.py` - 403 handling with NVIDIA key URL
- `server.py` - Logging init at entry point
- **Deleted**: `config/reasoning_budgets.yaml`, `reasoning_effort_presets.json`, `user_reasoning_effort_mapping.json`

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