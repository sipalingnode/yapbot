#!/usr/bin/env python3
"""
reply_bot_final.py — Playwright Twitter/X Auto-Reply Bot (FINAL)

Fitur utama:
- Scrape tweet dari 1 List X/Twitter via Playwright (tanpa Twitter API).
- Hanya balas ORIGINAL POST (bukan reply, bukan retweet).
- Wajib LIKE dulu sebelum reply:
  - Kalau sudah liked → langsung lanjut reply.
  - Kalau tombol LIKE belum muncul → bot tunggu beberapa kali.
  - Kalau tetap tidak bisa LIKE → reply dibatalkan.
- Analisa tweet sebelum reply:
  - GM/GN pendek → balas "GM NamaAkun" / "GN NamaAkun" (tanpa OpenAI).
  - GM/GN + konteks → balas GM/GN + lanjut bahas isi tweet (OpenAI).
  - Selain itu → reply konteks generik (OpenAI).
- Bahasa reply mengikuti bahasa tweet.
- Panjang reply 10–15 kata, tanpa emoji, tanpa karakter '-' (minus).
- Filter umur tweet:
  - Minimal umur: MIN_TWEET_AGE (detik) dari .env (default 3 menit).
  - Maksimal umur: MAX_TWEET_AGE_MINUTES (menit) dari .env (default 60 menit).
  - Tweet >24 jam di-skip.
- Per akun:
  - 1 reply per akun per cycle.
  - Cooldown per akun: PER_ACCOUNT_COOLDOWN (default 30 menit).
- Limit harian:
  - PAUSE_AFTER → pause 1 jam.
  - STOP_AFTER → stop sampai hari berikutnya.
"""

import os
import time
import json
import random
import re
import traceback
from datetime import datetime, date, timedelta, timezone

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from openai import OpenAI
import sys

load_dotenv()

# -------------------------
# CONFIG (.env)
# -------------------------
LIST_ID = os.getenv("LIST_ID")  # required
COOKIE_FILE = os.getenv("COOKIE_FILE", "cookies.json")
PLAYWRIGHT_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "1") == "1"

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

MAX_RESULTS = int(os.getenv("MAX_RESULTS", "50"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "900"))   # secs
MIN_TWEET_AGE = int(os.getenv("MIN_TWEET_AGE", "180"))   # secs (3 menit)

# umur maksimal tweet (menit) — setara within_time:XXmin
MAX_TWEET_AGE_MINUTES = int(os.getenv("MAX_TWEET_AGE_MINUTES", "60"))  # default 60 menit

DELAY_AFTER_REPLY = int(os.getenv("DELAY_AFTER_REPLY", "120"))  # secs

REPLIED_FILE = os.getenv("REPLIED_FILE", "replied_ids.txt")
STATS_FILE = os.getenv("STATS_FILE", "daily_stats.json")

# Safety caps
PAUSE_AFTER = int(os.getenv("PAUSE_AFTER", "500"))   # pause 1 hour
STOP_AFTER = int(os.getenv("STOP_AFTER", "1000"))    # stop until next day

# jitter
JITTER_MAX = int(os.getenv("JITTER_MAX", "6"))

# OpenAI backoff if quota/rate
OPENAI_BACKOFF_SEC = int(os.getenv("OPENAI_BACKOFF_SEC", "600"))

# navigation timeout (ms)
NAV_TIMEOUT = int(os.getenv("NAV_TIMEOUT", "60000"))

# per-account cooldown (default 30 menit)
PER_ACCOUNT_COOLDOWN = int(os.getenv("PER_ACCOUNT_COOLDOWN", "1800"))  # 30 * 60

AUTHOR_HISTORY_FILE = os.getenv("AUTHOR_HISTORY_FILE", "author_last_reply.json")

# -------------------------
# Basic checks
# -------------------------
if not LIST_ID:
    print("[ERR] LIST_ID not set in .env")
    sys.exit(1)
if not OPENAI_API_KEY:
    print("[ERR] OPENAI_API_KEY not set in .env")
    sys.exit(1)

# initialize OpenAI v1 client
openai_client = OpenAI(api_key=OPENAI_API_KEY)

