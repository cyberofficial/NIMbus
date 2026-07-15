# NIMbus

A lightweight FastAPI proxy that routes Claude Code through NVIDIA NIM. Free, no Anthropic API key required.

## Why NIMbus?

Claude Code CLI and VSCode extension require an Anthropic API key. NIMbus acts as a translation layer:

- **Free tier**: 40 requests per minute on NVIDIA NIM free tier
- **No Anthropic key needed**: Use Claude Code with NVIDIA's free API
- **Streaming support**: Full SSE streaming for real-time responses
- **Thinking models**: Converts reasoning content to Claude format
- **Lightweight**: Minimal dependencies, fast startup

## Quick Start

### Option 1: Standalone .exe (Windows, recommended)

No Python required. Download `nimbus.exe` from the [latest release](https://github.com/cyberofficial/NIMbus/releases).

```cmd
# 1. Run the exe - it auto-creates .env on first run
nimbus.exe --init

# 2. Follow the interactive wizard:
#    - Enter your NVIDIA API key (tested live)
#    - Choose your models and context window
#    - Auto-configures Claude Code settings

# 3. Start the proxy server
nimbus.exe

# 4. In another terminal, use Claude Code normally
claude
```

The `--init` wizard handles everything:
- Validates your NVIDIA API key against the live API
- Auto-generates a proxy API key
- Lets you pick models per Claude tier (Sonnet/Opus/Haiku) with context window selection
- Backs up and updates `%USERPROFILE%\.claude\settings.json` automatically
- Writes `.env` with all settings

To restore a backed-up settings.json: `nimbus.exe --init restore`

**Portable build:** The standalone exe embeds Playwright Chromium (~25 MB, total exe ~365 MB) via PyInstaller `--add-data`. No external `ms-playwright` folder or `playwright install` needed — browsers are extracted to a temp directory at runtime and cleaned up on exit.

### Option 2: Python (any OS)

**Prerequisites:** NVIDIA NIM API key, Python 3.14.3+, [Claude Code](https://github.com/anthropics/claude-code)

```bash
git clone https://github.com/cyberofficial/NIMbus.git
cd NIMbus
cp .env.example .env
```

Edit `.env`:

```dotenv
NVIDIA_NIM_API_KEY="nvapi-your-key-here"
MODEL="deepseek-ai/deepseek-v4-flash"
```

### Running the Server

**Using the standalone .exe (recommended - Windows):**
```bash
nimbus.exe
```

**Using Python directly:**
```bash
python server.py
```

**Using venv:**
```bash
python -m venv venv
venv\Scripts\activate   # Windows
source venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
python server.py
```

**Using start_server.py (cross-platform):**
```bash
python start_server.py
```

**Terminal 2 - Run Claude Code:**

```bash
ANTHROPIC_AUTH_TOKEN="<replaceme>" ANTHROPIC_BASE_URL="http://localhost:8082" claude
```

## VSCode Extension

1. Start the proxy server.
2. Open VSCode Settings (`Ctrl + ,`), search for `claude-code.environmentVariables`.
3. Click **Edit in settings.json** and add:

```json
"claude-code.environmentVariables": [
  { "name": "ANTHROPIC_BASE_URL", "value": "http://localhost:8082" },
  { "name": "ANTHROPIC_AUTH_TOKEN", "value": "<replaceme>" }
]
```

4. Reload extensions.

## Architecture

```
+------------------+      +----------------------+      +---------------+
| Claude Code      | ---> | NIMbus               | --->| NVIDIA NIM    |
| CLI / VSCode     | <--- | Proxy (:8082)        | <---| API           |
+------------------+      +----------------------+      +---------------+
   Anthropic format        Translation layer         OpenAI-compatible
   (SSE stream)                                      format (SSE stream)
```

**How it works:**

1. Claude Code sends Anthropic-format API requests to the proxy
2. Trivial requests (quota probes, title generation) are intercepted and answered locally
3. Real requests are translated to OpenAI format and sent to NVIDIA NIM
4. Responses are streamed back, converting thinking tags to Claude format

## Available Models

Browse all: [build.nvidia.com/explore/discover](https://build.nvidia.com/explore/discover)

## Model Mapping (Claude Tiers → NIM Models)

Claude Code sends requests with model identifiers like `claude-sonnet-4-6`, `claude-opus-4-7`, `claude-haiku-4-5`, `claude-fable-1-0`. NIMbus maps these to NIM models by position in the `MODEL` list (comma-separated):

| MODEL Count | Position 1 (Sonnet/Default) | Position 2 (Opus) | Position 3 (Haiku) | Position 4 (Fable) |
|-------------|-----------------------------|-------------------|---------------------|---------------------|
| 1 model | All tiers → model[0] | All tiers → model[0] | All tiers → model[0] | All tiers → model[0] |
| 2 models | Sonnet → model[0] | Opus → model[1] | Haiku → model[1] (last) | Fable → Opus model (model[1] or model[0] if 1 model) |
| 3 models | Sonnet → model[0] | Opus → model[1] | Haiku → model[2] (last) | Fable → Opus model (model[1]) |
| 4 models | Sonnet → model[0] | Opus → model[1] | Haiku → model[2] | Fable → model[1] |

**Fable:** The new `claude-fable-1-0` model defaults to the Opus position (model[1], or model[0] if only one model is configured). Override with `FABLE_OVERRIDE=owner/model-name` in `.env` (supports `[1m]` suffix for 1M context).

## Configuration

| Variable | Description | Default |
| --- | --- | --- |
| `MODEL` | Model identifier (`owner/model-name`, comma-separated for multi-model) | `deepseek-ai/deepseek-v4-flash` |
| `NVIDIA_NIM_API_KEY` | NVIDIA API key | **required** |
| `SERVER_TYPE` | Server mode: `stream` or `buffer` | `stream` |
| `NIM_MAX_TOKENS` | Max output tokens for responses | `202000` |
| `NIM_THINKING` | Enable thinking/reasoning content | `true` |
| `NIM_REASONING_EFFORT` | Reasoning effort: `low`, `medium`, or `high` | `high` |
| `NIM_REASONING_EFFORT_MAPPINGS` | JSON string mapping Claude effort levels to model-specific values | `` (empty) |
| `NIM_REASONING_BUDGET` | Max reasoning tokens (-1 = unlimited, 0 = auto) | `0` |
| `NIM_ENABLE_THINKING` | Enable thinking/reasoning mode | `true` |
| `NIM_CHAT_TEMPLATE_ENABLE_THINKING` | Enable thinking in chat template | `true` |
| `NIM_CHAT_TEMPLATE_LOW_EFFORT` | Enable low_effort flag in chat template | `false` |
| `NIM_CHAT_TEMPLATE_MEDIUM_EFFORT` | Enable medium_effort flag in chat template | `false` |
| `NIM_CHAT_TEMPLATE_HIGH_EFFORT` | Enable high_effort flag in chat template | `false` |
| `FABLE_OVERRIDE` | Override NIM model for Fable tier (supports `[1m]` suffix). Defaults to Opus model | `` (empty) |
| `PROVIDER_RATE_LIMIT` | Requests per window | `40` |
| `PROVIDER_RATE_WINDOW` | Rate window in seconds | `60` |
| `PROVIDER_MAX_CONCURRENCY` | Max concurrent streams | `5` |
| `RESOURCE_EXHAUSTED_RETRIES` | Max retries for ResourceExhausted (worker limit) errors (0 = endless) | `10` |
| `NIM_RPM_INITIAL` | Starting requests per minute (adaptive rate limiting) | `40` |
| `NIM_RPM_DROP` | RPM reduction per 429 hit | `10` |
| `NIM_RPM_MIN` | Floor RPM before hold delays | `20` |
| `NIM_RPM_HOLD_INITIAL` | First hold delay in seconds | `5` |
| `NIM_RPM_HOLD_MAX` | Maximum hold delay in seconds | `10` |
| `PROVIDER_MAX_WAIT_TIME` | Buffer mode max wait (s) | `30` |
| `PROVIDER_RETRY_ON_TRUNCATION` | Buffer mode retry count | `3` |
| `PROVIDER_RETRY_DELAY` | Buffer mode retry base delay (s) | `1.0` |
| `HTTP_READ_TIMEOUT` | Read timeout in seconds | `300` |
| `HTTP_WRITE_TIMEOUT` | Write timeout in seconds | `10` |
| `HTTP_CONNECT_TIMEOUT` | Connect timeout in seconds | `2` |
| `PORT` | Server port | `8082` |
| `PROXY_API_KEY` | Optional proxy authentication (auto-generated if empty) | (random) |
| `SWAPPER_ENABLED` | Enable dynamic `<modelswap:...>` chat tag | `false` |
| `SWAPPER_TEST_PROMPT` | Prompt used to validate swap-in model | `Please reply with pong only, nothing else` |
| `SWAPPER_TEST_TIMEOUT` | Model swapper test timeout (s) | `120.0` |
| `WEB_SEARCH_FETCH_TIMEOUT` | MCP `fetch_page` HTTP timeout (s) | `10.0` |
| `MCP_CACHE_TTL` | MCP cache TTL (s), max 3600, 0=disabled | `600` |
| `MCP_BROWSER_HEADLESS` | Use headless browser for MCP fetch_page (true/false) | `true` |
| `DISCORD_BROWSER_HEADLESS` | Use headless browser for Discord web search (true/false) | `true` |

### Log Files

Each server run creates a new timestamped log file in the format `server.YYYY-MM-DD_HH-MM-SS_XXXXXX.log` (e.g., `server.2026-07-11_20-33-47_529858.log`). There is no persistent `server.log` file and no log rotation during a session — one log file per server start. This avoids Windows file locking issues during rotation.

### Adaptive Rate Limiting & Reset Tag

NIMbus implements adaptive rate limiting that automatically backs off when NVIDIA returns 429 errors. Configuration:

| Variable | Description | Default |
| --- | --- | --- |
| `NIM_RPM_INITIAL` | Starting requests per minute | `40` |
| `NIM_RPM_DROP` | RPM reduction per 429 hit | `10` |
| `NIM_RPM_MIN` | Floor RPM before hold delays | `20` |
| `NIM_RPM_HOLD_INITIAL` | First hold delay in seconds | `5` |
| `NIM_RPM_HOLD_MAX` | Maximum hold delay in seconds | `10` |

**Reset Tag:** Include `<nimrpm:reset>` in any chat message (must be the **entire** message content, optional surrounding whitespace allowed) to reset adaptive backoff state — restores initial RPM and clears hold delays. Useful after changing models or when rate limits recover.

### Discord Bot (Optional)

Both the bot token and a configured guild ID must be present (and `DISCORD_ENABLED` not set to `false`) for the bot to start.

**Role mentions:** The bot now responds when mentioned via role (`@&role_id`) in addition to direct user mentions (`@username`). It checks if the mentioned role is assigned to the bot.

| Variable | Description | Default |
| --- | --- | --- |
| `DISCORD_ENABLED` | Explicit kill-switch  -  must be `true` for the bot to start even when a token/guild are configured | `true` (Pydantic default; `.env.example` ships `false`) |
| `DISCORD_BOT_TOKEN` | Discord bot token from https://discord.com/developers/applications | `` (empty) |
| `DISCORD_GUILD_ID` | Guild (server) IDs the bot is locked to  -  comma-separated for multiple servers; legacy single-ID form also accepted | `0` |
| `DISCORD_CONTROL_CHANNEL_ID` | Channel IDs that receive the control-panel embed and welcome messages  -  comma-separated | `0` |
| `DISCORD_CONVERSATION_CATEGORY_ID` | Category IDs under which conversation channels live  -  comma-separated | `0` |
| `DISCORD_CONVERSATION_CHANNEL_ID` | Specific channel IDs for conversations (alternative to categories)  -  comma-separated; combining with categories means "respond in either" | `` (empty) |
| `DISCORD_OWNER_ID` | Discord user ID granted owner-only access | `0` |
| `DISCORD_OWNER_ONLY` | `true` = only owner can use the bot; `false` = anyone in the server | `true` |
| `DISCORD_MAX_TOKENS` | Token limit per conversation before auto-compaction can trigger | `202000` |
| `DISCORD_COMPACT_THRESHOLD` | Fraction of `DISCORD_MAX_TOKENS` that triggers compaction (warn fires 5% earlier) | `0.8` |
| `DISCORD_AUTO_COMPACT` | `true` = summarize+restart at threshold; `false` = silently FIFO-drop oldest messages to make room | `true` |
| `DISCORD_MODEL` | Override model for Discord bot (use different model than API proxy). Empty = falls back to MODEL; if MODEL=windows:settings.json, uses Opus > Sonnet > Haiku from Claude settings. If nothing resolved, bot won't start. | `` (empty) |
| `DISCORD_USER_COOLDOWN` | Per-user cooldown (seconds) before another request is accepted | `10` |
| `DISCORD_SERVER_LIMIT` | Server-wide request cap per `DISCORD_SERVER_WINDOW` | `20` |
| `DISCORD_SERVER_WINDOW` | Length of the server-wide rate-limit window (seconds) | `60` |
| `DISCORD_SYSTEM_PROMPT` | System prompt sent with every chat request (live, slash, and prefix) | (casual/conversational default  -  see `config/settings.py`) |
| `DISCORD_SKIP_FILES` | Ignore messages that carry attachments | `true` |
| `DISCORD_SPLIT_THRESHOLD` | Max characters per outgoing message before splitting (Discord limit is 2000) | `1900` |
| `DISCORD_COMMAND_PREFIX` | Prefix that activates text commands (in addition to slash commands) | `!!` |
| `DISCORD_REQUIRE_MENTION` | `true` = only respond to @mentions / prefix; `false` = respond to every message in conversation channels | `true` |
| `DISCORD_CMD_ASK` | Enable the `/ask` slash command | `true` |
| `DISCORD_CMD_COMPACT` | Enable the `/compact` slash command | `true` |
| `DISCORD_CMD_NEW` | Enable the `/new` slash command | `true` |
| `DISCORD_CMD_STATUS` | Enable the `/status` slash command | `true` |
| `DISCORD_CMD_DOWNLOAD` | Enable the `/download` slash command | `true` |
| `DISCORD_CMD_BLOCK` | Enable the `/block` slash command | `true` |
| `DISCORD_CMD_UNBLOCK` | Enable the `/unblock` slash command | `true` |
| `DISCORD_CMD_BLOCKED` | Enable the `/blocked` slash command | `true` |
| `DISCORD_CMD_NEWCHANNEL` | Enable the `/newchannel` slash command | `true` |
| `DISCORD_CMD_PREFIX_ASK` | Enable the `!!ask` prefix command | `true` |
| `DISCORD_CMD_PREFIX_COMPACT` | Enable the `!!compact` prefix command | `true` |
| `DISCORD_CMD_PREFIX_NEW` | Enable the `!!new` prefix command | `true` |
| `DISCORD_CMD_PREFIX_STATUS` | Enable the `!!status` prefix command | `true` |
| `DISCORD_ENABLE_WEB_SEARCH` | Enable web search/fetch for the Discord bot | `true` |
| `DISCORD_WEB_SEARCH_MAX_RESULTS` | Max search results per query | `5` |
| `DISCORD_WEB_SEARCH_MAX_ITERATIONS` | Max tool call iterations per response | `10` |

### Web Search (Discord Bot & MCP Server)

The Discord bot and MCP server include web search and page fetch tools using **DuckDuckGo via Playwright** (since v2.0.8). This provides robust, JavaScript-capable searching with:

- **Playwright with stealth mode** — evades anti-bot detection via `playwright-stealth`
- **Persistent storage state** — reuses cookies/session across searches for better results
- **Multi-endpoint fallback** — tries `html.duckduckgo.com` first, falls back to `lite.duckduckgo.com`
- **Pagination with deduping** — fetches multiple pages, removes duplicate URLs
- **Failure tracking (Discord)** — consecutive `web_search` failures (2+) auto-disable the search tool, keeping only `fetch_page` available
- **Final buffered fallback** — when tool calls produce no text after max iterations, sends final request without tools to force a text response

MCP server configuration (see [MCP Server Mode](#mcp-server-mode-web-search-tools) for setup):
| Variable | Description | Default |
| --- | --- | --- |
| `WEB_SEARCH_FETCH_TIMEOUT` | HTTP timeout for `fetch_page` (s) | `10.0` |
| `MCP_CACHE_TTL` | Cache TTL (s), max 3600, 0=disabled | `600` |

### Stream vs Buffer Modes

NIMbus has two server modes controlled by `SERVER_TYPE`. Both produce Anthropic-format responses compatible with Claude Code, but they trade off latency for reliability differently.

#### Stream Mode (`SERVER_TYPE=stream` - default)

Tokens are relayed to Claude Code as NVIDIA generates them, just like a direct connection.

- **Lowest latency** - Claude Code sees tokens immediately
- **What happens during backend cutout**: The proxy sends a partial response with `stop_reason="max_tokens"` and logs a warning. Claude Code receives whatever was generated before the interruption.
- **No retry** - streaming cannot replay already-sent tokens, so a dropped connection means a partial response.
- **Best for** interactive use where you want to see output as it's produced.

```
Claude Code ──── SSE stream ──── NIMbus ──── SSE stream ──── NVIDIA NIM
              (live tokens)               (live tokens)
```

If NVIDIA's backend cuts out mid-stream, the `SSEBuilder.truncated` flag is set and the final `message_delta` event carries `stop_reason: "max_tokens"`.

#### Buffer Mode (`SERVER_TYPE=buffer`)

The proxy waits for NVIDIA to finish generating the **complete** response before sending anything to Claude Code. If the backend drops the connection, the proxy automatically retries.

- **Higher latency** - Claude Code waits until the full response is ready
- **Automatic retry with exponential backoff** on connection loss (`APIConnectionError`) and timeouts (`APITimeoutError`)
- **Configurable retry behavior**:
  | Setting | Default | What it does |
  |---|---|---|
  | `PROVIDER_RETRY_ON_TRUNCATION` | `3` | Number of retry attempts before giving up |
  | `PROVIDER_RETRY_DELAY` | `1.0` | Base delay between retries (seconds) - multiplies by attempt number |
  | `PROVIDER_MAX_WAIT_TIME` | `30` | Seconds to wait for NVIDIA before timing out and retrying |
- **Retries count against the rate limit** to prevent exceeding your quota when the backend is unstable
- If all retries are exhausted, raises `StreamTruncatedError` (mapped to an HTTP 500 error)
- **Best for** long-generation tasks where losing the response is worse than waiting

> **Note:** NVIDIA's free tier occasionally drops connections mid-response. Stream mode will produce a partial answer; buffer mode will retry up to `PROVIDER_RETRY_ON_TRUNCATION` times to get a complete response.
>
> **Note:** The underlying `execute_with_retry` helper (used by both stream and buffer modes) treats `max_retries=0` as "infinite retries" — it will retry forever until the request succeeds or a non-retryable error occurs.

```
Claude Code ──── JSON response ──── NIMbus ──── (wait + retry if needed) ──── NVIDIA NIM
              (all at once)                   (accumulate complete response)
```

**Which should I choose?**

| Scenario | Recommendation |
|---|---|
| Interactive coding / quick questions | `stream` (default) |
| Batch processing / generating large files | `buffer` |
| Spotty network or unstable backend | `buffer` |
| Lowest latency matters most | `stream` |

> **Note:** NVIDIA's free tier occasionally drops connections mid-response. Stream mode will produce a partial answer; buffer mode will retry up to `PROVIDER_RETRY_ON_TRUNCATION` times to get a complete response.

### Request Queue (New in v2.0.7)

NIMbus includes a built-in request queue to prevent `Worker local total request limit reached (33/32)` errors from NVIDIA NIM. Each NVIDIA NIM worker has a hard limit of 32 concurrent requests. When multiple sessions send requests simultaneously, the queue serializes them while respecting priority.

**How it works:**
- Requests are queued FIFO within their priority lane
- A worker pool (semaphore) limits concurrent in-flight requests to `REQUEST_QUEUE_MAX_CONCURRENT` (default 32)
- Background workers pull from the queue when slots are available
- If the queue is full, new requests receive a 503 error with retry guidance

| Variable | Description | Default |
| --- | --- | --- |
| `REQUEST_QUEUE_ENABLED` | Enable the request queue | `true` |
| `REQUEST_QUEUE_MAX_CONCURRENT` | Max concurrent requests to NVIDIA (must be ≤ 32) | `32` |
| `REQUEST_QUEUE_MAX_SIZE` | Max requests waiting in queue | `600` |
| `REQUEST_QUEUE_TIMEOUT` | Max seconds a request waits in queue before timeout | `300.0` |
| `REQUEST_QUEUE_NUM_WORKERS` | Background worker threads processing the queue | `4` |
| `REQUEST_QUEUE_DISCORD_PRIORITY` | Priority for Discord bot (2=HIGH, 1=NORMAL, 0=LOW) | `2` |
| `REQUEST_QUEUE_API_PRIORITY` | Priority for API proxy requests (2=HIGH, 1=NORMAL, 0=LOW) | `1` |

**Priority Lanes:**
- **HIGH (2)**: Discord bot - interactive chat, gets processed first
- **NORMAL (1)**: API proxy - buffered/streaming endpoints  
- **LOW (0)**: Background tasks - future use

**Observability:**
- `GET /queue/status` - Returns queue stats (depth, wait times, rejections, worker usage)
- `GET /status` - Includes `worker_limit` and `worker_available` from rate limiter
- Logs show queue depth and wait times at DEBUG level

**When to tune:**
| Scenario | Adjustment |
| --- | --- |
| Many concurrent Discord users | Increase `REQUEST_QUEUE_MAX_SIZE` (e.g., 1000) |
| Fewer NVIDIA workers available | Decrease `REQUEST_QUEUE_MAX_CONCURRENT` (e.g., 16) |
| Queue timeouts under burst load | Increase `REQUEST_QUEUE_TIMEOUT` |
| Need more throughput | Increase `REQUEST_QUEUE_NUM_WORKERS` |

**Rollback:** Set `REQUEST_QUEUE_ENABLED=false` to bypass the queue entirely - falls back to the existing rate limiter and retry logic.

### Optimization Settings

These settings speed up Claude Code by mocking/skipping unnecessary requests. The handlers run in this order at every request  -  first non-`None` result wins:

1. Recap skip
2. Quota probe mock
3. Prefix detection
4. Title generation skip
5. Suggestion mode skip
6. Filepath extraction mock

| Variable | Description | Default |
| --- | --- | --- |
| `ENABLE_RECAP_SKIP` | Block recap requests (stepped away/return) | `false` **disabled** |
| `ENABLE_NETWORK_PROBE_MOCK` | Mock quota probe requests | `true` |
| `FAST_PREFIX_DETECTION` | Fast command prefix detection | `true` |
| `ENABLE_TITLE_GENERATION_SKIP` | Skip title generation requests | `true` |
| `ENABLE_SUGGESTION_MODE_SKIP` | Skip suggestion mode requests | `false` **disabled** |
| `ENABLE_FILEPATH_EXTRACTION_MOCK` | Mock filepath extraction | `true` |

> **Note:** `ENABLE_SUGGESTION_MODE_SKIP` and `ENABLE_RECAP_SKIP` are currently no-ops. The handlers exist in the optimization chain but return `None` immediately, with their detection logic (and the call to `is_suggestion_mode_request` / `is_recap_request`) kept as commented-out reference code in `api/optimization_handlers.py`. They are disabled because their current detection produces false positives; the helpers can be re-enabled (in code) once better detection is implemented.

### Reasoning Effort Mapping

NIMbus now supports runtime mapping of Claude Code's reasoning effort levels to model-specific reasoning parameters via the `/effort` command. This allows users to control the depth of reasoning at runtime using Claude's native `/effort low|medium|high|xhigh|max|ultracode` command.

**Configuration:**
Set `NIM_REASONING_EFFORT_MAPPINGS` environment variable with a JSON string that maps Claude effort levels to model-specific values:
```bash
NIM_REASONING_EFFORT_MAPPINGS='{"deepseek": {"xhigh": "max", "high": "high"}}'
```

**Preset Mappings:**
A comprehensive set of preset mappings is available in `reasoning_config.json`. The file uses a 2/3-part format: `"low:medium:2048"` (level:mapped:budget) or `"low:2048"` (level:budget with identity mapping).

**How it works:**
1. When Claude Code sends an `/effort` command, it includes the effort level in the request's `thinking.effort` field
2. NIMbus looks up the model-specific mapping for that effort level
3. The mapped value is sent to the NIM model as the `reasoning_effort` parameter
4. If no mapping exists for a model, the effort level is used as-is (fallback behavior)

**Example mappings (from reasoning_config.json):**
- Nemotron 3 Ultra: `low` → `medium:2048`, `medium` → `medium:8192`, `high/xhigh/max` → `high:32768`, `ultracode` → `high:-1`
- DeepSeek models: `low` → `low:2048`, `medium` → `medium:8192`, `high` → `high:16384`, `xhigh/max/ultracode` → `max:32768`
- All levels to high: `{"deepseek": {"low": "high", "medium": "high", "high": "high", "xhigh": "high", "max": "high", "ultracode": "high"}}`
- Custom mapping: `{"deepseek": {"low": "xhigh", "medium": "high", "high": "high"}}` (low → xhigh, medium/high → high)
- Any combination: Users can define any mapping they want for any effort level

See [reasoning_config.json](reasoning_config.json) for detailed preset mappings and format.

See [`.env.example`](.env.example) for all options.

## API Endpoints

| Endpoint | Description |
| --- | --- |
| `GET /` | Root - returns provider info, model, and model list |
| `POST /v1/messages` | Create a message (streaming) |
| `POST /v1/messages/buffered` | Create a message (buffered, with retry) |
| `POST /v1/messages/count_tokens` | Count tokens for a request |
| `GET /health` | Health check |
| `GET /status` | Server status |
| `GET /queue/status` | Request queue status (depth, wait times, rejections) |
| `POST /stop` | Stop all CLI sessions and pending tasks |

## Model Swapper

Mid-session model switching via a chat tag, no proxy restart needed.

Set `SWAPPER_ENABLED=true` in `.env`, then in any Claude Code message include one of these tags (the proxy strips it before forwarding to the model):

| Tag | Effect |
| --- | --- |
| `<modelswap:model-name>` | Swap the active NIM model for the rest of the session |
| `<modelswap:clear>` | Revert to the default per-tier model mapping |

A short-name (e.g. `deepseek-v4-pro`) is resolved against NVIDIA's live catalog; the full `org/name` form also works. If the model doesn't exist or fails the test prompt, the proxy responds with a short error message and a list of similar models from the same org.

**Additional inline commands (always work, no config needed):**

| Tag | Effect |
| --- | --- |
| `<nimrpm:reset>` | Reset adaptive rate limit backoff (restore RPM, clear hold delays) |
| `<nimhelp>` | Show list of all available inline commands |
| `<nimeffort:level>` | Set reasoning effort: `low`, `medium`, `high`, `xhigh`, `max`, `ultracode`, or int (`-1` to `1000000`) |
| `<nimeffort>`         | Show current reasoning effort level for this session |

## NIM Server Swapper

Mid-session switching between `stream` and `buffer` server modes via a chat tag, no proxy restart needed. Works independently of the Model Swapper  -  you can swap both model and server type in the same session.

No configuration variable is needed to enable it (unlike `SWAPPER_ENABLED` for the model swapper).

In any Claude Code message include one of these tags (the proxy strips it before forwarding to the model):

| Tag | Effect |
| --- | --- |
| `<nimserver:stream>` | Switch to streaming mode for the rest of the session |
| `<nimserver:buffer>` | Switch to buffered mode (with retry on failure) for the rest of the session |
| `<nimserver:clear>` | Revert to the default `SERVER_TYPE` from `.env` |

**Additional inline commands (always work, no config needed):**

| Tag | Effect |
| --- | --- |
| `<nimrpm:reset>` | Reset adaptive rate limit backoff (restore RPM, clear hold delays) |
| `<nimhelp>` | Show list of all available inline commands |
| `<nimeffort:level>` | Set reasoning effort: `low`, `medium`, `high`, `xhigh`, `max`, `ultracode`, or int (`-1` to `1000000`) |
| `<nimeffort>`         | Show current reasoning effort level for this session |

Once set, **all subsequent requests** from that API key will use the chosen mode until you send `<nimserver:clear>` or restart the proxy.

### How it works behind the scenes

The override is stored per-API-key in the same in-memory pattern as the Model Swapper. When active:

- **On the streaming endpoint** (`/v1/messages`): If `<nimserver:buffer>` is active, the route calls `provider.buffered_request()` internally, collects the complete response, then converts it to SSE events. You get the reliability of buffer mode through the streaming protocol.
- **On the buffered endpoint** (`/v1/messages/buffered`): If `<nimserver:stream>` is active, the route calls `provider.stream_response()`, collects text deltas from the SSE stream, and returns the result as a JSON response.

### When to swap mid-session

| Situation | Tag |
|---|---|
| A generation keeps failing mid-stream | `<nimserver:buffer>`  -  retries automatically |
| You're waiting too long for buffered results | `<nimserver:stream>`  -  see tokens live |
| Interactive coding session getting a lot of errors | `<nimserver:buffer>`  -  more reliable |
| Done with the error-prone task, back to quick questions | `<nimserver:clear>` or `<nimserver:stream>` |

## Troubleshooting

### Common Issues

**Connection refused**
- Ensure the proxy is running on the correct port
- Check firewall settings

**Rate limit exceeded**
- NVIDIA NIM free tier: 40 requests/minute
- Wait and retry, or reduce concurrent requests

**Model not found**
- Verify MODEL format: `owner/model-name`
- Check available models at [build.nvidia.com](https://build.nvidia.com/explore/discover)

### Logs

Each server run creates a new timestamped log file: `server.YYYY-MM-DD_HH-MM-SS_XXXXXX.log` (e.g., `server.2026-07-11_20-33-47_529858.log`). No persistent `server.log` and no log rotation during a session — one log file per server start. This avoids Windows file locking issues during rotation.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## Discord Bot (Optional)

A Discord bot integration is included for multi-user access through Discord channels.

### Setup

1. Create a Discord application at https://discord.com/developers/applications
2. Enable "Message Content Intent" in the Bot section
3. Invite the bot to your server with these permissions:
   - Send Messages
   - Read Messages/View Channels
   - Manage Channels
   - Read Message History
4. Configure in `.env`:

```dotenv
# --- Bot enable / connection ---
DISCORD_ENABLED=true                        # Explicit kill-switch (true = bot can start; default false)
DISCORD_BOT_TOKEN="your-bot-token-here"    # Get bot token at https://discord.com/developers/applications
DISCORD_GUILD_ID="123456789"               # Server ID (comma-separated for multiple)
DISCORD_CONTROL_CHANNEL_ID="123456789"     # Admin channel for status (comma-separated)
DISCORD_CONVERSATION_CATEGORY_ID="123456789"  # Category for AI channels (comma-separated)
DISCORD_CONVERSATION_CHANNEL_ID=""         # Specific channel IDs (alternative to categories; comma-separated)

# --- Access control ---
DISCORD_OWNER_ID="123456789"               # Your Discord user ID
DISCORD_OWNER_ONLY=true                    # true = owner only, false = anyone in server

# --- Conversation / compaction ---
DISCORD_AUTO_COMPACT=true                  # true = summarize/restart, false = drop oldest messages
DISCORD_MAX_TOKENS=202000                  # Token limit per conversation before compact triggers
DISCORD_COMPACT_THRESHOLD=0.8              # Fraction of MAX_TOKENS that triggers compaction
DISCORD_SYSTEM_PROMPT="You are a helpful Discord bot. ..."  # System prompt applied to every chat

# --- Live conversation behavior ---
DISCORD_COMMAND_PREFIX="!!"                # Prefix for text-based commands (default !!)
DISCORD_REQUIRE_MENTION=true               # true = only @mentions/prefix, false = every message
DISCORD_SKIP_FILES=true                    # ignore messages with attachments
DISCORD_SPLIT_THRESHOLD=1900               # max chars before splitting long messages (Discord limit 2000)

# --- Rate limiting ---
DISCORD_USER_COOLDOWN=10                   # per-user cooldown (seconds)
DISCORD_SERVER_LIMIT=20                    # server-wide requests per window
DISCORD_SERVER_WINDOW=60                   # rate-limit window (seconds)

# --- Slash command toggles (default true; set false to disable) ---
DISCORD_CMD_ASK=true
DISCORD_CMD_COMPACT=true
DISCORD_CMD_NEW=true
DISCORD_CMD_STATUS=true
DISCORD_CMD_DOWNLOAD=true
DISCORD_CMD_BLOCK=true
DISCORD_CMD_UNBLOCK=true
DISCORD_CMD_BLOCKED=true
DISCORD_CMD_NEWCHANNEL=true

# --- Prefix command toggles (default true; set false to disable) ---
DISCORD_CMD_PREFIX_ASK=true
DISCORD_CMD_PREFIX_COMPACT=true
DISCORD_CMD_PREFIX_NEW=true
DISCORD_CMD_PREFIX_STATUS=true
```

**Channel Configuration:**
- **Categories**: Bot responds in any channel under `DISCORD_CONVERSATION_CATEGORY_ID`
- **Specific Channels**: Bot only responds in `DISCORD_CONVERSATION_CHANNEL_ID` channels
- **Both**: Can combine (bot responds in specified channels OR channels in categories)

### Bot Commands

Slash commands:

| Command | Description |
|---------|-------------|
| `/ask [question]` | Ask NIM a question with conversation history |
| `/compact` | Summarize conversation and restart (with backup option) |
| `/new` | Clear conversation history without summary |
| `/download` | Download conversation history as markdown |
| `/status` | Show bot and rate limit status |
| `/block [user]` | Block a user from using the bot (owner only) |
| `/unblock [user]` | Unblock a user (owner only) |
| `/blocked` | List blocked users (owner only) |
| `/newchannel [name]` | Create a new AI conversation channel |

Prefix commands (default prefix `!!`; configurable via `DISCORD_COMMAND_PREFIX`):
`!!ask <question>` · `!!compact` · `!!new` · `!!status`

The bot also responds to every message in conversation channels (when `DISCORD_REQUIRE_MENTION=false`) or only to @mentions/prefix invocations (when `true`).

### Features

- **Multi-server support**: Configure multiple guilds/servers with comma-separated IDs
- **System prompt**: Set via `DISCORD_SYSTEM_PROMPT`; applied to every chat request (live, slash, and prefix)
- **Rate limiting**: Per-user cooldown and server-wide limits (`DISCORD_USER_COOLDOWN`, `DISCORD_SERVER_LIMIT`, `DISCORD_SERVER_WINDOW`)
- **Conversation modes**:
  - `DISCORD_AUTO_COMPACT=true` (default): Summarizes and restarts conversation when token limit reached
  - `DISCORD_AUTO_COMPACT=false`: Silently drops oldest messages to make room for new ones
- **Mass-ping protection**: `@everyone` / `@here` are sanitized with zero-width spaces on user input, bot output, and stored history
- **Typing indicator with exponential backoff**: Typing indicator refreshes periodically (starting at 10s intervals) and automatically increases interval by +5s on Discord 429 rate limits (capped at 60s), then resets to 10s on success
- **Per-channel message queue**: Messages in each channel are processed sequentially (FIFO) to preserve conversation context order when multiple users ping simultaneously
- **Independent model configuration**: Use `DISCORD_MODEL` to run the Discord bot with a different model than the main API proxy. Falls back to MODEL or (if MODEL=windows:settings.json) Opus > Sonnet > Haiku from Claude settings. If nothing resolved, bot won't start.
- **Message splitting**: Automatically splits long responses for Discord's 2000 char limit
- **Command toggles**: Disable individual slash commands via `DISCORD_CMD_*` settings; disable individual prefix commands via `DISCORD_CMD_PREFIX_*` settings
- **Setup wizard**: `nimbus.exe --init` walks through every Discord setting interactively
- **Web Search Integration**: The Bot can search the web and fetch pages using DuckDuckGo. When enabled (default), the model automatically detects when a search is needed (e.g., "what's the latest version of...", "search for...") and performs searches, fetches pages, and cross-references sources until confident. Results are incorporated into the final answer with a disclaimer: `-# This response used online resources, please make sure to verify the information`. Search activity is logged to console (`[WEB SEARCH] tool=web_search input="..." | result_len=...`). Configure with `DISCORD_ENABLE_WEB_SEARCH=true`, `DISCORD_WEB_SEARCH_MAX_RESULTS=5`, `DISCORD_WEB_SEARCH_MAX_ITERATIONS=10`, `DISCORD_WEB_SEARCH_MAX_RESULT_SIZE=5000`, `DISCORD_WEB_SEARCH_INCLUDE_IN_HISTORY=true`, `DISCORD_BROWSER_HEADLESS=true`.

## MCP Server Mode (Web Search Tools)

NIMbus can also run as an MCP (Model Context Protocol) server, exposing web search and page fetch tools directly to Claude Code. This allows Claude to search the web and fetch page content without going through the NVIDIA NIM proxy.

### Quick Start

```bash
# Add to Claude Code (using exe)  -  global scope (available in all projects)
claude mcp add web_search -s user -- nimbus.exe --mcp

# Or using Python (venv) with start_server.py  -  global scope
claude mcp add web_search -s user -- /path/to/NIMbus/venv/bin/python /path/to/NIMbus/start_server.py --mcp

# Or using Python (venv) with server.py  -  global scope
claude mcp add web_search -s user -- /path/to/NIMbus/venv/bin/python /path/to/NIMbus/server.py --mcp

# For project-scoped installation (stored in .mcp.json, shareable via git)
# claude mcp add web_search -s project -- nimbus.exe --mcp
```

**Scope summary:**
| Flag | Scope | Stored in | Available |
|---|---|---|---|
| `-s user` | User/global | `~/.claude.json` | All projects |
| `-s project` | Project | `.mcp.json` in project root | Current project (shareable via git) |
| *(none)* | Local | `~/.claude.json` (per-path) | Current directory only |

After adding, verify with `claude mcp list` to confirm it shows under user scope.

### MCP Tools

| Tool | Description | Parameters |
|------|-------------|------------|
| `web_search` | Search the web using DuckDuckGo HTML | `query` (string) |
| `fetch_page` | Fetch and extract text from a webpage with chunked reading (supports search within page) | `url` (string), `offset` (int, default: 0), `limit` (int, default: 10000), `refresh` (bool, default: false), `search` (string, optional) |
| `search_cache` | Search all cached pages for a keyword/phrase | `query` (string), `case_sensitive` (bool, default: false), `max_results` (int, default: 50) |
| `search_cache_snippet` | Search cached pages with surrounding code snippets and smart line boundary detection | `query` (string), `before_chars` (int, default: 400), `after_chars` (int, default: 500), `case_sensitive` (bool, default: false), `max_results` (int, default: 20) |

### Running MCP Server Manually

```bash
# Development mode (using start_server.py)
python start_server.py --mcp

# Development mode (using server.py directly)
python server.py --mcp

# Standalone exe (Windows)
nimbus.exe --mcp
```

### MCP Environment Configuration

The MCP server inherits settings from `.env`. Configure web search behavior via:

```dotenv
# MCP Server settings
NVIDIA_NIM_API_KEY="nvapi-your-key-here"  # Not required for MCP mode but kept for proxy mode

# Web Search Configuration
WEB_SEARCH_FETCH_TIMEOUT=10.0     # HTTP timeout for fetch_page in seconds (default: 10.0)
MCP_BROWSER_HEADLESS=true         # Use headless browser for fetch_page (true = fast HTTP, false = visible browser)

# Cache Configuration
MCP_CACHE_TTL=600                 # Cache TTL in seconds (default: 600 = 10 minutes, max 3600, 0 = disabled)
                                  # Cache directory is hardcoded to ./NIMBUS_FETCH_CACHE next to mcp_server.py
```

### Using with Claude Code

Once added via `claude mcp add web_search ...`, Claude will have access to `web_search`, `fetch_page`, `search_cache`, and `search_cache_snippet` tools. Example usage in Claude:

```
> Can you search for "latest Rust async patterns" and fetch the first result?
```

Claude will automatically call the MCP tools and return the results.

#### Chunked Reading Example

For long pages (e.g., documentation), use `offset` and `limit` to read in chunks:

```
> Fetch page at offset 10000 with limit 10000
# Returns chunk 10000-20000 with metadata: total_length, cache status, etc.

> Fetch page with refresh=true
# Forces fresh fetch, bypassing cache
```

The `fetch_page` tool returns JSON with:
- `content`: The requested text chunk
- `total_length`: Full page length in characters
- `offset`: Starting position of returned chunk
- `limit`: Requested chunk size
- `cached`: Whether served from cache
- `cache_expires_at`: ISO timestamp when cache expires

**Cache Control:**
- Set `MCP_CACHE_TTL=0` to disable caching entirely (always fresh)
- Use `refresh=true` parameter to force fresh fetch on demand
- Default TTL: 10 minutes (600s), maximum: 1 hour (3600s)

#### Search Within Cache

Search across all cached pages with `search_cache` (returns matching lines) or `search_cache_snippet` (returns surrounding context):

```
> Search cached docs for "_ENV_TEMPLATE"
# Returns all matching lines with line numbers and character positions

> Search cached docs for ".env was deleted" with 400 before, 500 after
# Returns code snippets with smart line boundary detection

> Fetch Python docs and search for "async def"
# Returns matches within that specific page with context
```

You can also search within a specific fetched page using the `search` parameter on `fetch_page`:

```
> Fetch page with search=".env was deleted"
# Returns matches with line numbers, character positions, and surrounding context
```

---

## Changelog

See [CHANGELOGS.md](CHANGELOGS.md) for the full version history and release notes.

## License

AGPL-3.0 - See [LICENSE](LICENSE) for details.

## Acknowledgments

- [NVIDIA NIM](https://build.nvidia.com/) for providing free API access
- [Claude Code](https://github.com/anthropics/claude-code) by Anthropic
- [FastAPI](https://fastapi.tiangolo.com/) for the web framework
