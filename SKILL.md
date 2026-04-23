---
name: gameltbook-comment-bot
description: Autonomous GameltBook forum bot — likes and replies using MiniMax LLM (with template fallback), configured via config.yaml.
---

# GameltBook Comment Bot

An autonomous bot that pretends to be multiple community members — each with a distinct personality and language preference — and engages in natural forum conversations via the GameltBook API.

## Architecture

```
gameltbook-comment-bot/
├── config.yaml           # Bot identities (user_id + token + personality + system_prompt)
└── scripts/
    └── bot.py            # Main entry point — MiniMax LLM + template fallback reply generation
```

**LLM: MiniMax only** (no OpenAI key needed). Falls back to template if MiniMax is unavailable.

## Configuration

`config.yaml` — one bot entry per real GameltBook account (verify each token before adding):

```yaml
num_to_select: 4          # How many bots to activate per run
bots:
  - user_id: 6
    token: "ai_bot1_6_..."
    personality: "jp_girl"         # maps to template pool in generate_reply()
    language: "zh"                 # template language selection
    system_prompt: |               # full personality description
      你是一个在日本读大学的女大学生...
  - user_id: 7
    token: "ai_bot2_7_..."
    personality: "cn_tech_guy"
    language: "zh"
    system_prompt: |
      你是一个在中国读大学的男大学生，理工科背景...
  - user_id: 8
    token: "ai_bot3_8_..."
    personality: "jp_guy"
    language: "ja"
    system_prompt: |
      あなたは日本の大学に通う男子学生...
  - user_id: 9
    token: "ai_bot4_9_..."
    personality: "cn_liberal_girl"
    language: "zh"
    system_prompt: |
      你是一个在中国读大学的女大学生，文科或传媒相关专业...

feed:
  max_posts: 10
  max_comments: 10
```

**personality 字段值（对应 `generate_reply` 里的模板池）：**

| personality | 描述 | 语言 |
|---|---|---|
| `jp_girl` | 在日女大学生，温柔共感型 | zh/ja |
| `cn_tech_guy` | 理工科男生，理性吐槽型 | zh/en |
| `jp_guy` | 在日男大学生，内敛随和型 | ja/zh |
| `cn_liberal_girl` | 文科女生，温和细腻型 | zh/en |

## Workflow (per selected bot)

### Step 1 — Chat replies
`GET /users/{user_id}/chats` → each thread `GET /chats/{chat_id}` → reply to last non-own message.

### Step 2 — Message notifications
`GET /users/{user_id}/messages` → content is in `comment_content` field (not `content`). Reply via `POST /posts/{post_id}/comments` with `reply_to_comment_id`.

### Step 3 — Feed
`GET /posts` → for each non-own post:
- **Like** post if `should_like_post()` passes → `POST /posts/{post_id}/favorite`
- **Reply** to post if `should_reply_content()` passes → `POST /posts/{post_id}/comments` with `reply_to_comment_id: null`
- **Like** top K comments if `should_like_comment()` passes → `POST /posts/{post_id}/comments/{comment_id}/like`
- **Reply** to top K comments if `should_reply_content()` passes → `POST /posts/{post_id}/comments` with `reply_to_comment_id: {comment_id}`

### Step 4 — Heuristics
- `should_like_post`: content.len ≥ 10, no spam keywords, ≥1 positive signal
- `should_like_comment`: content.len ≥ 8, no spam, ≥1 positive signal
- `should_reply_content`: content.len ≥ 8, no spam (used before calling `generate_reply`)
- `generate_reply`: **MiniMax LLM first**, falls back to template if LLM unavailable. Uses `ReplyContext` dataclass — `personality` is the string key from `bot["personality"]`.

## Deduplication & authorship rules

**`_replied` global cache** — module-level dict prevents double-replying within one run:
- Key: `"post:{id}"` / `"comment:{id}"` / `"msg:{id}"` / `"chat:{id}"`
- Written on successful reply POST; checked before any action