# -------------------------
# Logging helpers
# -------------------------
def info(msg): print(f"[INFO] {msg}")
def ok(msg): print(f"[OK] {msg}")
def warn(msg): print(f"[WARN] {msg}")
def err(msg): print(f"[ERR] {msg}")

# -------------------------
# Persistence helpers
# -------------------------
def load_replied_ids(path=REPLIED_FILE):
    s = set()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                for l in f:
                    v = l.strip()
                    if v:
                        s.add(v)
        except Exception:
            pass
    else:
        open(path, "a").close()
    return s

def save_replied_id(tweet_id, path=REPLIED_FILE):
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(str(tweet_id) + "\n")
    except Exception as e:
        err(f"Failed to persist replied id: {e}")

def load_daily_stats(path=STATS_FILE):
    if not os.path.exists(path):
        return {"date": str(date.today()), "count": 0}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"date": str(date.today()), "count": 0}

def save_daily_stats(count, path=STATS_FILE):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"date": str(date.today()), "count": count}, f)
    except Exception as e:
        err(f"Failed save daily stats: {e}")

def load_author_history(path=AUTHOR_HISTORY_FILE):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_author_history(history, path=AUTHOR_HISTORY_FILE):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(history, f)
    except Exception as e:
        err(f"Failed save author history: {e}")

# -------------------------
# PROMPTS (generic + GMGN context, auto language)
# -------------------------
def build_generic_prompt(original_post: str) -> str:
    return f"""
You are a human-like Twitter user replying to a tweet.

TASK:
1. Read the Original Post.
2. Detect the language of the Original Post.
3. Write ONE short, natural reply that fits the exact context and intent.

CONSTRAINTS:
- Reply ONLY in the same language as the Original Post.
- Length: between 10 and 15 words.
- No emojis, no hashtags, no bullet points, no line breaks.
- Do NOT use the dash character (-) anywhere.
- Use normal punctuation only: commas, periods, question marks.
- The reply must specifically address the tweet's content and intent
  (e.g. question, opinion, alpha, announcement)
  and must NOT sound generic or templated.

Original Post:
{original_post}

Reply:
""".strip()

def build_gmgn_context_prompt(original_post: str, greeting_line: str) -> str:
    """
    greeting_line contoh: "GM John Crypto" atau "GN Alex".
    Model diminta:
    - mulai reply dengan greeting_line
    - lanjutkan bahas isi tweet
    """
    return f"""
You are a human-like Twitter user replying to a tweet that starts with a GM/GN greeting.

TASK:
1. Read the Original Post.
2. Detect the language of the Original Post.
3. Write ONE short, natural reply that:
   - STARTS with exactly: "{greeting_line}"
   - Then continues with a few more words reacting to the rest of the tweet.

CONSTRAINTS:
- Reply ONLY in the same language as the Original Post.
- Total length: between 10 and 15 words (including the greeting line).
- No emojis, no hashtags, no bullet points, no line breaks.
- Do NOT use the dash character (-) anywhere.
- Use normal punctuation only: commas, periods, question marks.
- The reply must clearly respond to the tweet's context, not just say GM or GN.

Original Post:
{original_post}

Reply:
""".strip()

def generate_reply_text_generic(original_post: str):
    prompt = build_generic_prompt(original_post)
    resp = openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.6,
        max_tokens=64,
    )
    reply_text = resp.choices[0].message.content.strip()
    # sel=1 → generic
    return 1, reply_text

def generate_reply_text_gmgn_context(original_post: str, base: str, display_name: str):
    greeting_line = base if not display_name else f"{base} {display_name}"
    prompt = build_gmgn_context_prompt(original_post, greeting_line)
    resp = openai_client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.6,
        max_tokens=64,
    )
    reply_text = resp.choices[0].message.content.strip()
    # sel=2 → gmgn_context
    return 2, reply_text

# -------------------------
# GM/GN detection & mode selection
# -------------------------
GM_PATTERN = re.compile(r"\bgm\b", re.IGNORECASE)
GN_PATTERN = re.compile(r"\bgn\b", re.IGNORECASE)

def detect_any_gm_gn(text: str):
    if not text:
        return None
    lower = text.lower()
    if GM_PATTERN.search(lower):
        return "GM"
    if GN_PATTERN.search(lower):
        return "GN"
    return None

