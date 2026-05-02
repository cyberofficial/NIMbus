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

### Prerequisites

1. NVIDIA NIM API key: [build.nvidia.com/settings/api-keys](https://build.nvidia.com/settings/api-keys)
2. [Claude Code](https://github.com/anthropics/claude-code) installed
3. Python 3.10+ installed

### Setup

```bash
git clone https://github.com/cyberofficial/NIMbus.git
cd NIMbus
cp .env.example .env
```

Edit `.env`:

```dotenv
NVIDIA_NIM_API_KEY="nvapi-your-key-here"
MODEL="z-ai/glm5"
```

The MODEL format is `owner/model-name`.

### Virtual Environment

**Option 1: Using uv (Recommended)**

If you have [uv](https://github.com/astral-sh/uv) installed:

```bash
# uv automatically manages the virtual environment
uv run uvicorn server:app --host 0.0.0.0 --port 8082
```

**Option 2: Using Python venv**

If you don't have `uv`, use Python's built-in `venv`:

```bash
# Create a virtual environment
python -m venv .venv

# Activate it
# On Linux/macOS:
source .venv/bin/activate
# On Windows:
.venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run the server
uvicorn server:app --host 0.0.0.0 --port 8082
```

### Run

**Terminal 1 - Start the proxy:**

```bash
# Using uv (recommended):
uv run uvicorn server:app --host 0.0.0.0 --port 8082

# Or using venv (after activating):
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

| Model | Use Case | Context |
| --- | --- | --- |
| `z-ai/glm5` | Coding and technical tasks | 202K |
| `deepseek-ai/deepseek-v4-flash` | Fast, reasoning-enabled | 128K |
| `deepseek-ai/deepseek-v4-pro` | Full power, no thinking | 128K |
| `qwen/qwen3.5-397b-a17b` | Large general purpose | 32K |

## Configuration

| Variable | Description | Default |
| --- | --- | --- |
| `MODEL` | Model identifier (`owner/model-name`) | `z-ai/glm5` |
| `NVIDIA_NIM_API_KEY` | NVIDIA API key | **required** |
| `NIM_MAX_TOKENS` | Max tokens for responses | `202000` |
| `NIM_THINKING` | Enable thinking/reasoning content | `true` |
| `NIM_REASONING_EFFORT` | Reasoning effort: `low`, `medium`, or `high` | `high` |
| `PROVIDER_RATE_LIMIT` | Requests per window | `40` |
| `PROVIDER_RATE_WINDOW` | Rate window in seconds | `60` |
| `PROVIDER_MAX_CONCURRENCY` | Max concurrent streams | `5` |
| `HTTP_READ_TIMEOUT` | Read timeout in seconds | `300` |
| `HTTP_WRITE_TIMEOUT` | Write timeout in seconds | `10` |
| `HTTP_CONNECT_TIMEOUT` | Connect timeout in seconds | `2` |
| `PORT` | Server port | `8082` |
| `PROXY_API_KEY` | Optional proxy authentication (auto-generated if empty) | (random) |

### Stream vs Buffer Modes

NIMbus has two server modes controlled by `SERVER_TYPE`. Both produce Anthropic-format responses compatible with Claude Code, but they trade off latency for reliability differently.

#### Stream Mode (`SERVER_TYPE=stream` — default)

Tokens are relayed to Claude Code as NVIDIA generates them, just like a direct connection.

- **Lowest latency** — Claude Code sees tokens immediately
- **What happens during backend cutout**: The proxy sends a partial response with `stop_reason="max_tokens"` and logs a warning. Claude Code receives whatever was generated before the interruption.
- **No retry** — streaming cannot replay already-sent tokens, so a dropped connection means a partial response.
- **Best for** interactive use where you want to see output as it's produced.

```
Claude Code ──── SSE stream ──── NIMbus ──── SSE stream ──── NVIDIA NIM
              (live tokens)               (live tokens)
```

If NVIDIA's backend cuts out mid-stream, the `SSEBuilder.truncated` flag is set and the final `message_delta` event carries `stop_reason: "max_tokens"`.

#### Buffer Mode (`SERVER_TYPE=buffer`)

The proxy waits for NVIDIA to finish generating the **complete** response before sending anything to Claude Code. If the backend drops the connection, the proxy automatically retries.

- **Higher latency** — Claude Code waits until the full response is ready
- **Automatic retry with exponential backoff** on connection loss (`APIConnectionError`) and timeouts (`APITimeoutError`)
- **Configurable retry behavior**:
  | Setting | Default | What it does |
  |---|---|---|
  | `PROVIDER_RETRY_ON_TRUNCATION` | `3` | Number of retry attempts before giving up |
  | `PROVIDER_RETRY_DELAY` | `1.0` | Base delay between retries (seconds) — multiplies by attempt number |
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

## License

AGPL-3.0 - See [LICENSE](LICENSE) for details.

## Acknowledgments

- [NVIDIA NIM](https://build.nvidia.com/) for providing free API access
- [Claude Code](https://github.com/anthropics/claude-code) by Anthropic
- [FastAPI](https://fastapi.tiangolo.com/) for the web framework
