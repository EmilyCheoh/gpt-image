# astrbot_plugin_gpt_image

GPT Image plugin for AstrBot — generates images via OpenAI-compatible API endpoints.

## Features

- **LLM tool** (`generate_image`): LLM automatically calls this when Felis Abyssalis asks to draw/generate an image. Translates the request into a detailed English prompt.
- **Direct command** (`/image_gen {prompt}`): Bypasses LLM entirely. Send the prompt wrapped in `{}` to generate an image directly.
- Supports two API formats:
  - `images` — standard `/v1/images/generations` endpoint
  - `chat` — `/v1/chat/completions` endpoint (extracts image URL from response)
  - `auto` — tries `images` first (15s probe), falls back to `chat`

## Usage

### Via LLM (automatic)
Just ask Abyss AI to draw something in conversation. The LLM decides when to call the tool.

### Via command (manual)
```
/image_gen {a beautiful cat sitting on the moon at midnight}
```

## Configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `api_key` | string | `""` | API Key |
| `api_base` | string | `""` | API base URL |
| `model` | string | `gpt-image-2` | Model name |
| `api_format` | string | `images` | `images`, `chat`, or `auto` |
| `timeout` | int | `240` | Request timeout in seconds |

## Credits

Original plugin by **Kai**.