def detect_strict_gm_gn(text: str):
    """
    Deteksi GM/GN "murni" / pendek.
    Contoh yang dianggap pure:
    - "gm"
    - "gm ct"
    - "gm fam"
    - "gn"
    - "gn all"
    Kalau kalimat sudah panjang (banyak kata), dianggap GM/GN + konteks.
    """
    if not text:
        return None

    lower = text.lower().strip()
    tokens = re.findall(r"[a-zA-Z]+", lower)
    if not tokens:
        return None

    has_gm = any(t == "gm" for t in tokens)
    has_gn = any(t == "gn" for t in tokens)
    if not (has_gm or has_gn):
        return None

    # kalau jumlah kata <= 3, anggap pure GM/GN
    if len(tokens) <= 3:
        return "GM" if has_gm else "GN"

    # lebih panjang dari itu → dianggap GM/GN + konteks
    return None

def classify_gmgn_mode(text: str):
    """
    Kembalikan:
    - ("gmgn_pure", "GM"/"GN") → jika tweet pure GM/GN pendek.
    - ("gmgn_context", "GM"/"GN") → jika tweet mengandung GM/GN + konteks lanjutan.
    - ("none", None) → bukan GM/GN.
    """
    strict = detect_strict_gm_gn(text)
    any_gmgn = detect_any_gm_gn(text)

    if strict:
        return "gmgn_pure", strict
    if any_gmgn:
        return "gmgn_context", any_gmgn
    return "none", None

def decide_mode(text: str):
    """
    Analisis konteks tweet:
    - gmgn_pure   : GM/GN pendek → balas "GM Nama" / "GN Nama" saja.
    - gmgn_context: GM/GN + konteks → balas GM/GN + lanjutan bahas isi tweet (OpenAI).
    - generic     : tweet biasa → prompt generik.
    """
    mode, val = classify_gmgn_mode(text)
    if mode == "gmgn_pure":
        return "gmgn_pure", val
    if mode == "gmgn_context":
        return "gmgn_context", val
    return "generic", None

# -------------------------
# Cookie helpers
# -------------------------
def save_cookies_to_file(context, path=COOKIE_FILE):
    try:
        cookies = context.cookies()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cookies, f)
        info(f"Saved cookies to {path}")
    except Exception as e:
        err(f"Failed to save cookies: {e}")

def load_cookies_from_file(path=COOKIE_FILE):
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None

# -------------------------
# Helpers: robust navigation
# -------------------------
def robust_goto(page, url, attempts=2, wait_state="networkidle"):
    for i in range(attempts):
        try:
            page.goto(url, wait_until=wait_state, timeout=NAV_TIMEOUT)
            return True
        except PWTimeout:
            warn(f"Navigation timeout (attempt {i+1}) for {url} with {wait_state}. Trying fallback.")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
                time.sleep(1.0 + random.random()*0.8)
                return True
            except PWTimeout:
                warn(f"Fallback domcontentloaded timeout (attempt {i+1}) for {url}.")
                time.sleep(1 + i*2)
        except Exception as e:
            warn(f"Navigation error: {e}")
            time.sleep(1 + i*2)
    try:
        page.goto(url, wait_until="load", timeout=max(NAV_TIMEOUT, 120000))
        return True
    except Exception:
        return False

# -------------------------
# Username & display name extraction
# -------------------------
def extract_username_from_article(article):
    """
    Cari handle dasar dari href="/username".
    Disimpan kalau perlu, tapi GM/GN pakai display name.
    Contoh hasil: "@elonmusk".
    """
    try:
        links = article.query_selector_all("a[href^='/']")
        for a in links:
            href = a.get_attribute("href")
            if not href:
                continue
            if "/status/" in href:
                continue
            if href.count("/") == 1:
                handle = href.strip("/")
                if handle and handle.lower() not in ("home", "explore", "notifications", "messages"):
                    return "@" + handle
    except Exception:
        return None
    return None

