#!/usr/bin/env python3
"""
Pokoclan Comment Bot — v2.
Flow per bot:
  1. Reply to unread chats (up to 2)
  2. Randomly pick 2 from [messages feed] combined
     - If message: reply as comment
     - If feed post: like it, then reply-to-post OR reply-to-a-comment
  3. LLM calls = 2 (chat) + 2 (message/feed reply) per bot
"""
import json, os, random, re, subprocess, sys, time
from dataclasses import dataclass
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
CONFIG_PATH = os.path.join(SKILL_DIR, "config.yaml")
API_SCRIPT = "/home/ubuntu/.hermes/skills/pokoclan-api/scripts/gameltbook_api.py"
BASE_URL = "https://api.pokoclan.com"

# Load .env
_HERMES_HOME = os.environ.get("HERMES_HOME", os.path.join(os.path.expanduser("~"), ".hermes"))
_ENV_PATH = os.path.join(_HERMES_HOME, ".env")
if os.path.exists(_ENV_PATH):
    for line in open(_ENV_PATH).read().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k and v and k not in os.environ:
            os.environ[k] = v.strip()

_MINIMAX_API_KEY = os.environ.get("MINIMAX_CN_API_KEY", os.environ.get("MINIMAX_API_KEY", "")).strip()
_MINIMAX_BASE_URL = "https://api.minimaxi.com/v1"


# ── LLM ─────────────────────────────────────────────────────────────────────────

def _minimax_chat(system_prompt: str, user_prompt: str,
                  model: str = "MiniMax-M2.7",
                  temperature: float = 0.7,
                  max_tokens: int = 200,
                  timeout: int = 15) -> str | None:
    if not _MINIMAX_API_KEY:
        return None
    import ssl, urllib.error, urllib.request
    url = f"{_MINIMAX_BASE_URL}/text/chatcompletion_v2"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt, "name": "MiniMax AI"},
            {"role": "user",   "content": user_prompt,   "name": "用户"},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    headers = {"Authorization": f"Bearer {_MINIMAX_API_KEY}", "Content-Type": "application/json"}
    context = ssl._create_unverified_context()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, context=context, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        result = json.loads(raw)
        base = result.get("base_resp", {})
        if base.get("status_code", 0) != 0:
            print(f"  [MiniMax] status {base.get('status_code')}", flush=True)
            return None
        choices = result.get("choices", [])
        if choices:
            msg = choices[0].get("message", {})
            content = msg.get("content", "") or msg.get("reasoning_content", "")
            if content and content.strip():
                return content.strip()
    except Exception as e:
        print(f"  [MiniMax] error: {e}", flush=True)
    return None


# ── Reply cache ────────────────────────────────────────────────────────────────

_replied = {}

def _load_cache():
    global _replied
    cache_path = os.path.join(SCRIPT_DIR, ".reply_cache.pkl")
    import pickle
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as f:
                _replied = pickle.load(f)
        except Exception:
            _replied = {}

def _save_cache():
    import pickle
    cache_path = os.path.join(SCRIPT_DIR, ".reply_cache.pkl")
    try:
        with open(cache_path, "wb") as f:
            pickle.dump(_replied, f)
    except Exception as e:
        print(f"  [cache] save failed: {e}")

def _reply_key(uid: int, kind: str, item_id: int | str) -> str:
    return f"uid:{uid}:{kind}:{item_id}"


# ── API helper ────────────────────────────────────────────────────────────────

