"""
Nike SB Dunk Tracker — SB Radar
- Monitors Nike-adjacent skate shop sites (national list, DNS-verified Sep 2026)
  plus San Diego / North County local shops
- Runs ONCE per invocation (designed for a GitHub Actions cron schedule, not a
  long-lived process) — loads the "seen" cache from disk, does one pass over
  every store, alerts on anything new, then writes the cache back out so the
  workflow can commit it. See .github/workflows/tracker.yml.
- Alerts go to a Discord webhook (with a product image embed when available)
  and, for real drops (not the first-run seed), an email via Gmail SMTP.
"""

import os
import json
import re
import time
import smtplib
import logging
import concurrent.futures
import urllib.parse
from collections import Counter
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from html import escape as _html_escape
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────
CACHE_FILE      = Path("seen_products.json")
REQUEST_TIMEOUT = 6
MAX_WORKERS     = 50

# Weekly digest = Nike SB NON-shoe items only (apparel + accessories).
# Shoes keep going out in real time via Discord/email; apparel never alerts in
# real time, it just accumulates and goes out once a week.
DIGEST_TZ      = ZoneInfo("America/Los_Angeles")
DIGEST_WEEKDAY = 4   # Friday
DIGEST_HOUR    = 7   # first run at/after 7 AM Pacific sends it

SB_KEYWORDS = [
    "sb dunk", "dunk sb", "nike sb", " sb low", " sb high",
    "sb mid", "sb zoom", "skate barge",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/json,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ── Discord ───────────────────────────────────────────────────────────────────
# Supports posting to multiple Discord servers — set DISCORD_WEBHOOK_URL for the
# primary server, and optionally DISCORD_WEBHOOK_URL_2 (a second server, e.g. a
# friend's), DISCORD_WEBHOOK_URL_3, etc. Any unset ones are just skipped.
DISCORD_WEBHOOKS = [
    v for v in (
        os.environ.get("DISCORD_WEBHOOK_URL"),
        os.environ.get("DISCORD_WEBHOOK_URL_2"),
        os.environ.get("DISCORD_WEBHOOK_URL_3"),
    )
    if v
]

# Mute-by-reaction (optional): needs a Discord bot in the PRIMARY server. Without
# DISCORD_BOT_TOKEN everything below is skipped and alerts behave exactly as before.
DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
DISCORD_OWNER_ID  = os.environ.get("DISCORD_OWNER_ID", "").strip()   # optional: only this user's reaction mutes
DISCORD_API       = "https://discord.com/api/v10"
MUTE_EMOJI        = "\U0001F6AB"   # 🚫 you add this to mute
OK_EMOJI          = "\u2705"       # ✅ the bot adds this once the mute is registered
MSG_KEEP_SECONDS  = 7 * 86400       # how long an alert stays muteable via reaction
MSG_PAGES         = 4               # channel history pages (100 msgs each) scanned per run

# Channel routing: the primary webhook's channel (#sb-drops) gets Nike Dunks + Nike SB
# Dunks ONLY. Every other SB shoe goes to #sbs, posted by the bot (needs DISCORD_BOT_TOKEN;
# without it, or if that post fails, it falls back to the primary channel so nothing is lost).
DISCORD_SBS_CHANNEL_ID = os.environ.get("DISCORD_SBS_CHANNEL_ID", "1562917305531506738").strip()

def send_discord(find: dict):
    """Post to every configured webhook. Returns {"mid", "cid"} for the PRIMARY
    webhook's message when a bot token is configured (needed for reaction muting), else None."""
    if not DISCORD_WEBHOOKS:
        log.warning("No DISCORD_WEBHOOK_URL* env vars set — skipping Discord alert")
        return None
    local_line = "\n📍 **LOCAL — CHECK IN-STORE**" if find.get("local") else ""
    embed = {
        "title": find["title"],
        "url": find["link"],
        "description": (
            f"**{find['store']}**{local_line}\n"
            f"💵 ${find['price']}\n"
            f"📏 {find['sizes']}"
        ),
        "color": 0xFF6A00,
    }
    if find.get("image"):
        embed["image"] = {"url": find["image"]}
    if find.get("flip"):
        content = "🔔 NOW AVAILABLE — sizes just unlocked"
    elif find.get("restock"):
        content = "🔁 RESTOCK — sizes back in stock"
    elif find.get("store") == "Nike SNKRS":
        content = "🗓️ SNKRS Dunk launch announced"
    elif find.get("feed") == "dunks":
        content = "🛹 New Dunk detected"
    else:
        content = "🛹 New SB's detected"
    primary_url = os.environ.get("DISCORD_WEBHOOK_URL")
    posted = None
    to_sbs = find.get("feed") == "sbs" and bool(DISCORD_BOT_TOKEN and DISCORD_SBS_CHANNEL_ID)
    if to_sbs:
        e = dict(embed)
        e["footer"] = {"text": f"React {MUTE_EMOJI} to mute this shoe (Discord + email)"}
        try:
            r = _bot("POST", f"/channels/{DISCORD_SBS_CHANNEL_ID}/messages", json={"content": content, "embeds": [e]})
            if r.status_code < 300:
                m = r.json()
                posted = {"mid": m["id"], "cid": m["channel_id"]}
            else:
                log.warning("#sbs post failed (HTTP %s %s) — sending to the main channel instead", r.status_code, r.text[:160])
                to_sbs = False
        except (requests.RequestException, ValueError, KeyError) as ex:
            log.warning("#sbs post failed (%s) — sending to the main channel instead", ex)
            to_sbs = False
    for webhook_url in DISCORD_WEBHOOKS:
        if to_sbs and webhook_url == primary_url:
            continue   # already posted to #sbs by the bot
        is_primary = bool(DISCORD_BOT_TOKEN) and webhook_url == primary_url
        e = dict(embed)
        if is_primary:
            e["footer"] = {"text": f"React {MUTE_EMOJI} to mute this shoe (Discord + email)"}
        try:
            resp = requests.post(
                webhook_url, json={"content": content, "embeds": [e]},
                params={"wait": "true"} if is_primary else None, timeout=10,
            )
            resp.raise_for_status()
            if is_primary:
                m = resp.json()
                posted = {"mid": m["id"], "cid": m["channel_id"]}
        except (requests.RequestException, ValueError, KeyError) as ex:
            log.warning(f"Discord post failed for one webhook: {ex}")
    return posted

# ── Mute by reaction ──────────────────────────────────────────────────────────
_STYLE_RE   = re.compile(r"(?<![A-Z0-9])([A-Z]{2}\d{4}-\d{3})(?!\d)")
_TITLE_STOP = {"nike", "sb", "mens", "men", "s", "shoe", "shoes"}
TITLE_KEY_MIN_TOKENS = 5   # a bare "Dunk Low Pro" must never mute every colorway

def _title_tokens(title: str) -> list:
    return [t for t in re.sub(r"[^a-z0-9]+", " ", (title or "").lower().replace("\u2019", "'").replace("'", "")).split()
            if t not in _TITLE_STOP]

def title_key(title: str):
    toks = _title_tokens(title)
    return ("t:" + " ".join(toks)) if len(toks) >= TITLE_KEY_MIN_TOKENS else None

# Cross-store name match: stores word the same shoe differently ("Dunk Low Pro Filbert"
# vs "Dunk Low Pro- Filbert/Light Chocolate-Black-Burgundy Ash") and many have no style
# code in their SKUs. A find counts as muted when one title's words are ALL contained in
# the other's, AND the shared words include something beyond model names / basic colors
# (e.g. "filbert") \u2014 so muting "Air Max Ishod Black" never mutes every black Ishod.
NAME_MATCH_MIN_TOKENS = 2   # after dropping the words below, e.g. {dunk, filbert}
_NAME_DROP    = {"sale", "low", "high", "mid", "pro", "og", "qs", "prm", "premium", "skateboard", "skateboarding", "skate",
                 "se", "lifestyle", "copy", "online", "and", "x"}
_NAME_NORMAL  = {"lite": "light", "lt": "light", "dk": "dark", "blk": "black", "wht": "white", "gry": "grey", "gray": "grey"}
_GENERIC_WORDS = {
    "dunk", "zoom", "air", "max", "blazer", "janoski", "ishod", "nyjah", "force", "react", "leo", "tennis",
    "classic", "code", "vertebrae", "ps8", "paul", "rodriguez", "p", "rod", "iso", "dg", "l", "plus",
    "black", "white", "grey", "gray", "red", "blue", "green", "brown", "pink", "purple", "yellow", "orange",
    "gold", "silver", "navy", "tan", "cream", "sail", "gum", "light", "dark", "deep", "university",
    "varsity", "summit", "metallic", "royal", "medium", "pale", "team", "game",
    "pogo", "chron", "bruin", "trainer", "adversary", "kobe", "protro", "retro", "baker", "slip", "wair",
    "af1", "1", "2", "3", "4", "58", "gt", "sp", "nba", "off", "noir", "multi", "color", "anthracite", "wolf",
    "womens", "wmns",
}
_COLOR_WORDS = {
    "black", "white", "grey", "red", "blue", "green", "brown", "pink", "purple", "yellow", "orange", "gold",
    "silver", "navy", "tan", "cream", "sail", "gum", "light", "dark", "deep", "university", "varsity", "summit",
    "metallic", "royal", "medium", "pale", "wolf", "anthracite", "noir", "off", "multi", "color",
}

def _name_set(title: str) -> frozenset:
    toks = (_NAME_NORMAL.get(t, t) for t in _title_tokens(title))
    return frozenset(t for t in toks if t not in _NAME_DROP)

def handle_text(link: str) -> str:
    """Words from a product URL slug — vague titles ("Nike SB Air Force 1 Low Shoes") often
    carry the colorway there (.../air-force-1-low-shoes-cool-grey-court-green). Shopify's
    "-copy" / "-7" de-dupe suffixes are stripped."""
    m = re.search(r"/(?:products|launch/t)/([^/?#]+)", link or "")
    if not m:
        return ""
    h = m.group(1).lower()
    if re.search(r"-copy(-\d+)?$", h):
        return ""   # a duplicated old page — its slug often names a different colorway
    while True:
        h2 = re.sub(r"-(copy|online|\d{1,2})$", "", h)
        if h2 == h:
            break
        h = h2
    return h.replace("-", " ")

def _full_name(title: str, link: str = "") -> frozenset:
    """Title words, plus the URL's words ONLY when the title names no colorway at all
    ("Nike SB PS8 Shoes") — when the title has one, it wins over a possibly stale URL."""
    t = _name_set(title)
    if any(w in _COLOR_WORDS or (w not in _GENERIC_WORDS and not w.isdigit()) for w in t):
        return t
    return t | _name_set(handle_text(link))

def names_match(a, b) -> bool:
    """a / b: a title string, or an already-built name set (title + URL words)."""
    sa = a if isinstance(a, frozenset) else _name_set(a)
    sb = b if isinstance(b, frozenset) else _name_set(b)
    for small, big in ((sa, sb), (sb, sa)):
        if len(small) >= NAME_MATCH_MIN_TOKENS and small <= big \
                and any(t not in _GENERIC_WORDS and not t.isdigit() for t in small):
            return True
    return False

_STYLE_LOOSE_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{2}\d{4})-?(\d{3})(?!\d)")   # also "IR0460001"

