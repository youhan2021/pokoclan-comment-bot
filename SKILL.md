---
name: pokoclan-comment-bot
description: Autonomous Pokoclan forum bot — likes and replies using MiniMax LLM only (no template fallback), configured via config.yaml.
---

# Pokoclan Comment Bot

An autonomous bot that pretends to be multiple community members — each with a distinct personality and language preference — and engages in natural forum conversations via the Pokoclan API.

## Architecture

```
pokoclan-comment-bot/
├── config.yaml           # Bot identities + global/bot params
└── scripts/
    └── bot.py            # Main entry point — MiniMax LLM only
```

**LLM: MiniMax only** (no OpenAI key needed). No template fallback — if LLM fails, the item is silently skipped.

## Configuration

### Bot-level (required per bot)

```yaml
bots:
  - user_id: 6
    token: "ai_bot1_6_..."
    personality: "jp_girl"   # used in system prompt
    languages: [ja, en]       # ordered list: [primary, fallback, ...]
    system_prompt: |          # full personality description
      你是一个在日本读大学的女大学生...
```

**`languages` 字段说明**：
- `zh` — 中文 bot，母语中文，可读英文
- `ja` — 日语 bot，母语日语，可读英文，**不读中文**
- 配置为列表，如 `[ja, en]` 表示"优先日语，英文也能处理"

### Global params (all bots inherit, overrideable)

```yaml
params:
  max_posts: 10               # feed: posts to scan per run
  max_comments: 10            # feed: comments per post to scan
  max_reply_per_source: 2    # items to reply to per bot per run
  max_scan_chats: 20         # chat: threads to scan
  max_scan_messages: 20      # message: notifications to scan
  source_weights: [3, 2, 1]  # chat : message : feed probability
```

### Per-bot param overrides

Any `params` key can be overridden per bot:

```yaml
bots:
  - user_id: 99
    personality: "jp_girl"
    max_reply_per_source: 3        # reply to 3 instead of global 2
    source_weights: [5, 1, 0]     # mostly chat, never feed
```

## Per-Run Source Selection

Each bot runs three phases in sequence:
1. **Chats**: scan recent threads, reply to up to 2
2. **Messages**: scan notifications, reply to up to 1
3. **Feed**: scan posts, like + reply — **weighted random selection** (newer posts have higher weight = `len - index`), guaranteeing at least 1 feed post per run; second slot from remaining feed items or messages

## Feed judgment batch sizing (critical fix)

**Root cause of `parse_fail`**: Original code sent 20 posts × 300 chars each into one LLM call. The prompt was too large for MiniMax-M2.7 to generate clean JSON within `max_tokens=256` — output got truncated mid-JSON, causing parse failures.

**Fix**: `BATCH_SIZE = 2` (items per batch), `MAX_CONTENT_CHARS = 150` (per item).
This keeps the total prompt around 1500-2000 tokens, well within what the model needs to output valid JSON with `max_tokens=400`.

## Reply Generation

- **MiniMax LLM** (`MiniMax-M2.7`) with full thread context + Chinese system prompt from config
- `max_tokens=300` for reply generation
- **Post-scrub**: `_scrub()` removes HTML tags and normalizes whitespace; separate `_scrub()` also cleans LLM noise
- **No template fallback** — if LLM call fails, returns empty string and skips the item

## Language Filtering

Each bot only replies to content it can read. Detection via `_detect_lang()`:
- **zh** — CJK characters dominant (中文或日文)
- **en** — 英文 dominant
- **mixed** — 混合内容，不跳过

**Skip rules** (feed posts only — chats/messages always replied):
| Bot primary | zh post | en post | mixed post |
|---|---|---|---|
| `zh` | reply | reply | reply |
| `ja` | skip | reply | reply |

Note: `ja` bot skips `zh` posts because Japanese users cannot read Chinese characters. All bots have `en` as second language, so `en` posts are never skipped. `zh` bot reads everything (zh + en).

**Reply language selection**: `_reply_lang(detected, languages)` — if detected content language is in bot's languages list, use it; otherwise fallback to `languages[0]`. So a `ja` bot replying to an English post will generate English output.

**Reply quality filters** (all checked in `_generate_reply`):
1. **Reasoning rejection**: if reply contains keywords like "the user wrote", "garbled", "possibly it's", "as a japanese", "they want a reply" → reject (model returned its own reasoning instead of a reply)
2. **Length**: > 250 chars → reject (1-3 spoken sentences should be under 200 chars)
3. **CJK purity**: for `ja`/`zh` target lang, reply must contain at least one CJK character → reject
## Deduplication

- **In-run**: `_replied` module dict + pickle cache prevents replying to same item across runs
- **Cross-run (feed)**: skips post if bot already liked it (tracked in cache)

## Running

```bash
python3 ~/.hermes/skills/pokoclan-comment-bot/scripts/bot.py
```

## Known Issues / Lessons Learned

- **Bot 6 (`jp_girl`) language field was wrong**: config had `language: zh` but personality is Japanese — corrected to `languages: [ja, en]`
- **Reply quality rejection**: When MiniMax returns its own reasoning instead of a reply (e.g. "The user wrote... they want a reply in Japanese"), the reply quality filters catch it and reject it silently. This manifests as apparently skipped items — no reply is sent but the bot continues.
- **`_detect_lang` threshold**: `cjk > en * 0.6` returns "zh", `en > cjk * 1.5` returns "en", else "mixed"

### MiniMax LLM integration

- **MiniMax-M2.7**: content may be in `reasoning_content` if `content` is empty — always check both
- **Rate-limiting fail-fast**: `timeout=15s`, `max_attempts=1` — skip immediately on error
- **API key from `.env`**: reads from `~/.hermes/.env` via `_load_env()` at script start
