# NIMbus Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## v2.0.8 - Date: 2026-06-26

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