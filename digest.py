#!/usr/bin/env python3
"""YouTube digest: picks the videos worth watching from your subscribed channels.

Fetches each channel's RSS feed, applies your per-channel rules (channel_rules.json),
asks an LLM (via OpenRouter) to choose the best ones, and writes a static page to docs/.
"""
import argparse
import csv
import html
import json
import os
import re
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

ROOT = Path(__file__).parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
STATE_PATH = ROOT / "state.json"
DOCS = ROOT / "docs"
UA = "sm-digest/1.0 (personal digest)"
MODEL = os.environ.get("MODEL", "google/gemini-2.5-flash-lite")
API = "https://openrouter.ai/api/v1/chat/completions"
TZ = ZoneInfo(CFG.get("timezone", "America/New_York"))

# Monthly roundup (python digest.py --monthly). Same scoring as the regular run; only these differ.
MONTHLY_WINDOW_DAYS = 30
MONTHLY_LIST_SIZE = 25
MONTHLY_MAX_PER_CHANNEL = 3
MONTHLY_MAX_MUST = 10          # must-watch slots (regular run uses config.json "max_must")
MONTHLY_MAX_CANDIDATES = 400   # cap on what is sent to the AI
MONTHLY_EXCLUDE_SEEN = False   # False = include videos already shown in regular digests
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}

# Per-channel rules, keyed by channel name or channel ID (case-insensitive).
_rules_path = ROOT / "channel_rules.json"
RULES = {
    k.strip().lower(): v
    for k, v in (json.loads(_rules_path.read_text(encoding="utf-8")) if _rules_path.exists() else {}).items()
    if not k.startswith("_")
}
USED_RULES = set()


def safe_err(e):
    """Error text that is safe to publish: never include API keys."""
    t = f"{type(e).__name__}: {e}"
    for name in ("OPENROUTER_API_KEY", "YOUTUBE_API_KEY"):
        val = (os.environ.get(name) or "").strip()
        if val:
            t = t.replace(val, "[hidden]")
    t = re.sub(r"(?i)bearer\s+\S+", "Bearer [hidden]", t)
    t = re.sub(r"(?i)([?&]key=)[^&\s'\"]+", r"\1[hidden]", t)
    t = re.sub(r"sk-[A-Za-z0-9_\-]+", "[hidden]", t)
    return t[:300]


# ---------------------------------------------------------------- state
def load_state():
    s = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    s.setdefault("last_run", None)
    s.setdefault("seen", {})
    return s


def save_state(state):
    cutoff = time.time() - 30 * 86400
    state["seen"] = {i: t for i, t in state["seen"].items() if t > cutoff}
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")