def style_code(p: dict):
    """Nike style code (e.g. HQ1625-201) for a Shopify product: from the variant SKUs first,
    else from tags, the URL handle, or the description — many stores leave SKUs blank."""
    codes = Counter()
    for v in p.get("variants", []) or []:
        m = _STYLE_LOOSE_RE.search((v.get("sku") or "").upper())
        if m:
            codes[f"{m.group(1)}-{m.group(2)}"] += 1
    if not codes:
        tags = p.get("tags") or []
        tags = ",".join(tags) if isinstance(tags, list) else str(tags)
        handle = p.get("handle") or ""             # e.g. "nike-hf5441-107-dunk-low-..."
        body = re.sub(r"<[^>]+>", " ", p.get("body_html") or "")
        for text in (tags, handle, body):          # first source that has one wins
            for m in _STYLE_RE.finditer(text.upper()):
                codes[m.group(1)] += 1
            if codes:
                break
    return codes.most_common(1)[0][0] if codes else None

def shoe_keys(p, title: str, pid: str) -> list:
    """Identity keys for a find, strongest first: s: Nike style code from the SKUs
    (matches the same shoe across stores), t: normalized full title (exact match
    only — never fuzzy), p: this exact store listing (always present)."""
    keys = []
    if p:
        sc = style_code(p)
        if sc:
            keys.append("s:" + sc)
    tk = title_key(title)
    if tk:
        keys.append(tk)
    keys.append("p:" + pid)
    return keys

def is_muted(find: dict, cache: dict) -> bool:
    muted = cache.get("muted", {})
    if any(k in muted for k in find.get("keys", [])):
        return True
    title, link = find.get("title", ""), find.get("link", "")
    mine = {_name_set(title), _full_name(title, link)}
    theirs = set()
    for e in muted.values():
        theirs.add(_name_set(e.get("title", "")))
        if e.get("link"):
            theirs.add(_full_name(e.get("title", ""), e["link"]))
    return any(names_match(a, b) for a in mine for b in theirs)

def _bot(method: str, path: str, **kw):
    r = requests.request(method, DISCORD_API + path, headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}, timeout=10, **kw)
    if r.status_code == 429:
        try: wait = min(float(r.json().get("retry_after", 1)), 5)
        except Exception: wait = 1
        time.sleep(wait)
        r = requests.request(method, DISCORD_API + path, headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}, timeout=10, **kw)
    return r

def _react(cid: str, mid: str, emoji: str, remove: bool = False):
    path = f"/channels/{cid}/messages/{mid}/reactions/{urllib.parse.quote(emoji)}/@me"
    try:
        r = _bot("DELETE" if remove else "PUT", path)
        if r.status_code >= 300:
            log.warning("Discord reaction %s %s failed: HTTP %s %s", "remove" if remove else "add", emoji, r.status_code, r.text[:120])
    except requests.RequestException as e:
        log.warning("Discord reaction call failed: %s", e)
    time.sleep(0.3)

def record_alert(cache: dict, find: dict, posted):
    """Remember which Discord message belongs to which shoe, and pre-add the 🚫 so muting is one tap."""
    if not (posted and DISCORD_BOT_TOKEN):
        return
    cache.setdefault("msgs", {})[posted["mid"]] = {
        "cid": posted["cid"], "keys": find.get("keys", []), "title": find["title"], "t": int(time.time()),
        "link": find.get("link", ""),
    }
    _react(posted["cid"], posted["mid"], MUTE_EMOJI)

def _reacted_by_owner(cid: str, mid: str) -> bool:
    r = _bot("GET", f"/channels/{cid}/messages/{mid}/reactions/{urllib.parse.quote(MUTE_EMOJI)}", params={"limit": 100})
    if r.status_code != 200:
        log.warning("Reaction user lookup failed: HTTP %s", r.status_code)
        return True   # fail open to the count we already saw
    return any(u.get("id") == DISCORD_OWNER_ID for u in r.json())

def process_mutes(cache: dict):
    """Read 🚫 reactions on recent alerts (one history fetch per channel) and sync cache['muted']."""
    if not DISCORD_BOT_TOKEN:
        return
    msgs  = cache.setdefault("msgs", {})
    muted = cache.setdefault("muted", {})
    now = int(time.time())
    for mid in [m for m, v in msgs.items() if now - v.get("t", 0) > MSG_KEEP_SECONDS]:
        del msgs[mid]
    if not msgs:
        return
    by_channel = {}
    for mid, v in msgs.items():
        by_channel.setdefault(v["cid"], set()).add(mid)
    for cid, wanted in by_channel.items():
        found, before = {}, None
        for _ in range(MSG_PAGES):
            params = {"limit": 100}
            if before:
                params["before"] = before
            r = _bot("GET", f"/channels/{cid}/messages", params=params)
            if r.status_code != 200:
                log.warning("Mute check skipped: channel history HTTP %s %s", r.status_code, r.text[:160])
                return            # keep existing mutes untouched
            page = r.json()
            if not page:
                break
            for m in page:
                if m["id"] in wanted:
                    found[m["id"]] = m
            before = page[-1]["id"]
            if len(found) == len(wanted):
                break
        for mid, m in found.items():
            info = msgs[mid]
            rx = {}
            for x in m.get("reactions", []) or []:
                rx[x["emoji"].get("name")] = x
            mute_rx = rx.get(MUTE_EMOJI)
            others = (mute_rx["count"] - (1 if mute_rx.get("me") else 0)) if mute_rx else 0
            if others > 0 and DISCORD_OWNER_ID:
                others = 1 if _reacted_by_owner(cid, mid) else 0
            ok_me = bool(rx.get(OK_EMOJI, {}).get("me"))
            if others > 0:
                for k in info["keys"]:
                    ent = muted.setdefault(k, {"mids": [], "title": info.get("title", ""), "link": info.get("link", ""), "t": now})
                    if mid not in ent["mids"]:
                        ent["mids"].append(mid)
                if not ok_me:
                    log.info("MUTED via reaction: %s  keys=%s", info.get("title"), info["keys"])
                    _react(cid, mid, OK_EMOJI)
            else:
                changed = False
                for k in info["keys"]:
                    ent = muted.get(k)
                    if ent and mid in ent["mids"]:
                        ent["mids"].remove(mid)
                        changed = True
                        if not ent["mids"]:
                            del muted[k]
                if changed:
                    log.info("UN-MUTED (reaction removed): %s", info.get("title"))
                if ok_me:
                    _react(cid, mid, OK_EMOJI, remove=True)