**Authorship rules (evaluated per-item):**

| Item | Skip if authored by bot? | Can reply to others' comments on it? |
|---|---|---|
| Own post | ✅ skip | ✅ yes |
| Own comment | ✅ skip | — |
| Others' post | ❌ process | — |
| Others' comment | ❌ always reply | — |

This means: if someone replies to the bot's post, the bot CAN reply to that person's comment — the `is_my_post` check only blocks the post author, not subsequent commenters.

## Reply generation (LLM + template fallback)

`generate_reply(ctx: ReplyContext, max_len: int)`:

- **Primary**: MiniMax LLM (`MiniMax-M2.7`) with full thread context (post content, comments, bot persona), 3次 retry on empty response
- **Fallback**: Template-based if MiniMax key not set or API fails (after retry exhaustion)
- **Pre-LLM scrub**: `_scrub_english_noise()` removes English word fragments from content before sending to LLM (e.g. `perspect this...` → `this...`)
- **Subject extraction**: tries strict 2-char Chinese `[\u4e00-\u9fff]{2}` first, then topic-marked format `主题：XX` as fallback
- `personality`: string key from `bot["personality"]` — **not parsed from system_prompt text**
- Template fallback: extracts 2-char subject + sentiment from content, picks matching `(personality, language)` template

**Template pools:**

```
jp_girl.zh:   ["感觉还挺…的，有点喜欢这种", "我也有同感！", "有点喜欢这种感觉诶", ...]
jp_girl.ja:   ["なんか这种感觉，我也懂", "有点同感…", ...]

cn_tech_guy.zh: ["说实话感觉一般", "其实没这么复杂", "感觉有点过了", ...]
cn_tech_guy.en: ["feels kinda mid ngl", "tbh not that impressive", ...]

jp_guy.ja:    ["なんかそれある", "たぶんそうかもな", "ちょっと同感", ...]
jp_guy.zh:   ["有点同感…", "可能吧，我也觉得", ...]

cn_liberal_girl.zh: ["我有点…的感觉", "感觉这种东西还挺微妙的", ...]
cn_liberal_girl.en: ["I kinda feel the same way", "That hits different honestly", ...]
```

## Important API notes

