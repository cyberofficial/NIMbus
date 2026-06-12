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

### Option 2: Python (any OS)

**Prerequisites:** NVIDIA NIM API key, Python 3.14+, [Claude Code](https://github.com/anthropics/claude-code)

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

**Using uv (recommended):**
```bash
uv run uvicorn server:app --host 0.0.0.0 --port 8082
```

**Using venv:**
```bash
python -m venv venv
venv\Scripts\activate   # Windows
source venv/bin/activate  # macOS/Linux
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8082
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

## Configuration

| Variable | Description | Default |
| --- | --- | --- |
| `MODEL` | Model identifier (`owner/model-name`, comma-separated for multi-model) | `deepseek-ai/deepseek-v4-flash` |
| `NVIDIA_NIM_API_KEY` | NVIDIA API key | **required** |
| `SERVER_TYPE` | Server mode: `stream` or `buffer` | `stream` |
| `NIM_MAX_TOKENS` | Max tokens for responses | `202000` |
| `NIM_THINKING` | Enable thinking/reasoning content | `true` |
| `NIM_REASONING_EFFORT` | Reasoning effort: `low`, `medium`, or `high` | `high` |
| `PROVIDER_RATE_LIMIT` | Requests per window | `40` |
| `PROVIDER_RATE_WINDOW` | Rate window in seconds | `60` |
| `PROVIDER_MAX_CONCURRENCY` | Max concurrent streams | `5` |
| `PROVIDER_RETRY_ON_TRUNCATION` | Buffer mode retry count | `3` |
| `PROVIDER_RETRY_DELAY` | Buffer mode retry base delay (s) | `1.0` |
| `PROVIDER_MAX_WAIT_TIME` | Buffer mode max wait (s) | `30` |
| `HTTP_READ_TIMEOUT` | Read timeout in seconds | `300` |
| `HTTP_WRITE_TIMEOUT` | Write timeout in seconds | `10` |
| `HTTP_CONNECT_TIMEOUT` | Connect timeout in seconds | `2` |
| `PORT` | Server port | `8082` |
| `PROXY_API_KEY` | Optional proxy authentication (auto-generated if empty) | (random) |

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

### Optimization Settings

These settings speed up Claude Code by mocking/skipping unnecessary requests:

| Variable | Description | Default |
| --- | --- | --- |
| `FAST_PREFIX_DETECTION` | Fast command prefix detection | `true` |
| `ENABLE_NETWORK_PROBE_MOCK` | Mock quota probe requests | `true` |
| `ENABLE_TITLE_GENERATION_SKIP` | Skip title generation requests | `true` |
| `ENABLE_SUGGESTION_MODE_SKIP` | Skip suggestion mode requests | `true` |
| `ENABLE_FILEPATH_EXTRACTION_MOCK` | Mock filepath extraction | `true` |
| `ENABLE_RECAP_SKIP` | Block recap requests (stepped away/return) | `true` |

See [`.env.example`](.env.example) for all options.

## API Endpoints

| Endpoint | Description |
| --- | --- |
| `POST /v1/messages` | Create a message (streaming) |
| `POST /v1/messages/buffered` | Create a message (buffered, with retry) |
| `POST /v1/messages/count_tokens` | Count tokens for a request |
| `GET /health` | Health check |
| `GET /status` | Server status |
| `POST /stop` | Stop all CLI sessions and pending tasks |

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

Logs are written to the console. For verbose output, check the terminal where the proxy is running.

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
DISCORD_BOT_TOKEN="your-bot-token-here"
DISCORD_GUILD_ID="123456789"               # Your server ID (comma-separated for multiple)
DISCORD_CONTROL_CHANNEL_ID="123456789"     # Admin channel for status (comma-separated)
DISCORD_CONVERSATION_CATEGORY_ID="123456789"  # Category for AI channels (comma-separated)
DISCORD_CONVERSATION_CHANNEL_ID=""         # Specific channel IDs (alternative to categories)
DISCORD_OWNER_ID="123456789"               # Your Discord user ID
DISCORD_OWNER_ONLY=true                    # true = owner only, false = anyone in server
DISCORD_AUTO_COMPACT=true                  # true = summarize/restart, false = drop oldest messages
```

**Channel Configuration:**
- **Categories**: Bot responds in any channel under `DISCORD_CONVERSATION_CATEGORY_ID`
- **Specific Channels**: Bot only responds in `DISCORD_CONVERSATION_CHANNEL_ID` channels
- **Both**: Can combine (bot responds in specified channels OR channels in categories)

### Bot Commands

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

### Features

- **Multi-server support**: Configure multiple guilds/servers with comma-separated IDs
- **Rate limiting**: Per-user cooldown and server-wide limits
- **Conversation modes**:
  - `DISCORD_AUTO_COMPACT=true` (default): Summarizes and restarts conversation when token limit reached
  - `DISCORD_AUTO_COMPACT=false`: Silently drops oldest messages to make room for new ones
- **Message splitting**: Automatically splits long responses for Discord's 2000 char limit
- **Command toggles**: Disable individual slash commands via `DISCORD_CMD_*` settings

## MCP Server Mode (Web Search Tools)

NIMbus can also run as an MCP (Model Context Protocol) server, exposing web search and page fetch tools directly to Claude Code. This allows Claude to search the web and fetch page content without going through the NVIDIA NIM proxy.

### Quick Start

```bash
# Add to Claude Code (using venv python)
claude mcp add websearch -- /path/to/NIMbus/.venv/bin/python /path/to/NIMbus/start_server.py --mcp
```

### MCP Tools

| Tool | Description | Parameters |
|------|-------------|------------|
| `web_search` | Search the web using DuckDuckGo | `query` (string) |
| `fetch_page` | Fetch and extract text from a webpage | `url` (string), `max_chars` (int, default: 10000) |

### Running MCP Server Manually

```bash
# Development mode
python start_server.py --mcp

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
```

### Using with Claude Code

Once added via `claude mcp add websearch ...`, Claude will have access to `web_search` and `fetch_page` tools. Example usage in Claude:

```
> Can you search for "latest Rust async patterns" and fetch the first result?
```

Claude will automatically call the MCP tools and return the results.

---

## Changelog

### v2.0.0 (June 2026)

**Standalone .exe:** NIMbus is now a single portable executable on Windows - no Python, no pip, no venv needed.
- `nimbus.exe --init`: Interactive setup wizard with live API key validation, model selection, Claude Code auto-config
- `nimbus.exe --init restore`: Restores backed-up settings.json
- Auto-creates `.env` from embedded template on first run
- Single `--onefile` PyInstaller build (~25 MB)

**Dynamic model resolution:** `MODEL=windows:settings.json` reads models from Claude Code's settings.json - no duplication. Model names are resolved dynamically against NVIDIA's catalog.

**Error recovery:**
- Auto-detects models that reject `system` role and retries with system→user conversion
- Detailed error logging with full causal chain
- Tiktoken special token handling (`<|endoftext|>`, `<|fim_prefix|>`, etc.)
- Fixed HTTP transport request attribution (OpenAI SDK retry compatibility)

**Per-tier model config:** Sonnet/Opus/Haiku each get their own model, mapped from Claude Code settings.json

## License

AGPL-3.0 - See [LICENSE](LICENSE) for details.

## Acknowledgments

- [NVIDIA NIM](https://build.nvidia.com/) for providing free API access
- [Claude Code](https://github.com/anthropics/claude-code) by Anthropic
- [FastAPI](https://fastapi.tiangolo.com/) for the web framework