# ── Email (Gmail SMTP) ───────────────────────────────────────────────────────
GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD")
EMAIL_TO           = os.environ.get("EMAIL_TO", GMAIL_ADDRESS)

def send_email(finds: list, subject: str | None = None):
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and EMAIL_TO):
        log.warning("Gmail SMTP env vars not set — skipping email alert")
        return

    # Plain-text fallback (for clients that don't render HTML)
    text_lines = []
    for f in finds:
        local_tag = " (LOCAL — check in-store)" if f.get("local") else ""
        text_lines.append(
            f"{f['store']}{local_tag}\n"
            f"{f['title']} — ${f['price']}\n"
            f"Sizes: {f['sizes']}\n"
            f"{f['link']}\n"
        )
    text_body = "\n---\n".join(text_lines)

    # HTML version — includes the product image (same URL used in the Discord embed)
    cards = []
    for f in finds:
        local_tag = " 📍 <b>LOCAL — check in-store</b>" if f.get("local") else ""
        img_html = (
            f'<img src="{_html_escape(f["image"])}" alt="" '
            f'style="max-width:320px;width:100%;border-radius:8px;'
            f'margin:8px 0;display:block;">'
            if f.get("image") else ""
        )
        cards.append(f"""
        <div style="margin-bottom:28px;padding-bottom:24px;border-bottom:1px solid #e2e2e2;">
          <div style="font-weight:600;font-size:14px;color:#555;">{_html_escape(f['store'])}{local_tag}</div>
          <a href="{_html_escape(f['link'])}" style="font-size:16px;font-weight:700;color:#ff6a00;text-decoration:none;">
            {_html_escape(f['title'])}
          </a>
          <div style="font-size:14px;margin-top:4px;">💵 ${_html_escape(str(f['price']))}</div>
          <div style="font-size:14px;">📏 {_html_escape(f['sizes'])}</div>
          {img_html}
          <a href="{_html_escape(f['link'])}" style="font-size:13px;color:#1a73e8;">{_html_escape(f['link'])}</a>
        </div>""")
    html_body = f"""
    <html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#111;max-width:480px;margin:0 auto;">
      <h2 style="color:#ff6a00;">🛹 SB Radar — {len(finds)} new drop(s)</h2>
      {''.join(cards)}
    </body></html>"""

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject or f"SB Radar: {len(finds)} new SB's detected"
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [EMAIL_TO], msg.as_string())

def send_weekly_digest(finds: list):
    """Weekly Friday recap of Nike SB apparel & accessories found since the last digest."""
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD and EMAIL_TO):
        log.warning("Gmail SMTP env vars not set — skipping weekly digest")
        return
    week_label = datetime.now(DIGEST_TZ).strftime("%b %d, %Y")
    text_lines = []
    for f in finds:
        local_tag = " (LOCAL — check in-store)" if f.get("local") else ""
        text_lines.append(
            f"{f['store']}{local_tag}\n"
            f"{f['title']} — ${f['price']}\n"
            f"Sizes: {f['sizes']}\n"
            f"{f['link']}\n"
        )
    text_body = "\n---\n".join(text_lines)
    cards = []
    for f in finds:
        local_tag = " 📍 <b>LOCAL</b>" if f.get("local") else ""
        img_html = (
            f'<img src="{_html_escape(f["image"])}" alt="" '
            f'style="max-width:320px;width:100%;border-radius:8px;margin:8px 0;display:block;">'
            if f.get("image") else ""
        )
        cards.append(
            '<div style="margin-bottom:28px;padding-bottom:24px;border-bottom:1px solid #e2e2e2;">'
            + f'<div style="font-weight:600;font-size:14px;color:#555;">{_html_escape(f["store"])}{local_tag}</div>'
            + f'<a href="{_html_escape(f["link"])}" style="font-size:16px;font-weight:700;color:#ff6a00;text-decoration:none;">{_html_escape(f["title"])}</a>'
            + f'<div style="font-size:14px;margin-top:4px;">💵 ${_html_escape(str(f["price"]))}</div>'
            + f'<div style="font-size:14px;">📏 {_html_escape(f["sizes"])}</div>'
            + img_html
            + f'<a href="{_html_escape(f["link"])}" style="font-size:13px;color:#1a73e8;">{_html_escape(f["link"])}</a>'
            + '</div>'
        )
    html_body = (
        '<html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#111;max-width:480px;margin:0 auto;">'
        + f'<h2 style="color:#ff6a00;">🛹 SB Radar Weekly — Apparel & Accessories ({week_label}, {len(finds)} item(s))</h2>'
        + ''.join(cards)
        + '</body></html>'
    )
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"SB Radar Weekly: {len(finds)} SB apparel & accessories — {week_label}"
    msg["From"]    = GMAIL_ADDRESS
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, [EMAIL_TO], msg.as_string())