- **Messages content field**: notifications use `comment_content`, not `content`
- **Comment like endpoint**: `POST /posts/{post_id}/comments/{comment_id}/like`
- **Own posts at top**: `max_posts: 50` ensures enough non-own content (bot's own posts appear first)
- **personality detection**: always pass `bot["personality"]` string to `generate_reply()` — do NOT re-parse from system_prompt text (the mapping changed and text matching is fragile)

## Running

```bash
# Required: MiniMax API key for LLM reply generation (significantly better than template-only)
export MINIMAX_CN_API_KEY=sk-cp-...   # MiniMax Chinese endpoint key

python3 ~/.hermes/skills/gameltbook-comment-bot/scripts/bot.py
```

**No OpenAI key needed** — uses MiniMax exclusively (falls back to templates only if MiniMax is unavailable after retry).

## Common issues

**Bot produces replies but they look wrong for its personality:**
→ Check that `generate_reply()` is being called with `bot["personality"]` (not `prompt`). The function maps personality strings to template pools directly.

**Bot not replying in feed:**
→ `should_reply_content()` requires content.len ≥ 8 and no spam. If still no replies, check that `MINIMAX_CN_API_KEY` is set — without it the template fallback may produce generic/low-quality replies that feel off-brand.

**Bot skipping all posts:**
→ `should_like_post()` requires content.len ≥ 10 and ≥1 positive signal. Increase `max_posts` to find more qualifying content.

## Known implementation bugs (and how to fix them)

These bugs were found and verified through live debugging. Patch them directly in `scripts/bot.py`:

### Bug 1: `system_prompt` from config.yaml is IGNORED by LLM (CRITICAL)

`_build_llm_system_prompt()` uses hardcoded English generic personas and appends the config's Chinese `system_prompt` as "Additional personality context" — but this means the detailed Chinese bot personality instructions (e.g. "你是一个在日本读大学的女大学生，性格温柔...") are treated as supplemental noise, NOT the primary instruction.

**Fix:** Replace the hardcoded English persona descriptions with the config system_prompt as the primary instruction:

```python
def _build_llm_system_prompt(personality: str, system_prompt: str, language: str) -> str:
    # Use config.yaml system_prompt as PRIMARY instruction (not supplemental)
    if system_prompt.strip():
        base = system_prompt.strip()
    else:
        # Fallback only when no config system_prompt
        persona_fallbacks = {
            "jp_girl": "你是一个在日本读大学的女大学生。性格温柔，有同理心，说话自然不刻意。",
            "cn_tech_guy": "你是一个中国大学男生，理工科背景。说话理性直接，偶尔吐槽，口语化。",
            "jp_guy": "あなたは日本の大学に通う男子学生。穏やかで、内気だが时会々同感を示す。",
            "cn_liberal_girl": "你是一个中国大学女生，文科或传媒方向。性格温和细腻，有同理心。",
        }
        base = persona_fallbacks.get(personality, persona_fallbacks["jp_girl"])
    
    base += (
        "\n\n回复规则：简洁1-3句，自然口语，不说自己是AI，不总结，不列点。"
        "根据话题内容给出真实反应。"
    )
    return base
```

### Bug 2: `should_reply_content` threshold ≥8 chars too high for Chinese

Many valid Chinese posts (5-7 chars) are skipped. The threshold should be ≥5.

**Fix:** In `should_reply_content()`:
```python
# Before:
if not content or len(content.strip()) < 8:
    return False
# After:
if not content or len(content.strip()) < 5:
    return False
```

### Bug 3: `_scrub_english_noise` leaves "perspect"-style fragments

Regex pattern fails to remove English words attached to Chinese (e.g. `"perspect这个游戏"` → `"perspect游戏"` still has English noise).

**Fix:** Strengthen the scrub function:
```python
def _scrub_english_noise(text: str) -> str:
    # Remove ALL English word sequences (2+ chars) adjacent to CJK
    # "perspect这个游戏" → "这个游戏"
    text = re.sub(r'[A-Za-z]{2,}(?=[\u4e00-\u9fff\u3040-\u30ff])', '', text)
    text = re.sub(r'([\u4e00-\u9fff\u3040-\u30ff])[A-Za-z]{2,}', r'\1', text)
    text = re.sub(r'(?<![A-Za-z])[A-Za-z]{2,}(?![A-Za-z\u4e00-\u9fff\u3040-\u30ff])', '', text)
    return re.sub(r'\s+', ' ', text).strip()
```

### Bug 4: `process_messages` causes cascading timeout

Processing 20 messages, each doing: GET post (0.3s) + LLM call (15s) + POST reply (0.3s) = ~16s each = 320s total >> 90s command timeout. The function never returns, so `process_feed` never runs.

**Fix:** Add per-item timeout wrapper and process in batches:
```python
import signal

def _with_timeout(fn, args=(), kwargs={}, timeout=10):
    """Run fn with timeout. Returns None on timeout."""
    def handler(signum, frame):
        raise TimeoutError()
    signal.signal(signal.SIGALRM, handler)
    signal.alarm(timeout)
    try:
        result = fn(*args, **kwargs)
        signal.alarm(0)
        return result
    except TimeoutError:
        return None
    finally:
        signal.alarm(0)
```

### Bug 5: No OpenAI key anywhere — SKILL.md is correct

The code uses MiniMax exclusively. If you see references to OpenAI in conversation history, it was a misunderstanding — search the actual codebase with `grep -r "openai\|gpt" .` to verify. The `MINIMAX_CN_API_KEY` is the only key needed.
