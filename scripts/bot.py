#!/usr/bin/env python3
"""
GameltBook Comment Bot — main entry point.
Loads config, randomly selects N bots, then for each bot:
  1. Check chat threads for messages to reply to
  2. Check message notifications
  3. Scan recent posts — like + reply to good ones, then scan comments
"""
import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
CONFIG_PATH = os.path.join(SKILL_DIR, "config.yaml")
API_SCRIPT = "/home/ubuntu/.hermes/skills/gameltbook-api/scripts/gameltbook_api.py"
BASE_URL = "https://gameltbook.2lh2o.com:8000"

# Persisted reply history — survives within one run, persists across bot instances
# Key: "post:{post_id}" / "comment:{comment_id}" / "msg:{msg_id}" / "chat:{chat_id}"
# Value: timestamp
_replied: dict[str, float] = {}


def load_config():
    import yaml
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def api(method, endpoint, token, user_id=None, data=None, form=None):
    """Call gameltbook-api helper. Returns parsed JSON body."""
    cmd = ["python3", API_SCRIPT, method, f"{BASE_URL}{endpoint}"]
    cmd += ["--token", token, "--insecure"]
    if user_id is not None:
        cmd += ["--user-id", str(user_id)]
    if data is not None:
        cmd += ["--data", json.dumps(data, ensure_ascii=False)]
    if form:
        for fld in form:
            cmd += ["--form", fld]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        resp = json.loads(result.stdout)
    except Exception:
        print(f"  [API ERROR] stdout={result.stdout[:200]} stderr={result.stderr[:100]}")
        return None

    if resp.get("status", 0) >= 400:
        return None
    return resp.get("body")


def select_bots(config):
    all_bots = config["bots"]
    n = min(config.get("num_to_select", 1), len(all_bots))
    return random.sample(all_bots, n)


# ── Step 1: Chat replies ──────────────────────────────────────────────────────

def process_chats(bot):
    uid = bot["user_id"]
    token = bot["token"]
    lang = bot["language"]
    prompt = bot.get("system_prompt", "")

    data = api("GET", f"/users/{uid}/chats", token, uid)
    if not data:
        return []

    chats = data if isinstance(data, list) else data.get("chats", data.get("results", []))
    if not chats:
        return []

    results = []
    for chat in chats[:5]:
        chat_id = chat.get("id")
        if not chat_id:
            continue

        thread = api("GET", f"/chats/{chat_id}", token, uid)
        if not thread:
            continue

        messages = thread if isinstance(thread, list) else thread.get("messages", thread.get("results", []))
        if not messages:
            continue

        last = messages[-1]
        last_sender = last.get("author", {}).get("account_id") or last.get("account_id")
        last_content = last.get("content", "")

        if last_sender == uid:
            continue
        if not last_content or len(last_content.strip()) < 5:
            continue

        reply = generate_reply(last_content, bot["personality"], lang, context="chat_message", max_len=120)
        if not reply:
            continue

        ok = api("POST", f"/chats/{chat_id}/messages", token, uid,
                 data={"user_id": uid, "content": reply})
        if ok:
            results.append(f"chat→{chat_id}: {reply[:50]}")
            _replied[f"chat:{chat_id}"] = time.time()

    return results


# ── Step 2: Message notifications ─────────────────────────────────────────────

def process_messages(bot):
    uid = bot["user_id"]
    token = bot["token"]
    lang = bot["language"]
    prompt = bot.get("system_prompt", "")

    data = api("GET", f"/users/{uid}/messages", token, uid)
    if not data:
        return []

    messages = data if isinstance(data, list) else data.get("messages", data.get("results", []))
    if not messages:
        return []

    results = []
    for msg in messages[:20]:
        msg_id = msg.get("id")
        if _replied.get(f"msg:{msg_id}"):
            continue

        # Notification content: comment_content is the reply made by the actor
        content = msg.get("comment_content", "") or msg.get("content", "")
        if not content or len(content.strip()) < 5:
            continue

        post_id = msg.get("post_id")
        comment_id = msg.get("comment_id")

        reply = generate_reply(content, bot["personality"], lang, context="notification", max_len=100)
        if reply:
            if post_id and comment_id:
                ok = api("POST", f"/posts/{post_id}/comments", token, uid,
                         data={"user_id": uid, "content": reply, "reply_to_comment_id": comment_id})
                if ok:
                    results.append(f"msg→{msg_id}: {reply[:50]}")
                    _replied[f"msg:{msg_id}"] = time.time()
            elif post_id:
                ok = api("POST", f"/posts/{post_id}/comments", token, uid,
                         data={"user_id": uid, "content": reply, "reply_to_comment_id": None})
                if ok:
                    results.append(f"msg→{msg_id}: {reply[:50]}")
                    _replied[f"msg:{msg_id}"] = time.time()

    return results