# ── Store list ────────────────────────────────────────────────────────────────
STORES = [
    # ── NATIONAL (verified-reachable, DNS-checked Sep 2026) ─────────────────
    {"name": "Doubler Kicks", "url": "https://doublerkicks.com"},
    {"name": "Humidity Skateshop", "url": "https://humiditynola.com"},
    {"name": "Bluetile Skateboards", "url": "https://bluetilesc.com"},
    {"name": "The Block Skate Supply", "url": "https://theblockskatesupply.com"},
    {"name": "Unheardof", "url": "https://unheardofbrand.com"},
    {"name": "Flatspot", "url": "https://www.flatspot.com"},
    {"name": "Stardust Skate Shop", "url": "https://www.stardustskateshop.com"},
    {"name": "Reset Mercantile", "url": "https://resetmercantile.com"},
    {"name": "Sequence", "url": "https://sequencealaska.com"},
    {"name": "BLX Skateshop", "url": "https://blxskateshop.com"},
    {"name": "Cowtown Skateboards", "url": "https://cowtownskate.com"},
    {"name": "Sidewalk Surfer", "url": "https://sidewalksurfer.com"},
    {"name": "The Berrics", "url": "https://theberrics.com"},
    {"name": "Boarders Topanga", "url": "https://boarders.com"},
    {"name": "Crown Apparel", "url": "https://crownapparelco.com"},
    {"name": "Defy Boardshop", "url": "https://defyboardshop.com"},
    {"name": "Icon Boardshop", "url": "https://iconboardshop.com"},
    {"name": "Innercity Skate", "url": "https://innercityskate.com"},
    {"name": "Kingswell CA", "url": "https://kingswell.com"},
    {"name": "Brooklyn Projects", "url": "https://brooklynprojects.com"},
    {"name": "New Star", "url": "https://newstarshop.com"},
    {"name": "Pawnshop Skate", "url": "https://pawnshopskate.com"},
    {"name": "Transport Skate", "url": "https://transportskate.com"},
    {"name": "Val Surf", "url": "https://valsurf.com"},
    {"name": "Asylum Skate", "url": "https://asylumskate.com"},
    {"name": "Contenders", "url": "https://contendersclothing.com"},
    {"name": "East 4th", "url": "https://east4th.com"},
    {"name": "Furnace Skate", "url": "https://furnaceskate.com"},
    {"name": "Jacks Surfboards", "url": "https://jackssurfboards.com"},
    {"name": "Long Beach Skate", "url": "https://longbeachskate.com"},
    {"name": "Sixes and Sevens", "url": "https://sixesandsevensskate.com"},
    {"name": "Urban Feet", "url": "https://urbanfeet.com"},
    {"name": "Pride Skate", "url": "https://prideskate.com"},
    {"name": "PLA Folsom", "url": "https://plaskate.com"},
    {"name": "510 Skateboarding", "url": "https://510skateboarding.com"},
    {"name": "Bills Wheels", "url": "https://billswheels.com"},
    {"name": "DLX SF", "url": "https://dlxsf.com"},
    {"name": "FTC SF", "url": "https://ftcsf.com"},
    {"name": "Skate Warehouse", "url": "https://skatewarehouse.com"},
    {"name": "Proof Lab", "url": "https://prooflab.com"},
    {"name": "303 Annex", "url": "https://303boards.com"},
    {"name": "Satellite Boardshop", "url": "https://satelliteboardshop.com"},
    {"name": "Skateboard Market", "url": "https://skateboardmarket.com"},
    {"name": "Relief Skate Supply", "url": "https://reliefskatesupply.com"},
    {"name": "Westside Skate Shop", "url": "https://westsideskate.com"},
    {"name": "Skatepark of Tampa", "url": "https://skateparkoftampa.com"},
    {"name": "The Compound FL", "url": "https://thecompoundfl.com"},
    {"name": "Island Water Sports", "url": "https://islandwatersports.com"},
    {"name": "Sunrise Skate", "url": "https://sunriseskate.com"},
    {"name": "Stratosphere", "url": "https://stratosphereskateboards.com"},
    {"name": "Clockwork Skate", "url": "https://clockworkskate.com"},
    {"name": "Uprise Skateshop", "url": "https://upriseskateshop.com"},
    {"name": "Jerics Skate", "url": "https://jerics.com"},
    {"name": "Fargo Skate", "url": "https://fargoskate.com"},
    {"name": "Rise Fort Wayne", "url": "https://risefortwayne.com"},
    {"name": "Minus Skate Shop", "url": "https://minusskateshop.com"},
    {"name": "Subsect Skate", "url": "https://subsect.com"},
    {"name": "Premier MI", "url": "https://thepremierstore.com"},
    {"name": "Olympia Skate Shop", "url": "https://olympiaskateshop.com"},
    {"name": "Brush Alley", "url": "https://brushalley.com"},
    {"name": "Exodus Rideshop", "url": "https://exodusrideshop.com"},
    {"name": "Modern Skate", "url": "https://modernskate.com"},
    {"name": "Plus Skateboarding", "url": "https://plusskateboarding.com"},
    {"name": "Skaters Advocate", "url": "https://skatersadvocate.com"},
    {"name": "Familia Skateboard Shop", "url": "https://familiaskateshop.com"},
    {"name": "Cal Surf MN", "url": "https://calsurf.com"},
    {"name": "Infinity Skate", "url": "https://infinityskate.com"},
    {"name": "Coureur Goods", "url": "https://coureurgoods.com"},
    {"name": "NJ Skateshop", "url": "https://njskateshop.com"},
    {"name": "Underground Skateshop", "url": "https://undergroundskateshop.com"},
    {"name": "Travel Skate Shop", "url": "https://travelskateshop.com"},
    {"name": "Seasons Skate Shop", "url": "https://seasonsskateshop.com"},
    {"name": "Richmond Hood", "url": "https://richmondhood.com"},
    {"name": "LICK NYC", "url": "https://licknyc.com"},
    {"name": "Bunger Sayville", "url": "https://bungersayville.com"},
    {"name": "Moms Skate Shop NY", "url": "https://momsskateshop.com"},
    {"name": "Supreme", "url": "https://supremenewyork.com"},
    {"name": "Tenant NY", "url": "https://tenantny.com"},
    {"name": "Homegrown NY", "url": "https://homegrownny.com"},
    {"name": "Black Sheep NC", "url": "https://blacksheepskateshop.com"},
    {"name": "Backdoor Skate Shop", "url": "https://backdoorskateshop.com"},
    {"name": "Endless Grind", "url": "https://endlessgrind.com"},
    {"name": "Permanent Vacation", "url": "https://permanentvacationshop.com"},
    {"name": "Push Skate Shop", "url": "https://pushskateshop.com"},
    {"name": "Stolen Skate Shop", "url": "https://stolenskateshop.com"},
    {"name": "Manifest Skate Shop", "url": "https://manifestskateshop.com"},
    {"name": "One Love Skate", "url": "https://oneloveskate.com"},
    {"name": "Core Boardshop", "url": "https://coreboardshop.com"},
    {"name": "CCS", "url": "https://ccs.com"},
    {"name": "Tactics", "url": "https://tactics.com"},
    {"name": "Home Base Skate Shop", "url": "https://homebaseskateshop.com"},
    {"name": "Nocturnal Skate", "url": "https://nocturnalskate.com"},
    {"name": "Radio Skateshop", "url": "https://radioskateshop.com"},
    {"name": "Continuum Skate Shop", "url": "https://continuumskateshop.com"},
    {"name": "Comfort Skateshop", "url": "https://comfortskateshop.com"},
    {"name": "No Comply TX", "url": "https://nocomply.com"},
    {"name": "Select TX", "url": "https://selecttx.com"},
    {"name": "Southside Skatepark", "url": "https://southsideskatepark.com"},
    {"name": "Fast Forward TX", "url": "https://fastforwardskate.com"},
    {"name": "Maven VT", "url": "https://mavenskate.com"},
    {"name": "Wonder Skate Shop", "url": "https://wonderskateshop.com"},
    {"name": "Skate Supply VA", "url": "https://skatesupplyva.com"},
    {"name": "35th North", "url": "https://35thnorth.com"},
    {"name": "New Yak City", "url": "https://newyakcity.com"},
    {"name": "Alumni Skate", "url": "https://alumniskate.com"},

    # ── SAN DIEGO / NORTH COUNTY LOCAL ───────────────────────────────────────
    {"name": "Pharmacy Temecula", "url": "https://pharmacyboardshop.com", "local_sd": True},
    {"name": "Sun Diego Boardshop", "url": "https://sundiego.com", "local_sd": True},
    {"name": "Rose Street Encinitas", "url": "https://rosestreetskateshop.com", "local_sd": True},
    {"name": "McGill's Skate Shop", "url": "https://www.mcgillsskateshop.com", "local_sd": True},
    {"name": "Heritage SKTBDS", "url": "https://heritageskateboards.com", "local_sd": True},
    {"name": "Status Skateshop SD", "url": "https://statusskateshop.com", "local_sd": True},
    {"name": "Soul Grind SD", "url": "https://soulgrind.com", "local_sd": True},
    {"name": "Slappy's Garage", "url": "https://slappysgarage.com", "local_sd": True},
    {"name": "Western Skate Co", "url": "https://westernskate.com", "local_sd": True},
    {"name": "House of Vista", "url": "https://houseofvista.bigcartel.com", "local_sd": True},
    # ── SOLEFEED 2-YEAR SB DUNK AUDIT (added Sep 2026) ─────────────────────
    {"name": "Embassy", "url": "https://embassyboardshop.com"},
    {"name": "Familia Skateshop", "url": "https://familiaskate.com"},
    {"name": "Kinetic", "url": "https://kineticskateboarding.com"},
    {"name": "Labor Skate Shop", "url": "https://laborskateshop.com"},
    {"name": "LB Skate", "url": "https://lbskate.com"},
    {"name": "Mainland", "url": "https://mainlandskateandsurf.com"},
    {"name": "Orchard", "url": "https://orchardshop.com"},
    {"name": "Overload", "url": "https://shopoverload.com", "local_sd": True},
    {"name": "Southside Boardshop", "url": "https://southsideskateshop.com"},
    {"name": "Arts & Rec", "url": "https://artsandrecstore.com", "local_sd": True},
    {"name": "No-Comply", "url": "https://www.nocomplyatx.com"},
    {"name": "Rock City Kicks", "url": "https://rockcitykicks.com"},
    {"name": "Drift House", "url": "https://drifthouse.com"},
    {"name": "After Hours Skateshop", "url": "https://afterhoursskateshop.com"},
    {"name": "Renarts", "url": "https://renarts.com"},
    {"name": "APB Store", "url": "https://apbstore.com"},
    {"name": "Civil", "url": "https://wearecivil.com"},
    {"name": "Theory Skate Shop", "url": "https://theoryskate.com"},
    {"name": "Recess Skate & Snow", "url": "https://recessrideshop.com"},
    {"name": "Xtreme", "url": "https://xbusa.com"},
    {"name": "People Skate and Snowboard", "url": "https://peopleskateandsnowboard.com"},
    {"name": "Barewires", "url": "https://barewiresurfshop.com"},
    {"name": "Pastime Skateshop", "url": "https://pastimeskateshop.com"},
    {"name": "VU Skate Shop", "url": "https://vuskateboardshop.com"},
    {"name": "Andrew", "url": "https://andrewdowntown.com"},
    {"name": "Deli Skate Supply", "url": "https://deliskatesupply.com"},
    {"name": "Blacklist", "url": "https://blacklistboardshop.com"},
    {"name": "Time Machine", "url": "https://timemachineskateshop.com"},
    {"name": "Blue Flowers", "url": "https://blueflowers.com"},
    {"name": "Swellophonic", "url": "https://chane.com"},
    {"name": "Geometric Skate Shop", "url": "https://geometricskateshop.com"},
    {"name": "Magnolia Skateshop", "url": "https://magnoliaskateshop.com"},
    {"name": "Detroit City Skateboards", "url": "https://detroitcityskateboards.com"},
    {"name": "303 Boards", "url": "https://303boards-com.myshopify.com"},
    {"name": "Cal Surf", "url": "https://cal-surf.com"},
    {"name": "Suite160", "url": "https://suite160.com"},
    # Sneaker boutiques (Oct 2026) — carry regular Nike Dunks, not just SB. Each one
    # was checked to serve a public Shopify /products.json before being added.
    {"name": "Undefeated", "url": "https://undefeated.com", "shopify_only": True},
    {"name": "Kith", "url": "https://kith.com", "shopify_only": True},
    {"name": "Concepts", "url": "https://cncpts.com", "shopify_only": True},
    {"name": "Bodega", "url": "https://bdgastore.com", "shopify_only": True},
    {"name": "A Ma Maniere", "url": "https://www.a-ma-maniere.com", "shopify_only": True},
    {"name": "Social Status", "url": "https://www.socialstatuspgh.com", "shopify_only": True},
    {"name": "Lapstone & Hammer", "url": "https://www.lapstoneandhammer.com", "shopify_only": True},
    {"name": "DTLR", "url": "https://www.dtlr.com", "shopify_only": True},
    {"name": "Extra Butter", "url": "https://extrabutterny.com", "shopify_only": True},
    {"name": "Packer Shoes", "url": "https://packershoes.com", "shopify_only": True},
    {"name": "Sneaker Politics", "url": "https://sneakerpolitics.com", "shopify_only": True},
    {"name": "Feature", "url": "https://www.featuresneakerboutique.com", "shopify_only": True},
    {"name": "Notre", "url": "https://www.notre-shop.com", "shopify_only": True},
    {"name": "Union LA", "url": "https://store.unionlosangeles.com", "shopify_only": True},
    {"name": "Shoe Palace", "url": "https://www.shoepalace.com", "shopify_only": True},
    {"name": "Xhibition", "url": "https://www.xhibition.co", "shopify_only": True},
    {"name": "Oneness", "url": "https://www.onenessboutique.com", "shopify_only": True},
    {"name": "Wish ATL", "url": "https://wishatl.com", "shopify_only": True},
    {"name": "Nice Kicks", "url": "https://shopnicekicks.com", "shopify_only": True},
    {"name": "Corporate", "url": "https://www.corporategotem.com", "shopify_only": True},
    {"name": "Burn Rubber", "url": "https://burnrubbersneakers.com", "shopify_only": True},
    {"name": "Sneakersnstuff", "url": "https://www.sneakersnstuff.com", "shopify_only": True},
    {"name": "Bows and Arrows", "url": "https://www.bowsandarrowsberkeley.com", "shopify_only": True},
    {"name": "Livestock", "url": "https://www.deadstock.ca", "shopify_only": True},
    {"name": "Haven", "url": "https://shop.havenshop.com", "shopify_only": True},
    # Nike's own SNKRS launch calendar (upcoming Dunk launches) — not Shopify.
    {"name": "Nike SNKRS", "url": "https://www.nike.com/launch", "snkrs": True},
]