def extract_display_name_from_article(article):
    """
    Ambil display name (nama profil Twitter), contoh: 'Elon Musk'.
    Biasanya berada di div[data-testid='User-Name'] span pertama.
    """
    try:
        user_block = article.query_selector("div[data-testid='User-Name']")
        if not user_block:
            return None
        spans = user_block.query_selector_all("span")
        if spans:
            name = spans[0].inner_text().strip()
            return name
    except Exception:
        return None
    return None

# -------------------------
# Parse tweet from container (article/div[data-testid='tweet'])
# -------------------------
def parse_tweet_from_article(article):
    try:
        time_el = article.query_selector("time")
        if not time_el:
            return None
        datetime_attr = time_el.get_attribute("datetime")
        if not datetime_attr:
            return None

        link = time_el.evaluate(
            "node => node.closest('a') ? node.closest('a').getAttribute('href') : null"
        )
        if not link:
            a = article.query_selector("a[href*='/status/']")
            if a:
                link = a.get_attribute("href")
        if not link:
            return None
        parts = link.split("/")
        tweet_id = parts[-1].split("?")[0]

        text_el = article.query_selector("div[data-testid='tweetText']")
        if text_el:
            text = text_el.inner_text()
        else:
            text = article.inner_text()

        has_image = False
        try:
            photo_div = article.query_selector("div[data-testid='tweetPhoto']")
            if photo_div:
                has_image = True
        except Exception:
            pass

        username = extract_username_from_article(article)
        display_name = extract_display_name_from_article(article)
        created_at = datetime.fromisoformat(datetime_attr.replace("Z", "+00:00"))
        return {
            "id": str(tweet_id),
            "text": text.strip(),
            "created_at": created_at,
            "has_image": has_image,
            "username": username,
            "display_name": display_name,
        }
    except Exception:
        return None

# -------------------------
# Fetch tweets from list page — ORIGINAL POSTS ONLY + skip retweet
# -------------------------
def fetch_tweets_from_list(page, max_results=MAX_RESULTS):
    list_url = f"https://x.com/i/lists/{LIST_ID}"
    info(f"Opening list URL: {list_url}")
    ok_nav = robust_goto(page, list_url)
    if not ok_nav:
        warn("Failed to load list page (after retries). Skipping this cycle.")
        return []

    # kasih waktu ekstra buat load konten list
    time.sleep(5)

    collected = {}
    scroll_tries = 0
    max_scrolls = 12
    while len(collected) < max_results and scroll_tries < max_scrolls:
        # Coba ambil tweet dengan beberapa selector berbeda
        article_handles = page.locator("article").element_handles()
        div_tweet_handles = page.locator("div[data-testid='tweet']").element_handles()

        # gabungkan (pakai id(handle) supaya unik)
        all_handles = list({id(h): h for h in (article_handles + div_tweet_handles)}.values())
        info(f"Found {len(all_handles)} tweet containers on page (scroll {scroll_tries}).")

        for art in all_handles:
            # skip retweet berdasarkan socialContext: "X reposted"
            try:
                social_ctx = art.query_selector("div[data-testid='socialContext']")
                if social_ctx:
                    ctx_text = social_ctx.inner_text().lower()
                    if "reposted" in ctx_text or "retweeted" in ctx_text:
                        continue
            except Exception:
                pass

            # skip reply yang benar-benar "Replying to"
            try:
                replying_to = art.query_selector("span:has-text('Replying to')")
                if replying_to:
                    continue
            except Exception:
                pass

            parsed = parse_tweet_from_article(art)
            if parsed and parsed["id"] not in collected:
                collected[parsed["id"]] = parsed
                if len(collected) >= max_results:
                    break

        if len(collected) >= max_results:
            break

        # scroll ke bawah
        page.evaluate("window.scrollBy(0, window.innerHeight);")
        time.sleep(1.0 + random.random()*0.8)
        scroll_tries += 1

    info(f"Collected {len(collected)} unique ORIGINAL tweets from list page.")

    # kalau masih 0, save screenshot untuk debug
    if len(collected) == 0:
        try:
            page.screenshot(path="debug_list.png", full_page=True)
            warn("No tweets found. Saved screenshot to debug_list.png")
        except Exception:
            pass

    return list(collected.values())