# ── Step 3: Feed + Comments ──────────────────────────────────────────────────

def process_feed(bot, max_posts=50, max_comments=5):
    uid = bot["user_id"]
    token = bot["token"]
    lang = bot["language"]
    prompt = bot.get("system_prompt", "")

    posts_data = api("GET", "/posts", token, uid)
    if not posts_data:
        return []

    posts = posts_data if isinstance(posts_data, list) else posts_data.get("posts", posts_data.get("results", []))
    if not posts:
        return []

    results = []
    for post in posts[:max_posts]:
        post_id = post.get("id")
        if not post_id:
            continue

        if _replied.get(f"post:{post_id}"):
            continue

        author = post.get("author", {})
        post_author_id = author.get("account_id") or author.get("user_id")
        is_my_post = (post_author_id == uid)

        # Rule: skip posts authored by the bot
        if is_my_post:
            continue

        content = post.get("content", "")

        # — Like the post if it's interesting —
        if should_like_post(content, prompt, lang):
            ok = api("POST", f"/posts/{post_id}/favorite", token, uid,
                     data={"user_id": uid})
            if ok:
                results.append(f"  ♡ post {post_id}")

        # — Reply to the post directly (no prior comment needed) —
        if should_reply_content(content):
            reply = generate_reply(content, bot["personality"], lang, context="post", max_len=150)
            if reply:
                ok = api("POST", f"/posts/{post_id}/comments", token, uid,
                         data={"user_id": uid, "content": reply, "reply_to_comment_id": None})
                if ok:
                    results.append(f"post {post_id}: {reply[:60]}")
                    _replied[f"post:{post_id}"] = time.time()

        # — Scan top comments on this post —
        post_data = api("GET", f"/posts/{post_id}", token, uid)
        if not post_data:
            continue

        comments = post_data if isinstance(post_data, list) else post_data.get("comments", [])
        for comment in comments[:max_comments]:
            cid = comment.get("id")
            if not cid:
                continue

            if _replied.get(f"comment:{cid}"):
                continue

            comment_author = comment.get("author", {})
            comment_author_id = comment_author.get("account_id") or comment_author.get("user_id")
            is_my_comment = (comment_author_id == uid)

            # Rule: skip comments authored by the bot
            if is_my_comment:
                continue

            comment_content = comment.get("content", "")

            # Like comment if interesting
            if should_like_comment(comment_content, prompt, lang):
                ok = api("POST", f"/posts/{post_id}/comments/{cid}/like", token, uid,
                         data={"user_id": uid})
                if ok:
                    results.append(f"  ♡ comment {cid}")

            # Reply to comment — always allowed (not bot's content)
            if should_reply_content(comment_content):
                reply = generate_reply(comment_content, bot["personality"], lang, context="comment", max_len=120)
                if reply:
                    ok = api("POST", f"/posts/{post_id}/comments", token, uid,
                             data={"user_id": uid, "content": reply, "reply_to_comment_id": cid})
                    if ok:
                        results.append(f"  comment {cid}→: {reply[:50]}")
                        _replied[f"comment:{cid}"] = time.time()

        time.sleep(1)

    return results


# ── Reply / Like generation ─────────────────────────────────────────────────