# ── Cache ─────────────────────────────────────────────────────────────────────

def load_cache() -> dict:
    if CACHE_FILE.exists():
        data = json.loads(CACHE_FILE.read_text())
        if isinstance(data, list):
            return {"seen": set(data), "shopify": {}, "seeded_stores": set(), "stock": {}, "oos": {}, "avail": {}, "muted": {}, "msgs": {}}
        return {
            "seen": set(data.get("seen", [])),
            "shopify": data.get("shopify", {}),
            "seeded_stores": set(data.get("seeded_stores", [])),
            "weekly_digest": data.get("weekly_digest", []),
            "apparel_seeded": data.get("apparel_seeded", False),
            "last_digest_date": data.get("last_digest_date"),
            "stock": data.get("stock", {}),
            "oos": data.get("oos", {}),
            "avail": data.get("avail", {}),
            "muted": data.get("muted", {}),
            "msgs": data.get("msgs", {}),
            "nike_dunk_seeded": data.get("nike_dunk_seeded", False),
            "boutique_reseed": data.get("boutique_reseed", False),
        }
    return {"seen": set(), "shopify": {}, "seeded_stores": set(), "weekly_digest": [],
            "apparel_seeded": False, "last_digest_date": None, "stock": {}, "oos": {}, "avail": {}, "muted": {}, "msgs": {}}

def save_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps({
        "seen": sorted(cache["seen"]),
        "shopify": cache["shopify"],
        "seeded_stores": sorted(cache["seeded_stores"]),
        "weekly_digest": cache.get("weekly_digest", []),
        "apparel_seeded": cache.get("apparel_seeded", False),
        "last_digest_date": cache.get("last_digest_date"),
        "stock": cache.get("stock", {}),
        "oos": cache.get("oos", {}),
        "avail": cache.get("avail", {}),
        "muted": cache.get("muted", {}),
        "msgs": cache.get("msgs", {}),
        "nike_dunk_seeded": cache.get("nike_dunk_seeded", False),
        "boutique_reseed": cache.get("boutique_reseed", False),
    }, indent=0))

# ── Detection & fetching ──────────────────────────────────────────────────────

def is_sb(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in SB_KEYWORDS)

def is_dunk(text: str) -> bool:
    return "dunk" in text.lower()

_KIDS_RE = re.compile(r"\b(gs|bg|ps|td|tdv|psv|grade school|little kids?|big kids?|older kids'?|younger kids'?|kids'?|toddlers?|pre-?school|infants?|baby)\b", re.I)

def is_kids(title: str) -> bool:
    return bool(_KIDS_RE.search(title or ""))

_WOMENS_RE = re.compile(r"\b(women'?s?|womens|wmns|wms)\b|^\s*w\s+(nike|dunk|sb)\b|\(w\)|\bw nike\b", re.I)

def is_womens(title: str) -> bool:
    return bool(_WOMENS_RE.search(title or ""))

def is_nike_dunk(title: str, vendor: str = "") -> bool:
    """Any Nike Dunk, SB or not (title says Nike, or the store lists Nike as the brand)."""
    t = (title or "").lower()
    return "dunk" in t and ("nike" in t or "nike" in (vendor or "").lower())

def feed_for(title: str, vendor: str = ""):
    """Which Discord channel a shoe belongs in:
    'dunks' -> #sb-drops: Nike Dunks + Nike SB Dunks (adult sizes) — and nothing else
    'sbs'   -> #sbs: every other Nike SB shoe (plus kids' SB Dunks)
    None    -> not tracked (non-SB, non-Dunk, women's, or a kids' non-SB Dunk)"""
    if is_womens(title):
        return None
    if is_dunk(title) and (is_sb(title) or is_nike_dunk(title, vendor)) and not is_kids(title):
        return "dunks"
    if is_sb(title):
        return "sbs"
    return None

APPAREL_KEYWORDS = [
    "hoodie", "t-shirt", "tee", "shirt", "jacket", "crewneck", "sweatshirt",
    "sweatpants", "pants", "shorts", "hat", "cap", "beanie", "socks",
    "backpack", "bag", "tote", "sweater", "pullover", "jersey", "vest",
    "sticker", "poster", "grip tape", "wheels", "trucks", "bearings",
    "gloves", "belt", "wallet", "deck",
    "pant", "longsleeve", "long sleeve", "long-sleeve", "trousers", "chino", "chinos",
    "short", "polo", "fleece", "flannel", "windbreaker", "anorak", "coaches", "overalls",
    "jorts", "sweater", "knit", "mask", "towel", "keychain", "lanyard",
]
FOOTWEAR_PRODUCT_TYPES = {"shoes", "shoe", "footwear", "sneakers", "sneaker", "skate shoes", "skate shoe"}

APPAREL_PRODUCT_TYPES = {"apparel", "headwear", "accessories", "accessory", "hardware", "clothing",
                         "pants", "bottoms", "tops", "shirts", "t-shirts", "tees", "outerwear", "jackets",
                         "hats", "hoodies", "sweatshirts", "shorts", "socks", "bags", "skateboards", "decks"}

def is_apparel(title: str, product_type: str = "") -> bool:
    # Block by Shopify product_type first (most reliable signal)
    if product_type.strip().lower() in APPAREL_PRODUCT_TYPES:
        return True
    # Shopify says it's footwear -> trust it (stops colorway words like "Cap Toe" etc. from hiding a shoe)
    if product_type.strip().lower() in FOOTWEAR_PRODUCT_TYPES:
        return False
    # Then check title for apparel keywords
    text = f"{title} {product_type}".lower()
    return any(re.search(rf"\b{re.escape(kw)}\b", text) for kw in APPAREL_KEYWORDS)

