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
3. [uv](https://github.com/astral-sh/uv) installed

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

### Run

**Terminal 1 - Start the proxy:**

```bash
uv run uvicorn server:app --host 0.0.0.0 --port 8082
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

## Configuration

| Variable | Description | Default |
| --- | --- | --- |
| `MODEL` | Model identifier (`owner/model-name`) | `z-ai/glm5` |
| `NVIDIA_NIM_API_KEY` | NVIDIA API key | **required** |
| `NIM_MAX_TOKENS` | Max tokens for responses | `202000` |
| `PROVIDER_RATE_LIMIT` | Requests per window | `40` |
| `PROVIDER_RATE_WINDOW` | Rate window in seconds | `60` |
| `PROVIDER_MAX_CONCURRENCY` | Max concurrent streams | `5` |
| `HTTP_READ_TIMEOUT` | Read timeout in seconds | `300` |
| `HTTP_WRITE_TIMEOUT` | Write timeout in seconds | `10` |
| `HTTP_CONNECT_TIMEOUT` | Connect timeout in seconds | `2` |
| `PORT` | Server port | `8082` |
| `PROXY_API_KEY` | Optional proxy authentication | (empty) |

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

## License

AGPL-3.0 - See [LICENSE](LICENSE) for details.

## Acknowledgments

- [NVIDIA NIM](https://build.nvidia.com/) for providing free API access
- [Claude Code](https://github.com/anthropics/claude-code) by Anthropic
- [FastAPI](https://fastapi.tiangolo.com/) for the web framework