def api(method, endpoint, token, user_id=None, data=None):
    cmd = ["python3", API_SCRIPT, method, f"{BASE_URL}{endpoint}",
           "--token", token, "--insecure"]
    if user_id is not None:
        cmd += ["--user-id", str(user_id)]
    if data is not None:
        cmd += ["--data", json.dumps(data, ensure_ascii=False)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        resp = json.loads(r.stdout)
    except Exception:
        print(f"  [API ERROR] {r.stdout[:200]} {r.stderr[:100]}")
        return None
    return resp.get("body") if resp.get("status", 0) < 400 else None


def api_raw(method, endpoint, token, user_id=None, data=None):
    """Return full response dict (includes status code)."""
    cmd = ["python3", API_SCRIPT, method, f"{BASE_URL}{endpoint}",
           "--token", token, "--insecure"]
    if user_id is not None:
        cmd += ["--user-id", str(user_id)]
    if data is not None:
        cmd += ["--data", json.dumps(data, ensure_ascii=False)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {"status": -1, "body": r.stdout[:200]}


# ── Config / bot selection ───────────────────────────────────────────────────

def load_config():
    import yaml
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

def select_bots(config):
    bots = config["bots"]
    n = min(config.get("num_to_select", 1), len(bots))
    return random.sample(bots, n)


# ── Text cleaning ────────────────────────────────────────────────────────────

def _clean(text):
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", re.sub(r"\s+", " ", text)).strip()

def _scrub(text):
    """Remove English noise from Chinese text."""
    text = re.sub(r'[A-Za-z]{2,}(?=[\u4e00-\u9fff\u3040-\u30ff])', '', text)
    text = re.sub(r'([\u4e00-\u9fff\u3040-\u30ff])[A-Za-z]{2,}', r'\1', text)
    text = re.sub(r'(?<![A-Za-z\u4e00-\u9fff\u3040-\u30ff])[A-Za-z]{2,}(?![A-Za-z\u4e00-\u9fff\u3040-\u30ff])', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def _detect_lang(text):
    """Detect if text is primarily Chinese, Japanese, or English."""
    cjk = len(re.findall(r'[\u4e00-\u9fff\u3040-\u30ff]', text))
    en = len(re.findall(r'[A-Za-z]', text))
    if cjk > en * 0.6:
        return "zh"    # Chinese or Japanese
    elif en > cjk * 1.5:
        return "en"
    else:
        return "mixed"


def _reply_lang(detected_lang, languages):
    """Choose reply language: use detected if bot can speak it, else fallback to primary."""
    if detected_lang in languages:
        return detected_lang
    return languages[0]


# ── Reply generation ─────────────────────────────────────────────────────────

def _persona_prompt(personality, system_prompt, lang):
    if system_prompt.strip():
        base = system_prompt.strip()
    else:
        fallback = {
            "jp_girl":       "你是一个在日本读大学的女大学生。性格温柔，有同理心，说话自然不刻意，偶尔会附和别人表达同感。",
            "cn_tech_guy":    "你是一个中国大学男生，理工科背景。说话理性直接，偶尔吐槽，口语化，不喜欢长篇大论。",
            "jp_guy":         "あなたは日本の大学に通う男子学生です。穏やかで，内気ですが，时々同感を示します。自然に短く返します。",
            "cn_liberal_girl":"你是一个中国大学女生，文科或传媒方向。性格温和细腻，有同理心，说话柔和但有主见。",
        }
        base = fallback.get(personality, fallback["jp_girl"])
    base += "\n\n回复规则：简洁1-3句，自然口语，不说自己是AI，不总结，不列点。"
    return base

_SYSTEM_PROMPTS = {
    "jp_girl":       "你是一个在日本读大学的女大学生。性格温柔，有同理心，说话自然不刻意，偶尔会附和别人表达同感。\n\n回复规则：简洁1-3句，自然口语，不说自己是AI，不总结，不列点。",
    "cn_tech_guy":   "你是一个中国大学男生，理工科背景。说话理性直接，偶尔吐槽，口语化，不喜欢长篇大论。\n\n回复规则：简洁1-3句，自然口语，不说自己是AI，不总结，不列点。",
    "jp_guy":        "あなたは日本の大学に通う男子学生です。穏やかで，内気ですが，时々同感を示します。自然に短く返します。\n\n回复规则：简洁1-3句，自然口语，不说自己是AI，不总结，不列点。",
    "cn_liberal_girl":"你是一个中国大学女生，文科或传媒方向。性格温和细腻，有同理心，说话柔和但有主见。\n\n回复规则：简洁1-3句，自然口语，不说自己是AI，不总结，不列点。",
}

def _build_prompt(ctx_type, content, post_content="", recent_comments=None, lang="zh"):
    recent = recent_comments or []
    lang_hint = {"zh": "回复语言：中文。", "ja": "回复语言：日本語。", "en": "Reply language: English."}.get(lang, "回复语言：中文。")
    lines = []
    if ctx_type == "chat":
        lines.append(f"回复这条私信：\n\"{_scrub(content)}\"")
    elif ctx_type == "notification":
        lines.append(f"别人在你的帖子下发了一条评论，现在你要回复这条评论：\n\"{_scrub(content)}\"")
        if post_content:
            lines.append(f"原帖内容：\n\"{_scrub(post_content)}\"")
    elif ctx_type == "comment":
        lines.append(f"回复这条评论：\n\"{_scrub(content)}\"")
        lines.append(f"所在帖子：\n\"{_scrub(post_content)}\"")
    else:  # post
        lines.append(f"回复这个帖子：\n\"{_scrub(content)}\"")
    if recent:
        lines.append(f"\n其他评论：\n" + "\n".join(f"- {_scrub(c)}" for c in recent[:3]))
    lines.append(f"\n{lang_hint}\n写1-3句自然、口语化的回复。")
    return "\n".join(lines)

def _generate_reply(ctx_type, content, post_content, recent_comments, personality, lang):
    sys_prompt = _SYSTEM_PROMPTS.get(personality, _SYSTEM_PROMPTS["jp_girl"])
    user_prompt = _build_prompt(ctx_type, content, post_content, recent_comments, lang)
    reply = _minimax_chat(sys_prompt, user_prompt, max_tokens=300, temperature=0.7)
    if not reply:
        return ""
    # Sanity check: reject if it looks like a reasoning/logging output
    reply_lower = reply.lower()
    if any(kw in reply_lower for kw in [
            "the user wrote", "garbled", "possibly it's", "the user request",
            "as a japanese", "but what is the appropriate",
            "they want a reply", "the content is random",
    ]):
        return ""
    # Reject if too long (1-3 spoken sentences should be < 200 chars)
    if len(reply) > 250:
        return ""
    # For CJK target lang, require at least some CJK characters
    if lang in ("ja", "zh") and not re.search(r'[\u4e00-\u9fff\u3040-\u30ff]', reply):
        return ""
    return reply


# ── Step 1: Chats ─────────────────────────────────────────────────────────────

def process_chats(bot):
    """Reply to up to 2 unread chats. Returns list of result strings."""
    uid = bot["user_id"]
    token = bot["token"]
    languages = bot["languages"]
    personality = bot["personality"]
    system_prompt = bot.get("system_prompt", "")

    data = api("GET", f"/users/{uid}/chats", token, uid)
    if not data:
        return []
    chats = data if isinstance(data, list) else data.get("chats", data.get("results", []))
    if not chats:
        return []

    results = []
    chat_count = 0
    for chat in chats[:6]:
        chat_id = chat.get("id")
        if not chat_id or _replied.get(_reply_key(uid, "chat", chat_id)):
            continue
        thread = api("GET", f"/chats/{chat_id}", token, uid)
        if not thread:
            continue
        messages = thread if isinstance(thread, list) else thread.get("messages", thread.get("results", []))
        if not messages:
            continue
        last = messages[-1]
        last_sender = last.get("author", {}).get("account_id") or last.get("account_id")
        last_content = _clean(last.get("content", ""))
        if last_sender == uid or len(last_content) < 3:
            continue

        reply = _generate_reply("chat", last_content, "", [], personality, languages[0])
        if not reply:
            continue
        ok = api("POST", f"/chats/{chat_id}/messages", token, uid,
                 data={"user_id": uid, "content": reply})
        if ok:
            results.append(f"chat→{chat_id}: {reply[:50]}")
            _replied[_reply_key(uid, "chat", chat_id)] = time.time()
            chat_count += 1
            if chat_count >= 2:
                break
    return results


# ── Step 2: Messages ──────────────────────────────────────────────────────────

def process_messages(bot):
    """Fetch recent message notifications (not chats). Returns list of dicts."""
    uid = bot["user_id"]
    token = bot["token"]

    data = api("GET", f"/users/{uid}/messages", token, uid)
    if not data:
        return []
    messages = data if isinstance(data, list) else data.get("messages", data.get("results", []))
    items = []
    for msg in messages[:15]:
        msg_id = msg.get("id")
        if not msg_id or _replied.get(_reply_key(uid, "msg", msg_id)):
            continue
        content = _clean(msg.get("comment_content", "") or msg.get("content", ""))
        if not content or len(content) < 3:
            continue
        post_id = msg.get("post_id")
        # Fetch post content if we have a post_id
        post_content = ""
        if post_id:
            post_data = api("GET", f"/posts/{post_id}", token, uid)
            if post_data:
                post_content = _clean(post_data.get("content", "") or "")
        items.append({
            "id": msg_id,
            "post_id": post_id,
            "comment_id": msg.get("comment_id"),
            "content": content,
            "post_content": post_content,
        })
    return items


# ── Step 3: Feed ──────────────────────────────────────────────────────────────

def process_feed(bot):
    """Fetch recent posts. Returns list of (post_id, content)."""
    uid = bot["user_id"]
    token = bot["token"]

    data = api("GET", "/posts", token, uid)
    if not data:
        return []
    posts = data if isinstance(data, list) else data.get("posts", data.get("results", []))
    items = []
    for post in posts[:30]:
        post_id = post.get("id")
        if not post_id or _replied.get(_reply_key(uid, "post", post_id)):
            continue
        author = post.get("author", {})
        author_id = author.get("account_id") or author.get("user_id")
        if author_id == uid:
            continue
        content = _clean(post.get("content", ""))
        if not content or len(content) < 5:
            continue
        items.append({"id": post_id, "content": content})
    return items


# ── Like ──────────────────────────────────────────────────────────────────────

def like_post(bot, post_id):
    """Like a post (skip if already liked)."""
    uid = bot["user_id"]
    token = bot["token"]
    if _replied.get(_reply_key(uid, "like", post_id)):
        return False
    raw = api_raw("POST", f"/posts/{post_id}/favorite",
                   token, uid, data={"user_id": uid})
    if raw.get("status") == 200:
        _replied[_reply_key(uid, "like", post_id)] = time.time()
        return True
    return False


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    _load_cache()
    print(f"[{datetime.now(timezone.utc).isoformat()}] Bot starting... LLM={'MiniMax' if _MINIMAX_API_KEY else 'NONE'}")

    config = load_config()
    bots = select_bots(config)
    print(f"Selected {len(bots)} bots: {[b['user_id'] for b in bots]}")

    for bot in bots:
        uid = bot["user_id"]
        personality = bot["personality"]
        languages = bot["languages"]
        lang = languages[0]
        token = bot["token"]
        print(f"\n--- Bot {uid} ({personality}) ---")

        # Step 1: Chats
        print("  [chats]...")
        chat_results = process_chats(bot)
        for r in chat_results:
            print(f"  ✓ {r}")
        if not chat_results:
            print("  (no chat replies)")

        # Step 2: Collect messages + feed
        # Always pick at least 1 feed post (newest), fill 2nd slot randomly from remaining
        messages = process_messages(bot)
        feed_items = process_feed(bot)

        selected = []
        # Weighted random: newer posts have higher probability
        # Build weighted pool: each feed item gets weight = len - index
        if feed_items:
            weighted_feed = [("feed", f, len(feed_items) - i) for i, f in enumerate(feed_items)]
            total_weight = sum(w for _, _, w in weighted_feed)
            r = random.uniform(0, total_weight)
            cum = 0
            chosen_feed = None
            for item, f, w in weighted_feed:
                cum += w
                if r <= cum:
                    chosen_feed = f
                    break
            if chosen_feed is None:
                chosen_feed = weighted_feed[0][1]
            selected.append(("feed", chosen_feed))

        # Second slot: prefer another feed post, else a message
        remaining = [("feed", f) for f in feed_items if f.get("id") != (selected[0][1].get("id") if selected else None)] + \
                    [("msg", m) for m in messages]
        if remaining and len(selected) < 2:
            second = random.choice(remaining)
            selected.append(second)

        # Step 3: Reply to selected items
        msg_done = 0
        for kind, item in selected:
            if kind == "msg":
                if msg_done >= 1:
                    break  # only reply 1 message max
                msg_id = int(item["id"])
                post_id = item.get("post_id")
                comment_id = item.get("comment_id")
                content = item["content"]
                post_content = item.get("post_content", "")

                reply = _generate_reply("notification", content, post_content, [], personality, lang)
                if not reply:
                    continue
                if post_id and comment_id:
                    ok = api("POST", f"/posts/{post_id}/comments", token, uid,
                             data={"user_id": uid, "content": reply, "reply_to_comment_id": comment_id})
                elif post_id:
                    ok = api("POST", f"/posts/{post_id}/comments", token, uid,
                             data={"user_id": uid, "content": reply})
                else:
                    ok = None
                if ok:
                    print(f"  ✓ msg→{msg_id}: {reply[:50]}")
                    _replied[_reply_key(uid, "msg", msg_id)] = time.time()
                    msg_done += 1

            else:  # feed
                post_id = int(item["id"])
                content = item["content"]

                # Like the post first
                if like_post(bot, post_id):
                    print(f"  ♥ feed post {post_id}")

                # No language filtering — all posts are candidates
                # Reply language determined by _reply_lang() below
                post_lang = _detect_lang(content)

                # Randomly choose: reply-to-post OR reply-to-a-comment
                choice = random.choice(["post", "comment"])

                if choice == "comment":
                    # Fetch comments, pick a random one to reply to
                    post_data = api("GET", f"/posts/{post_id}", token, uid)
                    comments = []
                    if post_data:
                        raw_comments = post_data.get("comments", []) if isinstance(post_data, dict) else []
                        if isinstance(raw_comments, list):
                            for c in raw_comments[:10]:
                                cid = c.get("id")
                                if not cid:
                                    continue
                                c_author = c.get("author", {})
                                c_uid = c_author.get("account_id") or c_author.get("user_id")
                                if c_uid == uid or _replied.get(_reply_key(uid, "comment", cid)):
                                    continue
                                c_content = _clean(c.get("content", ""))
                                if c_content and len(c_content) >= 3:
                                    comments.append({"id": cid, "content": c_content})
                    if comments:
                        chosen = random.choice(comments)
                        reply_lang = _reply_lang(_detect_lang(chosen["content"]), languages)
                        reply = _generate_reply("comment", chosen["content"], content, [], personality, reply_lang)
                        if reply:
                            ok = api("POST", f"/posts/{post_id}/comments", token, uid,
                                     data={"user_id": uid, "content": reply, "reply_to_comment_id": chosen["id"]})
                            if ok:
                                print(f"  ✓ feed comment {post_id}/{chosen['id']}: {reply[:50]}")
                                _replied[_reply_key(uid, "comment", chosen["id"])] = time.time()
                                _replied[_reply_key(uid, "post", post_id)] = time.time()
                    else:
                        # Fallback to reply-to-post
                        choice = "post"

                if choice == "post":
                    reply_lang = _reply_lang(post_lang, languages)
                    reply = _generate_reply("post", content, "", [], personality, reply_lang)
                    if reply:
                        ok = api("POST", f"/posts/{post_id}/comments", token, uid,
                                 data={"user_id": uid, "content": reply})
                        if ok:
                            print(f"  ✓ feed post {post_id}: {reply[:50]}")
                            _replied[_reply_key(uid, "post", post_id)] = time.time()

                time.sleep(1)

        if not chat_results and not selected:
            print("  (nothing to do)")
        time.sleep(2)

    _save_cache()
    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Done.")

if __name__ == "__main__":
    main()