def is_shopify(url: str, cache: dict) -> bool:
    base = url.rstrip("/")
    if base in cache["shopify"]:
        return cache["shopify"][base]
    try:
        r = requests.get(f"{base}/products.json?limit=1", timeout=REQUEST_TIMEOUT, headers=HEADERS)
        result = r.status_code == 200 and "products" in r.text
    except Exception:
        result = False
    cache["shopify"][base] = result
    return result

def fetch_shopify(store: dict) -> list:
    all_products, page = [], 1
    base = store["url"].rstrip("/")
    while True:
        try:
            r = requests.get(
                f"{base}/products.json?limit=250&page={page}",
                timeout=REQUEST_TIMEOUT, headers=HEADERS,
            )
            if r.status_code != 200:
                if store.get("shopify_only") and page == 1:
                    log.warning(f"{store['name']} products.json HTTP {r.status_code} — skipped this run")
                break
            products = r.json().get("products", [])
            if not products:
                break
            all_products.extend(products)
            if len(products) < 250:
                break
            page += 1
        except Exception as e:
            log.warning(f"{store['name']} Shopify error p{page}: {e}")
            break
    return all_products

def fetch_html(store: dict) -> list:
    base  = store["url"].rstrip("/")
    found = []
    urls  = [base, f"{base}/collections/nike-sb", f"{base}/collections/nike", f"{base}/products"]
    for url in urls:
        try:
            r = requests.get(url, timeout=REQUEST_TIMEOUT, headers=HEADERS)
            if r.status_code != 200 or not is_sb(r.text.lower()):
                continue
            html = r.text
            candidates = re.findall(
                r'(?:alt|title|aria-label|data-name)=["\']([^"\'<>\n]{5,80})["\']', html, re.IGNORECASE
            )
            candidates += re.findall(r'<h[123][^>]*>([^<]{5,100})</h[123]>', html, re.IGNORECASE)
            for c in candidates:
                c = c.strip()
                if feed_for(c) and not is_apparel(c):
                    found.append({
                        "id":    f"{store['name']}::html::{c.lower()[:60]}",
                        "title": c,
                        "price": "See site",
                        "sizes": "Check site for sizes",
                        "link":  url,
                        "image": None,
                    })
            if found:
                break
        except Exception as e:
            log.warning(f"{store['name']} HTML error: {e}")
    return found

def format_sizes(product: dict) -> str:
    available = []
    for v in product.get("variants", []):
        if v.get("available", True):
            size = v.get("option1") or v.get("title", "")
            if size and size != "Default Title":
                available.append(size)
    if not available:
        return "Check site"
    def key(s):
        try: return float(re.sub(r"[^\d.]", "", s) or 999)
        except Exception: return 999
    return ", ".join(sorted(set(available), key=key))

WATCH_SIZES = ("11", "11.5")          # email-only restock watch (Dunks)
OOS_RUNS = 3                          # an item must read unavailable this many runs in a row (~15 min) to count as sold out
_SIZE_RE = re.compile(r"^\s*(?:us|m|men'?s)?\s*(11(?:\.0+)?|11\.5)\s*(?:us|m)?\s*$", re.I)

def watched_label(label: str):
    """Map a size label to '11' / '11.5' (men's only), else None."""
    m = _SIZE_RE.match(label or "")
    if not m:
        return None
    return "11.5" if float(m.group(1)) == 11.5 else "11"

def _num(l):
    try: return float(re.sub(r"[^\d.]", "", l) or 999)
    except Exception: return 999

def product_image(product: dict) -> str | None:
    images = product.get("images") or []
    if images:
        return images[0].get("src")
    if product.get("image"):
        return product["image"].get("src")
    return None

SNKRS_FEED    = "https://api.nike.com/product_feed/threads/v3/"
SNKRS_CHANNEL = "010794e5-35fe-4e32-aaff-cd2c74f89d61"   # Nike's SNKRS channel id (public)
SNKRS_PAGES   = 2                                        # newest 100 launch threads

def _snkrs_when(iso):
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(DIGEST_TZ)
        return "SNKRS launch " + dt.strftime("%a %b %d, %I:%M %p").replace(" 0", " ") + " PT"
    except Exception:
        return "On SNKRS — check app for launch time"

def _snkrs_image(o: dict, pi: dict):
    img = (pi.get("imageUrls") or {}).get("productImageUrl")
    if img:
        return img
    for n in ((o.get("publishedContent") or {}).get("nodes") or []):
        props = n.get("properties") or {}
        for k in ("squarishURL", "portraitURL", "landscapeURL"):
            if props.get(k):
                return props[k]
    return None

def check_snkrs(store: dict, cache: dict) -> list:
    """Upcoming Nike Dunk / SB Dunk launches from Nike's SNKRS feed. One alert per
    launch (style code), the first time it shows up — no stock tracking."""
    seen, finds, got = cache["seen"], [], set()
    for page in range(SNKRS_PAGES):
        try:
            r = requests.get(SNKRS_FEED, headers=HEADERS, timeout=REQUEST_TIMEOUT * 2, params={
                "anchor": page * 50, "count": 50,
                "filter": ["marketplace(US)", "language(en)", f"channelId({SNKRS_CHANNEL})"]})
            if r.status_code != 200:
                log.warning("Nike SNKRS feed HTTP %s", r.status_code)
                break
            objs = r.json().get("objects", []) or []
            cache.setdefault("_fetched", set()).add(store["name"])
        except Exception as e:
            log.warning("Nike SNKRS feed error: %s", e)
            break
        for o in objs:
            slug = (((o.get("publishedContent") or {}).get("properties") or {}).get("seo") or {}).get("slug", "")
            for pi in o.get("productInfo") or []:
                pc    = pi.get("productContent") or {}
                name  = pc.get("fullTitle") or pc.get("title") or ""
                color = pc.get("colorDescription") or ""
                title = f"{name} - {color}" if color else name
                if feed_for(title, "Nike") != "dunks":
                    continue
                sc  = ((pi.get("merchProduct") or {}).get("styleColor") or "").upper()
                pid = f"{store['name']}::{sc or slug}"
                if pid in seen or pid in got:
                    continue
                got.add(pid)
                keys = (["s:" + sc] if _STYLE_RE.fullmatch(sc) else []) + [k for k in [title_key(title)] if k] + ["p:" + pid]
                finds.append({
                    "id":    pid,
                    "store": store["name"],
                    "title": title,
                    "price": (pi.get("merchPrice") or {}).get("currentPrice", "?"),
                    "sizes": _snkrs_when((pi.get("launchView") or {}).get("startEntryDate")),
                    "link":  f"https://www.nike.com/launch/t/{slug}" if slug else store["url"],
                    "image": _snkrs_image(o, pi),
                    "keys":  keys,
                    "local": False,
                    "apparel": False,
                    "feed":  "dunks",
                    "gr":    True,
                })
    for f in finds:
        log.info(f"  NEW SNKRS [{store['name']}]: {f['title']} — {f['sizes']}")
    return finds

NEW_MAX_AGE_DAYS = 10   # a never-seen listing older than this is back-catalog, not a new drop

def _listing_age_days(p: dict) -> float:
    ts = p.get("published_at") or p.get("created_at")
    if not ts:
        return 0.0
    try:
        when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return (datetime.now(when.tzinfo) - when).total_seconds() / 86400
    except Exception:
        return 0.0