# -------------------------------------------------------------- youtube
def load_channels():
    chans = {}
    csv_path = ROOT / "subscriptions.csv"  # Google Takeout export
    if csv_path.exists():
        with open(csv_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
                cid = row.get("channel id", "")
                if cid:
                    chans[cid] = (row.get("channel title", "") or cid, row.get("priority", "").lower())
    txt_path = ROOT / "channels.txt"
    if txt_path.exists():
        for line in txt_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cid, _, label = line.partition("#")
            if cid.strip():
                chans[cid.strip()] = (label.strip() or cid.strip(), "")
    return [(cid, label, pri) for cid, (label, pri) in chans.items()]


def find_rule(v):
    """Rule from channel_rules.json (if any); the CSV's Priority column fills in priority if the rule has none."""
    rule = {}
    for key in (v["channel_id"], v["channel"], v["label"]):
        k = (key or "").strip().lower()
        if k in RULES:
            USED_RULES.add(k)
            rule = dict(RULES[k])
            break
    pri = (rule.get("priority") or v.get("csv_priority") or "").strip().lower()
    if pri in ("must", "high", "low"):
        rule["priority"] = pri
    return rule


def fetch_channel(ch):
    cid, label, csv_priority = ch
    r = requests.get(
        f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
        headers={"User-Agent": UA}, timeout=20,
    )
    r.raise_for_status()
    root = ET.fromstring(r.content)
    channel = root.findtext("a:author/a:name", namespaces=NS) or label
    vids = []
    for e in root.findall("a:entry", NS):
        stats = e.find("media:group/media:community/media:statistics", NS)
        desc = (e.findtext("media:group/media:description", namespaces=NS) or "").replace("\n", " ")
        vids.append({
            "id": e.findtext("yt:videoId", namespaces=NS),
            "title": e.findtext("a:title", namespaces=NS) or "",
            "channel": channel,
            "channel_id": cid,
            "label": label,
            "csv_priority": csv_priority,
            "published": datetime.fromisoformat(e.findtext("a:published", namespaces=NS)).timestamp(),
            "desc_full": desc[:3000],
            "desc": desc[:200],
            "views": int(stats.get("views", 0)) if stats is not None else None,
            "duration": None,
        })
    return vids


def fetch_channel_api(ch, key, since_ts):
    """Uploads playlist via the YouTube Data API: reaches back further than the RSS feed's ~15 videos."""
    cid, label, csv_priority = ch
    out, token = [], None
    for _ in range(4):  # up to 200 videos per channel
        params = {"part": "snippet,contentDetails", "playlistId": "UU" + cid[2:], "maxResults": 50, "key": key}
        if token:
            params["pageToken"] = token
        r = requests.get("https://www.googleapis.com/youtube/v3/playlistItems", params=params, timeout=20)
        r.raise_for_status()
        j = r.json()
        reached_end = False
        for it in j.get("items", []):
            sn, cd = it["snippet"], it["contentDetails"]
            if sn.get("title") in ("Private video", "Deleted video"):
                continue
            pub = cd.get("videoPublishedAt") or sn.get("publishedAt")
            ts = datetime.fromisoformat(pub.replace("Z", "+00:00")).timestamp()
            if ts < since_ts:
                reached_end = True
                continue
            desc = (sn.get("description") or "").replace("\n", " ")
            out.append({
                "id": cd["videoId"], "title": sn.get("title", ""),
                "channel": sn.get("videoOwnerChannelTitle") or label,
                "channel_id": cid, "label": label, "csv_priority": csv_priority,
                "published": ts, "desc_full": desc[:3000], "desc": desc[:200],
                "views": None, "duration": None,
            })
        token = j.get("nextPageToken")
        if reached_end or not token:
            break
    return out


def passes_text_rules(v):
    r = v["rule"]
    hay = v["title"] + (" " + v["desc_full"] if r.get("search_description") else "")
    if r.get("include_regex") and not re.search(r["include_regex"], hay, re.I):
        return False
    if r.get("exclude_regex") and re.search(r["exclude_regex"], hay, re.I):
        return False
    return True


def passes_duration_rules(v, unknown):
    lo, hi = v["rule"].get("min_minutes"), v["rule"].get("max_minutes")
    if lo is None and hi is None:
        return True
    d = v["duration"]
    if d is None:
        unknown.add(v["channel"])
        return False
    return (lo is None or d >= lo * 60) and (hi is None or d <= hi * 60)


def is_short(video_id):
    """youtube.com/shorts/<id> returns 200 for Shorts and redirects for normal videos."""
    try:
        r = requests.head(f"https://www.youtube.com/shorts/{video_id}",
                          allow_redirects=False, timeout=10, headers={"User-Agent": UA})
        return r.status_code == 200
    except Exception:
        return False


def enrich_with_api(videos, key):
    """Exact duration and view counts via the YouTube Data API."""
    for i in range(0, len(videos), 50):
        batch = videos[i:i + 50]
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "contentDetails,statistics", "id": ",".join(v["id"] for v in batch), "key": key},
            timeout=20,
        )
        r.raise_for_status()
        info = {it["id"]: it for it in r.json().get("items", [])}
        for v in batch:
            it = info.get(v["id"])
            if not it:
                continue
            m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", it["contentDetails"]["duration"])
            if m:
                h, mi, s = (int(x or 0) for x in m.groups())
                v["duration"] = h * 3600 + mi * 60 + s
            v["views"] = int(it.get("statistics", {}).get("viewCount", v["views"] or 0))