# -------------------------
# Auto-like helper (WAJIB LIKE sebelum reply)
# -------------------------
def like_tweet_if_possible(page, max_wait_attempts=8):
    """
    WAJIB like sebelum reply.
    - Jika tweet sudah liked → langsung return True (aman)
    - Jika tombol like belum muncul → tunggu dan ulangi
    - Jika sampai max_wait_attempts tetap tidak muncul → return False (jangan reply)
    """
    try:
        # 1) Sudah liked?
        try:
            unlike_btn = page.locator("div[data-testid='unlike']").first
            if unlike_btn and unlike_btn.is_visible():
                info("Tweet already liked (unlike button visible).")
                return True
        except Exception:
            pass

        # 2) Loop tunggu sampai tombol Like muncul
        wait_counter = 0

        while wait_counter < max_wait_attempts:
            selectors = [
                "div[data-testid='like']",
                "button[aria-label*='Like']",
                "button[aria-label*='Suka']",
                "div[role='button'][aria-label*='Like']",
                "div[role='button'][aria-label*='Suka']",
            ]

            for sel in selectors:
                try:
                    btn = page.locator(sel).first
                    if btn and btn.is_visible():
                        btn.scroll_into_view_if_needed()
                        btn.click(timeout=5000)
                        info(f"Tweet liked using selector: {sel}")
                        time.sleep(0.4 + random.random())
                        return True
                except Exception:
                    continue

            wait_counter += 1
            info(f"Like button not visible yet, waiting... attempt {wait_counter}/{max_wait_attempts}")
            page.wait_for_timeout(700)  # 0.7 detik

        warn("Failed to find LIKE button after multiple attempts.")
        return False

    except Exception as e:
        err(f"Error in like_tweet_if_possible: {e}")
        return False

# -------------------------
# Robust reply function (with mandatory auto-like)
# -------------------------
def reply_to_tweet(page, tweet_id, reply_text):
    try:
        status_url = f"https://x.com/i/web/status/{tweet_id}"
        ok_nav = robust_goto(page, status_url)
        if not ok_nav:
            err(f"Navigation failed for {status_url}")
            return False

        # kasih sedikit waktu sebelum cari tombol
        time.sleep(1.5 + random.random())

        # WAJIB like sebelum reply
        liked_ok = like_tweet_if_possible(page)
        if not liked_ok:
            warn(f"Skip reply for {tweet_id} because LIKE failed.")
            return False

        clicked = False
        attempts = [
            ("div[data-testid='reply']", 5000),
            ("div[aria-label='Reply']", 5000),
            ("role=button >> text=Reply", 5000),
        ]
        for sel, to in attempts:
            try:
                el = page.locator(sel).first
                if el:
                    el.scroll_into_view_if_needed()
                    el.click(timeout=to)
                    clicked = True
                    break
            except Exception:
                clicked = False

        if not clicked:
            try:
                article = page.locator("article").first
                if article:
                    article.click(timeout=3000)
                    page.keyboard.press("r")
                    clicked = True
            except Exception:
                clicked = False

        # Wait for composer textbox
        textbox = None
        selectors = [
            "div[role='textbox']",
            "div[aria-label='Tweet text']",
            "div[data-testid='tweetTextarea_0']",
            "div[contenteditable='true'][role='textbox']"
        ]
        total_wait = 0
        while total_wait < 25:
            for sel in selectors:
                try:
                    el = page.locator(sel).first
                    if el and el.is_visible():
                        textbox = el
                        break
                except Exception:
                    continue
            if textbox:
                break
            time.sleep(1)
            total_wait += 1

        if not textbox:
            err("Reply composer not found after attempts.")
            return False

        try:
            textbox.click(timeout=3000)
        except Exception:
            pass
        try:
            textbox.fill("")
        except Exception:
            try:
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
            except Exception:
                pass

        # type human-like
        for ch in reply_text:
            try:
                textbox.type(ch, delay=5)
            except Exception:
                try:
                    page.keyboard.insert_text(ch)
                except Exception:
                    pass
        time.sleep(0.3 + random.random()*0.6)

        # send tweet
        try:
            send = page.locator("div[data-testid='tweetButton']").first
            send.click(timeout=8000)
        except Exception:
            try:
                page.keyboard.down("Control")
                page.keyboard.press("Enter")
                page.keyboard.up("Control")
            except Exception:
                err("Failed to send reply.")
                return False

        time.sleep(2 + random.random()*2)
        return True

    except Exception as e:
        err(f"Failed to reply to {tweet_id}: {e}")
        return False