def check_store(store: dict, cache: dict) -> list:
    seen  = cache["seen"]
    stock = cache.get("stock", {})
    obs   = cache.setdefault("_stock_obs", {})   # observed this run; merged into cache["stock"] after alerts
    aobs  = cache.setdefault("_avail_obs", {})   # debounced available-size state per SB shoe, observed this run
    avail_prev = cache.get("avail", {})
    finds = []
    vendors = {}   # pid -> Shopify vendor, used to pick the channel for each find below

    fetched = cache.setdefault("_fetched", set())   # stores whose catalog actually loaded this run

    if store.get("snkrs"):
        return check_snkrs(store, cache)

    # Boutiques: Shopify feed only, re-tried every run (never cached as "not Shopify",
    # never homepage-scraped — scraping them produced duplicate, priceless junk alerts).
    if store.get("shopify_only") or is_shopify(store["url"], cache):
        products = fetch_shopify(store)
        if products:
            fetched.add(store["name"])
        for p in products:
            title = p.get("title", "")
            vendor = p.get("vendor", "") or ""
            feed = feed_for(title, vendor)
            if feed is None or is_womens((p.get("handle") or "").replace("-", " ")):
                continue   # also catches women's listings that only say so in the URL
            cache.setdefault("_links", {})[f"{store['name']}::{p.get('id')}"] = \
                f"{store['url'].rstrip('/')}/products/{p.get('handle','')}"
            product_type = p.get("product_type", "")
            apparel = is_apparel(title, product_type)
            if apparel and not is_sb(title):
                continue   # the weekly digest is SB apparel only
            vendors[f"{store['name']}::{p.get('id')}"] = vendor
            pid = f"{store['name']}::{p.get('id')}"
            pkeys = shoe_keys(p, title, pid)
            variants = p.get("variants", [])
            in_stock = any(x.get("available", True) for x in variants)
            if not apparel:
                obs[pid] = in_stock
            v = variants or [{}]
            restock_sizes = []
            size_back = []
            if not apparel:
                labels = {str(x.get("option1") or x.get("title") or "") for x in variants if x.get("available", True)} - {""}
                rec = avail_prev.get(pid)
                if isinstance(rec, list):            # legacy format [labels, t] from the first restock version
                    rec = {"a": rec[0], "m": {}}
                if rec is not None and "l" not in rec and any("|" in l for l in labels):
                    rec = None   # old "|"-joined record can't hold labels containing "|" — start fresh, no restock
                if rec is None:
                    new = {"l": sorted(labels), "m": {}}
                else:
                    # "l" = list of confirmed-available labels; "a" = legacy "|"-joined string
                    conf = set(rec["l"]) if "l" in rec else (set(rec["a"].split("|")) if rec.get("a") else set())
                    miss = dict(rec.get("m", {}))
                    for l in list(conf):
                        if l in labels:
                            miss.pop(l, None)
                        else:
                            miss[l] = miss.get(l, 0) + 1
                            if miss[l] >= OOS_RUNS:      # confirmed sold out
                                conf.discard(l); miss.pop(l)
                    newly = labels - conf                # genuinely back (was confirmed sold out)
                    if pid in seen:
                        restock_sizes = sorted(newly, key=_num)
                        if feed == "dunks":
                            size_back = sorted({watched_label(l) for l in newly if watched_label(l)}, key=float)
                    conf |= newly
                    new = {"l": sorted(conf), "m": miss}
                aobs[pid] = new
            if pid in seen:
                # Already alerted on this product. Re-alert only when it goes from
                # nothing purchasable -> purchasable (early listing unlocking, or a
                # restock). Products with no recorded stock state yet (cache from
                # before this feature) are just recorded silently.
                if restock_sizes and not ((not apparel) and stock.get(pid) is False and in_stock):
                    finds.append({
                        "id":    pid,
                        "store": store["name"],
                        "title": title,
                        "price": v[0].get("price", "?"),
                        "sizes": "RESTOCK — back: " + ", ".join(restock_sizes) + " (all available: " + format_sizes(p) + ")",
                        "link":  f"{store['url'].rstrip('/')}/products/{p.get('handle','')}",
                        "image": product_image(p),
                        "keys":  pkeys,
                        "local": store.get("local_sd", False),
                        "apparel": False,
                        "restock": True,
                    })
                if (not apparel) and stock.get(pid) is False and in_stock:
                    finds.append({
                        "id":    pid,
                        "store": store["name"],
                        "title": title,
                        "price": v[0].get("price", "?"),
                        "sizes": format_sizes(p),
                        "link":  f"{store['url'].rstrip('/')}/products/{p.get('handle','')}",
                        "image": product_image(p),
                        "keys":  pkeys,
                        "local": store.get("local_sd", False),
                        "apparel": False,
                        "flip":  True,
                    })
                elif size_back:
                    finds.append({
                        "id":    pid,
                        "store": store["name"],
                        "title": title,
                        "price": v[0].get("price", "?"),
                        "sizes": "RESTOCK — size " + " & ".join(size_back) + " back in stock (all available: " + format_sizes(p) + ")",
                        "link":  f"{store['url'].rstrip('/')}/products/{p.get('handle','')}",
                        "image": product_image(p),
                        "keys":  pkeys,
                        "local": store.get("local_sd", False),
                        "apparel": False,
                        "size_only": True,
                        "size_back": size_back,
                    })
                continue
            finds.append({
                "id":    pid,
                "store": store["name"],
                "title": title,
                "price": v[0].get("price", "?") if v else "?",
                "sizes": format_sizes(p) if in_stock else "None purchasable yet — will alert when sizes unlock",
                "link":  f"{store['url'].rstrip('/')}/products/{p.get('handle','')}",
                "image": product_image(p),
                        "keys":  pkeys,
                "local": store.get("local_sd", False),
                "apparel": apparel,
                # First time WE see it, but not a new drop: listed long ago (store seeded late,
                # catalog only partly loaded before, ...) -> record silently.
                "stale": _listing_age_days(p) > NEW_MAX_AGE_DAYS,
                # Nothing buyable -> record silently; the NOW-AVAILABLE alert fires if sizes unlock.
                "unbuyable": not in_stock,
            })
    elif not store.get("shopify_only"):
        html_finds = fetch_html(store)
        if html_finds:
            fetched.add(store["name"])
        for f in html_finds:
            if f["id"] not in seen:
                finds.append({**f, "store": store["name"], "local": store.get("local_sd", False), "apparel": False,
                              "keys": shoe_keys(None, f["title"], f["id"])})

    for f in finds:
        f["feed"] = feed_for(f["title"], vendors.get(f["id"], ""))
        f["gr"]   = not is_sb(f["title"])   # regular (non-SB) Nike Dunk — newly tracked as of Oct 2026

    for f in finds:
        kind = "APPAREL" if f.get("apparel") else ("SIZE-RESTOCK" if f.get("size_only") else ("RESTOCK" if f.get("restock") else ("NOW-AVAILABLE" if f.get("flip") else "SB")))
        log.info(f"  NEW {kind} [{store['name']}]: {f['title']} — ${f['price']}")
    return finds

# ── Main (single pass) ────────────────────────────────────────────────────────