def collect_youtube(state, errors, monthly=False):
    cfg = CFG["youtube"]
    channels = load_channels()
    if not channels:
        errors.append("No channels found (add subscriptions.csv or channels.txt).")
        return []
    channels = [c for c in channels if c[2] != "none"]  # Priority "none" = never show
    seen = {} if (monthly and not MONTHLY_EXCLUDE_SEEN) else state["seen"]
    now = time.time()
    videos, failed = [], 0
    window = MONTHLY_WINDOW_DAYS * 86400 if monthly else cfg.get("max_age_hours", 96) * 3600
    api_key = (os.getenv("YOUTUBE_API_KEY") or "").strip()

    def safe(ch):
        if monthly and api_key:
            try:
                return fetch_channel_api(ch, api_key, time.time() - window)
            except Exception:
                pass  # fall back to the RSS feed
        try:
            return fetch_channel(ch)
        except Exception:
            return None

    with ThreadPoolExecutor(8) as ex:
        for res in ex.map(safe, channels):
            if res is None:
                failed += 1
            else:
                videos += res
    if failed:
        errors.append(f"{failed} of {len(channels)} channel feeds failed to load.")

    max_age = window
    videos = [v for v in videos if v["id"] not in seen and now - v["published"] <= max_age]
    for v in videos:
        v["rule"] = find_rule(v)
        v["url"] = f"https://www.youtube.com/watch?v={v['id']}"
    videos = [v for v in videos if passes_text_rules(v)]

    with ThreadPoolExecutor(8) as ex:
        flags = list(ex.map(lambda v: is_short(v["id"]), videos))
    videos = [v for v, s in zip(videos, flags) if not s]

    key = (os.getenv("YOUTUBE_API_KEY") or "").strip()
    if key and videos:
        try:
            enrich_with_api(videos, key)
        except Exception as e:
            errors.append(f"YouTube API enrichment failed: {safe_err(e)}")
    unknown = set()
    min_s = cfg.get("min_minutes", 0) * 60
    videos = [v for v in videos
              if (v["duration"] is None or v["duration"] >= max(min_s, 1)) and passes_duration_rules(v, unknown)]
    if unknown:
        errors.append("Duration rules couldn't be checked for: " + ", ".join(sorted(unknown))
                      + " (is YOUTUBE_API_KEY set?).")
    unmatched = [k for k in RULES if k not in USED_RULES]
    if unmatched:
        errors.append("Rules with no recent video to apply to (usually just no new uploads; if it keeps showing, "
                      "check the channel name spelling): " + ", ".join(unmatched))
    if monthly:  # priority channels first, then newest
        rank = {"must": 0, "high": 1, "low": 3}
        videos.sort(key=lambda v: (rank.get(v["rule"].get("priority"), 2), -v["published"]))
        return videos[:MONTHLY_MAX_CANDIDATES]
    videos.sort(key=lambda v: v["published"], reverse=True)
    return videos[: cfg.get("max_candidates", 150)]


# ------------------------------------------------------------ selection
def fmt_num(n):
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


def fmt_age(ts):
    h = (time.time() - ts) / 3600
    return f"{int(h)}h ago" if h < 48 else f"{int(h // 24)}d ago"


