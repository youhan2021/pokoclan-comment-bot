---
name: gameltbook-comment-bot
description: Autonomous GameltBook forum bot — likes and replies using template-based generation (no LLM API needed), configured via config.yaml.
---

# GameltBook Comment Bot

An autonomous bot that pretends to be multiple community members — each with a distinct personality and language preference — and engages in natural forum conversations via the GameltBook API.

## Architecture

```
gameltbook-comment-bot/
├── config.yaml           # Bot identities (user_id + token + personality + system_prompt)
└── scripts/
    └── bot.py            # Main entry point — template-based reply generation
```

**No external API keys needed** — reply generation is entirely template-based.

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

### Step 4 — Heuristics (no LLM)
- `should_like_post`: content.len ≥ 10, no spam keywords, ≥1 positive signal
- `should_like_comment`: content.len ≥ 8, no spam, ≥1 positive signal
- `should_reply_content`: content.len ≥ 30, no spam (used before calling `generate_reply`)
- `generate_reply`: **template-based**, personality is passed as a string key directly (e.g. `"jp_girl"`, not parsed from system_prompt). No API call needed.

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

## Reply generation (template-based)

`generate_reply(source_content, personality, language, context, max_len)`:

- `personality`: string key directly from `bot["personality"]` — **not parsed from system_prompt text**
- Picks a random template from the pool matching `(personality, language)`, fills in `{keyword}` from content
- Keyword = 3rd word of content (or last word if shorter)

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
python3 ~/.hermes/skills/gameltbook-comment-bot/scripts/bot.py
```

## Common issues

**Bot produces replies but they look wrong for its personality:**
→ Check that `generate_reply()` is being called with `bot["personality"]` (not `prompt`). The function maps personality strings to template pools directly.

**Bot not replying in feed:**
→ `should_reply_content()` requires content.len ≥ 30 and no spam. Many short Chinese posts may not pass.

**Bot skipping all posts:**
→ `should_like_post()` requires content.len ≥ 10 and ≥1 positive signal. Increase `max_posts` to find more qualifying content.