def generate_reply(source_content, personality, language, context, max_len=120):
    """Template-based reply generation — no LLM needed.
    personality: one of jp_girl | cn_tech_guy | jp_guy | cn_liberal_girl
    """

    # Template pools

    # 在日女大学生 — 温柔共感型
    jp_girl = {
        "zh": [
            "感觉还挺…的，有点喜欢这种",
            "我也有同感！",
            "有点喜欢这种感觉诶",
            "感觉还挺真实的",
            "有点被戳到…",
            "我也有点这种感觉！",
            "感觉这种体验还挺有意思的",
            "确实会有这种感觉诶",
        ],
        "ja": [
            "なんか这种感觉，我也懂",
            "有点同感…",
        ],
    }

    # 理工直男 — 理性吐槽型
    cn_tech_guy = {
        "zh": [
            "说实话感觉一般",
            "其实没这么复杂",
            "感觉有点过了",
            "说实话不太行",
            "其实问题不大",
            "感觉说的有点道理",
            "其实还好吧",
            "说实话第一反应也是这样",
            "感觉见多了就那样",
        ],
        "en": [
            "feels kinda mid ngl",
            "tbh not that impressive",
            "idk about this one",
            "kinda makes sense tho",
        ],
    }

    # 在日男大学生 — 内敛随和型
    jp_guy = {
        "ja": [
            "なんかそれある",
            "たぶんそうかもな",
            "ちょっと同感",
            "なんか分かる",
            "まあそんな感じ吧",
            "なんか违和感ある",
            "ちょっと気になっている",
            "まあ分かる",
        ],
        "zh": [
            "有点同感…",
            "可能吧，我也觉得",
            "有点这种感觉",
            "好像确实是…",
            "有点理解",
        ],
    }

    # 文科女生 — 温和细腻型
    cn_liberal_girl = {
        "zh": [
            "我有点…的感觉",
            "感觉这种东西还挺微妙的",
            "我其实也有点这种感觉…",
            "有点想说点什么但是又说不清",
            "感觉挺真实的…",
            "我有点…的感觉吧",
            "说实话我也会有点这种感觉",
            "感觉这个挺戳人的",
        ],
        "en": [
            "I kinda feel the same way",
            "That hits different honestly",
        ],
    }

    # Map personality name → template dict
    personality_map = {
        "jp_girl": jp_girl,
        "cn_tech_guy": cn_tech_guy,
        "jp_guy": jp_guy,
        "cn_liberal_girl": cn_liberal_girl,
    }

    # Direct personality → templates lookup
    templates = personality_map.get(personality, jp_girl)

    lang_templates = templates.get(language, templates.get("zh", templates.get("ja", [])))

    # Extract keyword — first meaningful 3+ char word
    words = re.findall(r'[\w]{3,}', source_content)
    keyword = words[2] if len(words) > 2 else (words[-1] if words else "that")
    keyword = keyword[:20]

    if not lang_templates:
        return None

    template = random.choice(lang_templates)
    reply = template.format(keyword=keyword)
    return reply[:max_len]


def should_reply_content(content: str) -> bool:
    if not content or len(content.strip()) < 30:
        return False
    spam = ["click here", "buy now", "free money", "earn $", "DM me"]
    if any(w in content.lower() for w in spam):
        return False
    return True


def should_like_post(content: str, system_prompt: str, language: str) -> bool:
    """Decide if a post is interesting enough to like (heuristic, no LLM)."""
    if not content or len(content.strip()) < 10:
        return False
    spam = ["click here", "buy now", "free money", "earn $", "DM me", "discord.gg"]
    if any(w in content.lower() for w in spam):
        return False
    positive = ["我", "我觉得", "?", "actually", "really", "很有意思", "推荐", "吐槽",
                "game", "ゲーム", "游戏", "lol", "nice", "确实", "interesting", "amazing"]
    score = sum(1 for s in positive if s.lower() in content.lower())
    return score >= 1


def should_like_comment(content: str, system_prompt: str, language: str) -> bool:
    """Decide if a comment is interesting enough to like (heuristic, no LLM)."""
    if not content or len(content.strip()) < 8:
        return False
    spam = ["click here", "buy now", "free money", "discord.gg"]
    if any(w in content.lower() for w in spam):
        return False
    positive = ["同意", "哈哈", "确实", "有意思", "?", "lol", "nice", "真的", "其实",
                "我", "game", "ゲーム", "游戏", "赞", "interesting"]
    score = sum(1 for s in positive if s.lower() in content.lower())
    return score >= 1


def evaluate_for_personality(content, system_prompt, language):
    """Always return True - reply decision is driven by should_reply_content heuristic."""
    return True


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    print(f"[{datetime.now(timezone.utc).isoformat()}] GameltBook Comment Bot starting...")

    config = load_config()
    bots = select_bots(config)
    max_posts = config.get("feed", {}).get("max_posts", 50)
    max_comments = config.get("feed", {}).get("max_comments", 5)

    print(f"Selected {len(bots)} bots: {[b['user_id'] for b in bots]}")

    for bot in bots:
        uid = bot["user_id"]
        personality = bot["personality"]
        print(f"\n--- Bot {uid} ({personality}) ---")

        print("Checking chats...")
        results = process_chats(bot)
        for r in results:
            print(f"  ✓ {r}")
        if not results:
            print("  (no chat replies)")

        print("Checking messages...")
        results = process_messages(bot)
        for r in results:
            print(f"  ✓ {r}")
        if not results:
            print("  (no message replies)")

        print("Scanning feed...")
        results = process_feed(bot, max_posts=10, max_comments=10)
        for r in results:
            print(f"  ✓ {r}")
        if not results:
            print("  (no feed activity)")

        time.sleep(2)

    print(f"\n[{datetime.now(timezone.utc).isoformat()}] Done.")


if __name__ == "__main__":
    main()