def run():
    log.info("=" * 60)
    log.info("SB Radar | %d stores | single-pass run @ %s", len(STORES), datetime.now().isoformat(timespec="seconds"))
    log.info("=" * 60)

    cache     = load_cache()
    try:
        process_mutes(cache)
    except Exception as e:   # muting must never break alerting
        log.error("Mute sync failed (existing mutes kept): %s", e)
    first_run = len(cache["seen"]) == 0
    if first_run:
        log.info("First run — seeding cache silently. Only NEW drops will alert from here on.")
    # One-time fix-up (Oct 22): the boutiques were marked "seeded" while their feeds weren't
    # loading, so un-seed them; each gets a real silent seed pass the first time it loads.
    if not cache.get("boutique_reseed", False):
        cache["seeded_stores"] -= {s["name"] for s in STORES if s.get("shopify_only")}
        for s in STORES:
            if s.get("shopify_only"):
                cache["shopify"].pop(s["url"].rstrip("/"), None)
        cache["boutique_reseed"] = True

    all_finds = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        future_to_store = {ex.submit(check_store, store, cache): store for store in STORES}
        for future in concurrent.futures.as_completed(future_to_store):
            store = future_to_store[future]
            try:
                finds = future.result()
                all_finds.extend(finds)
            except Exception as e:
                log.error("Error on %s: %s", store["name"], e)

    # Mutes made before links were saved: fill in the product link from this run's catalog
    # (via the muted listing's p: key) so a vague muted title can match by its URL colorway.
    links = cache.pop("_links", {})
    pid_of_mid = {mid: k[2:] for k, e in cache.get("muted", {}).items() if k.startswith("p:") for mid in e.get("mids", [])}
    for e in cache.get("muted", {}).values():
        if not e.get("link"):
            for mid in e.get("mids", []):
                if links.get(pid_of_mid.get(mid)):
                    e["link"] = links[pid_of_mid[mid]]
                    break

    # NOTE: "seen"/stock state is committed to the cache only AFTER alerts are sent
    # (see below), so a run that dies mid-send re-alerts next run instead of
    # silently swallowing the drop.

    # New stores get one silent seed pass (like the global first-run seed, but
    # scoped per-store) so adding a store never floods alerts for its whole
    # existing catalog — only genuinely new drops alert from its second check on.
    checked_names = {store["name"] for store in STORES}
    fetched = cache.pop("_fetched", set())
    new_stores = checked_names - cache["seeded_stores"]
    if new_stores & fetched:
        log.info("Silently seeding %d new store(s): %s", len(new_stores & fetched), ", ".join(sorted(new_stores & fetched)))
    alertable_finds = [f for f in all_finds if f["store"] not in new_stores]
    _dedup = {}
    for f in alertable_finds:   # same listing can be found twice in one run — alert once
        _dedup.setdefault((f["id"], bool(f.get("flip")), bool(f.get("restock")), bool(f.get("size_only"))), f)
    alertable_finds = list(_dedup.values())
    _quiet = [f for f in alertable_finds if f.get("stale") or (f.get("unbuyable") and not f.get("apparel"))]
    if _quiet:
        log.info("Recorded silently (old listing or nothing purchasable): %d — %s", len(_quiet), "; ".join(f["title"][:40] for f in _quiet[:15]))
        alertable_finds = [f for f in alertable_finds if f not in _quiet]
    _muted_now = [f for f in alertable_finds if not f.get("apparel") and is_muted(f, cache)]
    if _muted_now:
        log.info("Muted: suppressing %d alert(s): %s", len(_muted_now), "; ".join(f["title"][:50] for f in _muted_now))
        alertable_finds = [f for f in alertable_finds if f not in _muted_now]
    # Only a store whose catalog actually loaded counts as seeded — otherwise a store that
    # fails on its seed run would dump its whole back catalog as "new" once it loads.
    cache["seeded_stores"] |= (checked_names & fetched)

    # Regular (non-SB) Nike Dunks were never tracked before, so the first run after that
    # ships marks the ones already listed at existing stores as seen WITHOUT alerting —
    # otherwise #sb-drops would get every GR Dunk currently in stock everywhere.
    if not cache.get("nike_dunk_seeded", False):
        n = sum(1 for f in alertable_finds if f.get("gr"))
        log.info("Nike Dunk seed pass: marked %d existing non-SB Dunk listing(s) seen, no alerts.", n)
        alertable_finds = [f for f in alertable_finds if not f.get("gr")]
        cache["nike_dunk_seeded"] = True

    def _commit_state():
        for f in all_finds:
            cache["seen"].add(f["id"])
        # Stock state: record what we observed. Flipped items are now in_stock=True,
        # so they won't re-alert until they go fully unavailable and back again.
        stock_c = cache.setdefault("stock", {})
        oos_c   = cache.setdefault("oos", {})
        for pid, ok in cache.pop("_stock_obs", {}).items():
            if ok:
                stock_c[pid] = True
                oos_c.pop(pid, None)
            else:
                n = oos_c.get(pid, 0) + 1
                if stock_c.get(pid) is not True or n >= OOS_RUNS:   # first sight / already sold out, or newly confirmed sold out
                    stock_c[pid] = False
                    oos_c.pop(pid, None)
                else:
                    oos_c[pid] = n                              # maybe a blip — wait for confirmation
        cache.setdefault("avail", {}).update(cache.pop("_avail_obs", {}))

    # Apparel/accessories were never tracked before, so the first run after this
    # feature ships marks every existing SB apparel item as seen WITHOUT adding
    # it to the digest — otherwise the first digest would be the whole back catalog.
    apparel_seed_run = not cache.get("apparel_seeded", False)
    size_finds    = [f for f in alertable_finds if f.get("size_only")]   # email-only (Dunk size 11/11.5 restock)
    shoe_finds    = [f for f in alertable_finds if not f.get("apparel") and not f.get("size_only")]
    apparel_finds = [] if (first_run or apparel_seed_run) else [f for f in alertable_finds if f.get("apparel")]
    if apparel_seed_run:
        n = sum(1 for f in all_finds if f.get("apparel"))
        log.info("Apparel seed pass: marked %d existing SB apparel/accessory item(s) seen, none added to digest.", n)
        cache["apparel_seeded"] = True

    if apparel_finds:
        digest_buf = cache.get("weekly_digest", [])
        for f in apparel_finds:
            digest_buf.append({
                "store": f["store"], "title": f["title"], "price": f["price"],
                "sizes": f["sizes"], "link": f["link"],
                "image": f.get("image"), "local": f.get("local", False),
            })
        cache["weekly_digest"] = digest_buf
        log.info("Added %d SB apparel/accessory item(s) to weekly digest (%d total queued).", len(apparel_finds), len(digest_buf))

    if first_run:
        _commit_state()
        save_cache(cache)
        log.info("Cache seeded: %d products tracked. No alerts sent on first run.", len(cache["seen"]))
        return

    if not shoe_finds and not size_finds:
        _commit_state()
        save_cache(cache)
        maybe_send_weekly_digest(cache)
        log.info("No new SB shoes this run.")
        return

    if size_finds:
        try:
            send_email(size_finds, "SB Radar: SB Dunk size " + " / ".join(sorted({x for f in size_finds for x in f["size_back"]}, key=float)) + " RESTOCK")
        except Exception as e:
            log.error("Size-restock email failed: %s", e)

    # Real-time alerts are SHOES ONLY — apparel goes to the weekly digest instead.
    for f in shoe_finds:
        try:
            record_alert(cache, f, send_discord(f))
        except Exception as e:
            log.error("Discord alert failed for %s: %s", f["title"], e)

    dunk_finds = [f for f in shoe_finds if f.get("feed") == "dunks" and not f.get("restock")]
    if dunk_finds:
        n_live = sum(1 for f in dunk_finds if f.get("flip"))
        subject = f"SB Radar: {len(dunk_finds)} new Dunk(s) detected"
        if n_live:
            subject = (f"SB Radar: {n_live} Dunk(s) NOW AVAILABLE" if n_live == len(dunk_finds)
                       else f"SB Radar: {len(dunk_finds)} Dunk(s) — {n_live} now available")
        try:
            send_email(dunk_finds, subject)
        except Exception as e:
            log.error("Email alert failed: %s", e)
    else:
        log.info("No Dunks this run (%d non-Dunk SB shoe find(s) -> #sbs) — email skipped.", len(shoe_finds))

    # Alerts are out — now it is safe to mark everything seen / record stock state.
    _commit_state()
    save_cache(cache)
    maybe_send_weekly_digest(cache)

    log.info("Run complete: %d SB shoe alert(s) sent (%d new, %d now-available).",
             len(shoe_finds), sum(1 for f in shoe_finds if not f.get("flip")), sum(1 for f in shoe_finds if f.get("flip")))


def maybe_send_weekly_digest(cache: dict):
    """Send the apparel/accessories digest once per week: the first run on
    Friday at/after DIGEST_HOUR Pacific. last_digest_date stops it re-sending
    on every 5-minute run for the rest of Friday."""
    now = datetime.now(DIGEST_TZ)
    today = now.date().isoformat()
    if now.weekday() != DIGEST_WEEKDAY or now.hour < DIGEST_HOUR:
        return
    if cache.get("last_digest_date") == today:
        return
    digest_buf = cache.get("weekly_digest", [])
    if digest_buf:
        log.info("Friday — sending weekly apparel digest (%d item(s)) and clearing buffer.", len(digest_buf))
        try:
            send_weekly_digest(digest_buf)
        except Exception as e:
            log.error("Weekly digest failed (will retry next run): %s", e)
            return
        cache["weekly_digest"] = []
    else:
        log.info("Friday — no SB apparel/accessories found this week, no digest sent.")
    cache["last_digest_date"] = today
    save_cache(cache)

def backfill_digest(days: int):
    """One-off: email a digest of SB apparel/accessories that Shopify stores
    published in the last `days` days. Read-only — doesn't touch the cache or
    the weekly buffer. Triggered manually via the workflow's backfill_days input."""
    from datetime import timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cache = load_cache()
    log.info("Backfill: SB apparel/accessories published since %s (%d day(s))", cutoff.isoformat(timespec="minutes"), days)

    def scan(store):
        out = []
        if not is_shopify(store["url"], cache):
            return out
        for p in fetch_shopify(store):
            title = p.get("title", "")
            if not is_sb(title) or not is_apparel(title, p.get("product_type", "")):
                continue
            ts = p.get("published_at") or p.get("created_at")
            try:
                when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
            if when < cutoff:
                continue
            v = p.get("variants", [{}])
            out.append({
                "store": store["name"], "title": title,
                "price": v[0].get("price", "?") if v else "?",
                "sizes": format_sizes(p),
                "link":  f"{store['url'].rstrip('/')}/products/{p.get('handle','')}",
                "image": product_image(p), "local": store.get("local_sd", False),
                "_when": when.isoformat(),
            })
        return out

    finds = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for fut in concurrent.futures.as_completed([ex.submit(scan, s) for s in STORES]):
            try:
                finds.extend(fut.result())
            except Exception as e:
                log.error("Backfill store error: %s", e)
    finds.sort(key=lambda f: f["_when"], reverse=True)
    for f in finds:
        log.info("  BACKFILL [%s] %s — %s", f["store"], f["title"], f["_when"][:10])
    if not finds:
        log.info("Backfill: nothing found, no email sent.")
        return
    send_weekly_digest(finds)
    log.info("Backfill: emailed %d item(s).", len(finds))


if __name__ == "__main__":
    _bf = os.environ.get("BACKFILL_DAYS", "").strip()
    if _bf and _bf != "0":
        backfill_digest(int(_bf))
    else:
        run()