def fmt_dur(sec):
    if not sec:
        return ""
    h, rem = divmod(sec, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def describe(v):
    tag = {"must": "[MUST-WATCH CHANNEL] ", "high": "[HIGH PRIORITY CHANNEL] ", "low": "[LOW PRIORITY CHANNEL] "}.get(v["rule"].get("priority"), "")
    return tag + " | ".join(x for x in [
        v["channel"], v["title"], fmt_dur(v["duration"]),
        f"{fmt_num(v['views'])} views" if v["views"] is not None else "",
        fmt_age(v["published"]), v["desc"]] if x)


def pick(cands, n, taste, extra_rules, errors):
    """Ask the LLM for the best n candidates. Returns [(video, reason)]."""
    if not cands or n <= 0:
        return []
    listing = "\n".join(f"[{i}] {describe(c)}" for i, c in enumerate(cands))
    prompt = (
        f"You are curating a personal YouTube watch list. From the numbered candidates below, "
        f"choose the {n} best for this viewer.\n\nViewer's taste:\n{taste}\n\n"
        f"Rules: quality and relevance over raw popularity; keep variety. {extra_rules}\n"
        "Strongly favor videos tagged HIGH PRIORITY CHANNEL; pick LOW PRIORITY CHANNEL videos only if "
        "nothing better is available. Never mention these tags or priorities in your reasons. "
        "You only see titles and short descriptions, so describe what the video is about without guessing "
        "or hedging ('likely', 'probably').\n"
        'For each pick give a short reason (max 15 words) on why this viewer would care. '
        'Return ONLY a JSON array, best first, like [{"i": 3, "why": "..."}]. No markdown.\n\n'
        f"Candidates:\n{listing}"
    )
    try:
        r = requests.post(
            API,
            headers={"Authorization": f"Bearer {os.environ['OPENROUTER_API_KEY'].strip()}"},
            json={"model": MODEL, "max_tokens": 3000, "messages": [{"role": "user", "content": prompt}]},
            timeout=120,
        )
        r.raise_for_status()
        text = r.json()["choices"][0]["message"]["content"]
        data = json.loads(re.search(r"\[.*\]", text, re.S).group(0))
        out, used = [], set()
        for d in data:
            i = int(d["i"])
            if 0 <= i < len(cands) and i not in used:
                used.add(i)
                out.append((cands[i], str(d.get("why", "")).strip()))
        if out:
            return out[:n]
    except Exception as e:
        errors.append(f"AI selection failed ({safe_err(e)}); showing the newest videos instead.")
    return [(c, "") for c in cands[:n]]


# --------------------------------------------------------------- render
CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--mut:#6b6b6b;--link:#0b57d0;--line:#e5e5e5}
@media(prefers-color-scheme:dark){:root{--bg:#161616;--fg:#e8e8e8;--mut:#9a9a9a;--link:#8ab4f8;--line:#2c2c2c}}
body{font:16px/1.5 -apple-system,system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg);max-width:760px;margin:0 auto;padding:24px 16px 64px}
h1{margin:0 0 4px}a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.sub{color:var(--mut);font-size:14px}ul{list-style:none;padding:0;margin-top:24px}
li{padding:10px 0;border-bottom:1px solid var(--line)}.meta{color:var(--mut);font-size:13px}
.badge{font-size:12px;border:1px solid var(--line);border-radius:4px;padding:0 6px;margin-left:8px;color:var(--mut)}
.why{font-size:14px;margin-top:2px}.err{color:var(--mut);font-size:13px;margin-top:32px}
"""


def render_page(date_label, items, errors, archive_href, home_href=None, title="YouTube digest"):
    rows = "".join(
        f'<li><a href="{html.escape(v["url"])}">{html.escape(("★ " if v["rule"].get("always_include") or v["rule"].get("priority") in ("must", "high") else "") + v["title"])}</a>'
        f'<div class="meta">{html.escape(" · ".join(x for x in [v["channel"], fmt_dur(v["duration"]), (fmt_num(v["views"]) + " views") if v["views"] is not None else "", fmt_age(v["published"])] if x))}</div>'
        + (f'<div class="why">{html.escape(why)}</div>' if why else "") + "</li>"
        for v, why in items
    ) or '<li class="meta">Nothing new this time.</li>'
    err = ('<div class="err">Notes:<br>' + "<br>".join(html.escape(e) for e in errors) + "</div>") if errors else ""
    nav = f'<a href="{archive_href}">Past digests</a>'
    if home_href:
        nav = f'<a href="{home_href}">Latest</a> · ' + nav
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{html.escape(title)} {html.escape(date_label)}</title><style>{CSS}</style></head><body>"
        f'<h1>{html.escape(title)}</h1><div class="sub">{html.escape(date_label)} · {len(items)} videos · ★ = priority channel · {nav}</div>'
        f"<ul>{rows}</ul>{err}</body></html>"
    )


def write_site(now, items, errors, monthly=False, n_candidates=0):
    arch = DOCS / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    (DOCS / ".nojekyll").touch()  # plain HTML site: tell GitHub Pages not to run Jekyll
    local = now.astimezone(TZ)
    label = local.strftime("%A, %B %d, %Y").replace(" 0", " ")
    stamp = local.strftime("%Y-%m-%d")
    if monthly:
        title = f"Monthly digest: last {MONTHLY_WINDOW_DAYS} days"
        (arch / f"monthly-{stamp}.html").write_text(
            render_page(label, items, errors, "index.html", "../index.html", title), encoding="utf-8")
        meta = {
            "mode": "monthly", "date": stamp, "generated": now.isoformat(),
            "window_days": MONTHLY_WINDOW_DAYS, "list_size": MONTHLY_LIST_SIZE,
            "candidates": n_candidates, "count": len(items),
            "videos": [{"id": v["id"], "title": v["title"], "channel": v["channel"], "url": v["url"]}
                       for v, _ in items],
        }
        (arch / f"monthly-{stamp}.json").write_text(
            json.dumps(meta, indent=1, ensure_ascii=False), encoding="utf-8")
    else:  # the regular run owns the main page
        (DOCS / "index.html").write_text(render_page(label, items, errors, "archive/index.html"), encoding="utf-8")
        (arch / f"{stamp}.html").write_text(
            render_page(label, items, errors, "index.html", "../index.html"), encoding="utf-8")

    entries = []
    for p in arch.glob("*.html"):
        m = re.match(r"(monthly-)?(\d{4}-\d{2}-\d{2})$", p.stem)
        if m:
            entries.append((m.group(2), bool(m.group(1)), p.stem))
    entries.sort(reverse=True)
    links = "".join(
        f'<li><a href="{stem}.html">{date}</a>' + ('<span class="badge">Monthly</span>' if is_m else "") + "</li>"
        for date, is_m, stem in entries)
    (arch / "index.html").write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta name="robots" content="noindex"><title>Past digests</title><style>{CSS}</style></head><body>'
        f'<h1>Past digests</h1><div class="sub"><a href="../index.html">Latest</a></div><ul>{links}</ul></body></html>',
        encoding="utf-8")


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--monthly", action="store_true",
                    help=f"{MONTHLY_WINDOW_DAYS}-day roundup (top {MONTHLY_LIST_SIZE}); leaves the main page and seen-state alone")
    monthly = ap.parse_args().monthly

    now = datetime.now(timezone.utc)
    state = load_state()
    if not monthly and os.getenv("FORCE") != "1" and state["last_run"]:
        last = datetime.fromisoformat(state["last_run"])
        if now - last < timedelta(hours=CFG.get("min_hours_between_runs", 46)):
            print(f"Last run {last.isoformat()} is recent; skipping.")
            return

    errors = []
    cands = collect_youtube(state, errors, monthly)
    print(f"{len(cands)} candidates ({'monthly' if monthly else 'regular'})")
    y = CFG["youtube"]
    n = MONTHLY_LIST_SIZE if monthly else y["count"]
    max_must = MONTHLY_MAX_MUST if monthly else y.get("max_must", 6)
    per_channel = MONTHLY_MAX_PER_CHANNEL if monthly else 2

    must_all = sorted((v for v in cands if v["rule"].get("always_include") or v["rule"].get("priority") == "must"),
                      key=lambda v: v["published"], reverse=True)
    must = must_all[: min(max_must, n)]
    for v in must_all[len(must):]:  # overflow competes with the rest as high priority
        v["rule"] = dict(v["rule"], priority="high", always_include=False)
    must_ids = {v["id"] for v in must}
    rest = [v for v in cands if v["id"] not in must_ids]

    notes = {}
    for v in rest:
        if v["rule"].get("note"):
            notes[v["channel"]] = v["rule"]["note"]
    extra = f"Avoid more than {per_channel} from one channel unless exceptional."
    if monthly:
        extra += (f" These videos span the last {MONTHLY_WINDOW_DAYS} days: pick the best across the "
                  "whole period, not just the newest.")
    if notes:
        extra += " Per-channel instructions: " + " ".join(f'For "{c}": {t}' for c, t in notes.items())

    items = [(v, "Must-watch channel") for v in must] + pick(rest, n - len(must), y["taste"], extra, errors)

    if not monthly:  # the monthly roundup never changes what the regular run considers "seen"
        t = time.time()
        state["seen"].update({v["id"]: t for v, _ in items})
        state["last_run"] = now.isoformat()
        save_state(state)
    write_site(now, items, errors, monthly, len(cands))
    print(f"Wrote {'monthly ' if monthly else ''}digest with {len(items)} videos.")


if __name__ == "__main__":
    main()