# -------------------------
# Day reset & sleep util
# -------------------------
def reset_if_new_day():
    global reply_count_today, current_day
    today = str(date.today())
    if today != current_day:
        info("New day detected — reset daily counter.")
        reply_count_today = 0
        current_day = today
        save_daily_stats(reply_count_today)

def sleep_until_next_day():
    now = datetime.now()
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    secs = (tomorrow - now).total_seconds()
    warn(f"Sleeping until next day ({int(secs)}s).")
    time.sleep(secs)
    reset_if_new_day()

# -------------------------
# Single-cycle logic
# -------------------------
def process_cycle(play):
    global replied_ids, reply_count_today, author_last_reply

    reset_if_new_day()

    context = None
    browser = None
    authors_replied_this_cycle = set()

    try:
        cookies = load_cookies_from_file(COOKIE_FILE)
        browser = play.chromium.launch(
            headless=PLAYWRIGHT_HEADLESS,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        if cookies:
            context = browser.new_context()
            context.add_cookies(cookies)
            page = context.new_page()
            info("Loaded cookies into browser context.")
        else:
            info("No cookies found — opening browser for manual login.")
            context = browser.new_context()
            page = context.new_page()
            page.goto("https://x.com/login", wait_until="networkidle")
            info("Please log in manually in opened browser. After login, press ENTER here to continue.")
            input("Press ENTER after login...")
            save_cookies_to_file(context, COOKIE_FILE)

        tweets = fetch_tweets_from_list(page, max_results=MAX_RESULTS)
        tweets_map = {t["id"]: t for t in tweets}

        if not tweets_map:
            info("No tweets found this cycle.")
            context.close(); browser.close()
            return

        now = datetime.now(timezone.utc)
        recent_cutoff = now - timedelta(minutes=MAX_TWEET_AGE_MINUTES)

        ready = []
        waiting = []

        for tid, t in tweets_map.items():
            if tid in replied_ids:
                continue
            created_at = t.get("created_at")
            if not created_at:
                continue

            age_sec = (now - created_at).total_seconds()

            # skip > 24 jam
            if age_sec > 86400:
                continue

            # skip lebih tua dari MAX_TWEET_AGE_MINUTES
            if created_at < recent_cutoff:
                continue

            if age_sec >= MIN_TWEET_AGE:
                ready.append(t)
            else:
                waiting.append({"tweet": t, "age": age_sec})
                info(
                    f"Skipping new tweet {tid} (age {int(age_sec)}s < {MIN_TWEET_AGE}s). "
                    "Will re-evaluate after ready replies."
                )

        if not ready and not waiting:
            info("No candidates after filtering.")
            context.close(); browser.close()
            return

        # process ready tweets
        if ready:
            info(f"Processing {len(ready)} ready tweets.")
        for t in ready:
            if reply_count_today >= STOP_AFTER:
                info("Reached STOP_AFTER limit — sleeping until next day.")
                sleep_until_next_day()
                break

            tid = t["id"]
            if tid in replied_ids:
                continue

            text = t["text"] or ""
            display_name = t.get("display_name") or ""
            username = t.get("username") or ""
            author_key = (username or display_name or "unknown").lower()

            # cooldown per akun
            now_author = datetime.now(timezone.utc)
            last_ts_str = author_last_reply.get(author_key)
            if last_ts_str:
                try:
                    last_ts = datetime.fromisoformat(last_ts_str)
                    diff = (now_author - last_ts).total_seconds()
                    if diff < PER_ACCOUNT_COOLDOWN:
                        info(
                            f"Skipping {tid} from {author_key} due to per-account cooldown "
                            f"({int(PER_ACCOUNT_COOLDOWN - diff)}s left)."
                        )
                        continue
                except Exception:
                    pass

            # 1 reply per akun per cycle
            if author_key in authors_replied_this_cycle:
                info(f"Already replied to {author_key} in this cycle, skipping tweet {tid}.")
                continue

            try:
                mode, gmgn_val = decide_mode(text)

                if mode == "gmgn_pure":
                    base = gmgn_val or "GM"
                    reply_text = base if not display_name else f"{base} {display_name}"
                    sel = 0
                    info(
                        f"[MODE=GM/GN PURE] Detected {base} tweet {tid}, "
                        f"reply '{reply_text}' (no OpenAI)."
                    )
                elif mode == "gmgn_context":
                    base = gmgn_val or "GM"
                    info(f"[MODE=GM/GN CONTEXT] Detected {base} tweet {tid}, using GM/GN+context prompt.")
                    try:
                        sel, reply_text = generate_reply_text_gmgn_context(text, base, display_name)
                    except Exception as oe:
                        err(f"OpenAI GMGN-context error: {oe}")
                        warn(f"Sleeping {OPENAI_BACKOFF_SEC}s due to OpenAI error.")
                        time.sleep(OPENAI_BACKOFF_SEC)
                        continue
                else:  # generic
                    info(f"[MODE=GENERIC] Tweet {tid} processed with generic context prompt.")
                    try:
                        sel, reply_text = generate_reply_text_generic(text)
                    except Exception as oe:
                        err(f"OpenAI error: {oe}")
                        warn(f"Sleeping {OPENAI_BACKOFF_SEC}s due to OpenAI error.")
                        time.sleep(OPENAI_BACKOFF_SEC)
                        continue

                success = reply_to_tweet(page, tid, reply_text)
                if success:
                    replied_ids.add(tid); save_replied_id(tid)
                    reply_count_today += 1; save_daily_stats(reply_count_today)

                    authors_replied_this_cycle.add(author_key)
                    author_last_reply[author_key] = datetime.now(timezone.utc).isoformat()
                    save_author_history(author_last_reply)

                    ok(f"Replied to {tid} (Mode {mode}, sel={sel}) — Count Today = {reply_count_today}")
                    if reply_count_today == PAUSE_AFTER:
                        warn(f"Reached {PAUSE_AFTER} replies — pausing 1 hour.")
                        time.sleep(3600)
                    sleep_sec = DELAY_AFTER_REPLY + random.uniform(0, JITTER_MAX)
                    info(f"Sleeping {int(sleep_sec)}s after reply.")
                    time.sleep(sleep_sec)
                else:
                    err(f"Reply failed for {tid}.")
            except Exception as e:
                err(f"Error processing ready tweet {tid}: {e}")
                traceback.print_exc()
                continue

        # re-evaluate waiting tweets from same scan
        if waiting:
            info(f"Re-evaluating {len(waiting)} waiting tweets from this scan.")
        for item in waiting:
            if reply_count_today >= STOP_AFTER:
                info("Reached STOP_AFTER limit — sleeping until next day.")
                sleep_until_next_day()
                break

            t = item["tweet"]
            tid = t["id"]
            if tid in replied_ids:
                continue

            created_at = t.get("created_at")
            if not created_at:
                continue

            now2 = datetime.now(timezone.utc)
            age_now = (now2 - created_at).total_seconds()

            # >24 jam skip
            if age_now > 86400:
                continue

            # >max umur skip
            if created_at < (now2 - timedelta(minutes=MAX_TWEET_AGE_MINUTES)):
                continue

            if age_now < MIN_TWEET_AGE:
                info(
                    f"Still too new after processing others: {tid} (age {int(age_now)}s) "
                    "— skipping until next cycle."
                )
                continue

            text = t["text"] or ""
            display_name = t.get("display_name") or ""
            username = t.get("username") or ""
            author_key = (username or display_name or "unknown").lower()

            # cooldown per akun
            last_ts_str = author_last_reply.get(author_key)
            if last_ts_str:
                try:
                    last_ts = datetime.fromisoformat(last_ts_str)
                    diff = (datetime.now(timezone.utc) - last_ts).total_seconds()
                    if diff < PER_ACCOUNT_COOLDOWN:
                        info(
                            f"Skipping {tid} from {author_key} due to per-account cooldown "
                            f"({int(PER_ACCOUNT_COOLDOWN - diff)}s left)."
                        )
                        continue
                except Exception:
                    pass

            if author_key in authors_replied_this_cycle:
                info(f"Already replied to {author_key} in this cycle, skipping tweet {tid}.")
                continue

            try:
                mode, gmgn_val = decide_mode(text)

                if mode == "gmgn_pure":
                    base = gmgn_val or "GM"
                    reply_text = base if not display_name else f"{base} {display_name}"
                    sel = 0
                    info(
                        f"[MODE=GM/GN PURE] Detected {base} tweet {tid}, "
                        f"reply '{reply_text}' (no OpenAI)."
                    )
                elif mode == "gmgn_context":
                    base = gmgn_val or "GM"
                    info(f"[MODE=GM/GN CONTEXT] Detected {base} tweet {tid}, using GM/GN+context prompt.")
                    try:
                        sel, reply_text = generate_reply_text_gmgn_context(text, base, display_name)
                    except Exception as oe:
                        err(f"OpenAI GMGN-context error: {oe}")
                        warn(f"Sleeping {OPENAI_BACKOFF_SEC}s due to OpenAI error.")
                        time.sleep(OPENAI_BACKOFF_SEC)
                        continue
                else:
                    info(f"[MODE=GENERIC] Tweet {tid} processed with generic context prompt.")
                    try:
                        sel, reply_text = generate_reply_text_generic(text)
                    except Exception as oe:
                        err(f"OpenAI error: {oe}")
                        warn(f"Sleeping {OPENAI_BACKOFF_SEC}s due to OpenAI error.")
                        time.sleep(OPENAI_BACKOFF_SEC)
                        continue

                success = reply_to_tweet(page, tid, reply_text)
                if success:
                    replied_ids.add(tid); save_replied_id(tid)
                    reply_count_today += 1; save_daily_stats(reply_count_today)

                    authors_replied_this_cycle.add(author_key)
                    author_last_reply[author_key] = datetime.now(timezone.utc).isoformat()
                    save_author_history(author_last_reply)

                    ok(f"Replied to {tid} (Mode {mode}, sel={sel}) — Count Today = {reply_count_today}")
                    if reply_count_today == PAUSE_AFTER:
                        warn(f"Reached {PAUSE_AFTER} replies — pausing 1 hour.")
                        time.sleep(3600)
                    sleep_sec = DELAY_AFTER_REPLY + random.uniform(0, JITTER_MAX)
                    info(f"Sleeping {int(sleep_sec)}s after reply.")
                    time.sleep(sleep_sec)
                else:
                    err(f"Reply failed for {tid}.")
            except Exception as e:
                err(f"Error processing waiting tweet {tid}: {e}")
                traceback.print_exc()
                continue

        info("Finished processing candidates from this cycle.")
    except Exception as e:
        err(f"Exception in process_cycle: {e}")
        traceback.print_exc()
    finally:
        try:
            if context:
                context.close()
            if browser:
                browser.close()
        except Exception:
            pass

# -------------------------
# Main
# -------------------------
def main():
    global replied_ids, current_day, reply_count_today, author_last_reply
    replied_ids = load_replied_ids()
    stats = load_daily_stats()
    current_day = stats.get("date", str(date.today()))
    reply_count_today = int(stats.get("count", 0))
    author_last_reply = load_author_history()

    info(f"Loaded {len(replied_ids)} replied IDs. Stats: {current_day}, count={reply_count_today}")
    info(f"Loaded per-account history: {len(author_last_reply)} authors tracked.")
    info(
        f"Starting Playwright bot. LIST_ID={LIST_ID}, POLL_INTERVAL={POLL_INTERVAL}s, "
        f"MIN_TWEET_AGE={MIN_TWEET_AGE}s, MAX_TWEET_AGE_MINUTES={MAX_TWEET_AGE_MINUTES}, "
        f"PER_ACCOUNT_COOLDOWN={PER_ACCOUNT_COOLDOWN}s"
    )

    with sync_playwright() as play:
        while True:
            try:
                process_cycle(play)
            except KeyboardInterrupt:
                warn("Stopped by user.")
                break
            except Exception as e:
                err(f"Main loop error: {e}")
                traceback.print_exc()
            info(f"Sleeping {POLL_INTERVAL}s...")
            time.sleep(POLL_INTERVAL)

if __name__ == "__main__":
    main()
