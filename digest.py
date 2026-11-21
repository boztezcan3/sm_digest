#!/usr/bin/env python3
"""Social digest: top Reddit posts + YouTube videos from your subscriptions.

Fetches candidates, asks Claude to pick the best ones for your taste,
and writes a static HTML page to docs/ (served by GitHub Pages).
"""
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
UA = "script:sm-digest:v1.0 (personal digest)"
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
TZ = ZoneInfo(CFG.get("timezone", "America/New_York"))
NS = {
    "a": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


# ---------------------------------------------------------------- state
def load_state():
    if STATE_PATH.exists():
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    else:
        s = {}
    s.setdefault("last_run", None)
    s.setdefault("seen", {})
    s["seen"].setdefault("reddit", {})
    s["seen"].setdefault("youtube", {})
    return s


def save_state(state):
    cutoff = time.time() - 30 * 86400
    for k in state["seen"]:
        state["seen"][k] = {i: t for i, t in state["seen"][k].items() if t > cutoff}
    STATE_PATH.write_text(json.dumps(state, indent=1), encoding="utf-8")


# --------------------------------------------------------------- reddit
def reddit_token():
    cid, sec = os.getenv("REDDIT_CLIENT_ID"), os.getenv("REDDIT_CLIENT_SECRET")
    if not (cid and sec):
        return None
    r = requests.post(
        "https://www.reddit.com/api/v1/access_token",
        auth=(cid, sec),
        data={"grant_type": "client_credentials"},
        headers={"User-Agent": UA},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def reddit_get(sub, token):
    sort = sub.get("sort", "top")
    params = {"limit": sub.get("fetch_limit", 50), "t": sub.get("time", "week"), "raw_json": 1}
    if token:
        url = f"https://oauth.reddit.com/r/{sub['name']}/{sort}"
        headers = {"User-Agent": UA, "Authorization": f"bearer {token}"}
    else:
        url = f"https://www.reddit.com/r/{sub['name']}/{sort}.json"
        headers = {"User-Agent": UA}
    r = requests.get(url, params=params, headers=headers, timeout=20)
    r.raise_for_status()
    return [c["data"] for c in r.json()["data"]["children"]]


def collect_reddit(state, errors):
    cfg = CFG["reddit"]
    try:
        token = reddit_token()
    except Exception as e:
        token = None
        errors.append(f"Reddit auth failed ({e}); trying without login.")
    seen = state["seen"]["reddit"]
    now = time.time()
    out = []
    for sub in cfg["subreddits"]:
        try:
            posts = reddit_get(sub, token)
        except Exception as e:
            errors.append(f"r/{sub['name']}: {e}")
            continue
        rx = re.compile(sub["title_regex"], re.I) if sub.get("title_regex") else None
        max_age = sub.get("max_age_hours", cfg.get("max_age_hours", 72)) * 3600
        keep = []
        for p in posts:
            if p["id"] in seen or now - p["created_utc"] > max_age:
                continue
            if p.get("over_18") and not cfg.get("allow_nsfw", False):
                continue
            if rx:
                if not rx.search(p["title"]):
                    continue
            elif p.get("stickied"):
                continue
            keep.append({
                "id": p["id"],
                "sub": sub["name"],
                "title": p["title"],
                "url": "https://www.reddit.com" + p["permalink"],
                "score": p.get("score", 0),
                "comments": p.get("num_comments", 0),
                "created": p["created_utc"],
                "pinned": bool(rx and sub.get("always_include")),
            })
        keep.sort(key=lambda x: x["score"], reverse=True)
        out += keep[: sub.get("candidates", cfg.get("candidates_per_sub", 12))]
        time.sleep(1)
    return out


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
                    chans[cid] = row.get("channel title", "") or cid
    txt_path = ROOT / "channels.txt"
    if txt_path.exists():
        for line in txt_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            cid, _, label = line.partition("#")
            cid = cid.strip()
            if cid:
                chans[cid] = label.strip() or cid
    return list(chans.items())


def fetch_channel(ch):
    cid, label = ch
    r = requests.get(
        f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}",
        headers={"User-Agent": UA},
        timeout=20,
    )
    r.raise_for_status()
    root = ET.fromstring(r.content)
    channel = root.findtext("a:author/a:name", namespaces=NS) or label
    vids = []
    for e in root.findall("a:entry", NS):
        stats = e.find("media:group/media:community/media:statistics", NS)
        vids.append({
            "id": e.findtext("yt:videoId", namespaces=NS),
            "title": e.findtext("a:title", namespaces=NS) or "",
            "channel": channel,
            "published": datetime.fromisoformat(e.findtext("a:published", namespaces=NS)).timestamp(),
            "desc": (e.findtext("media:group/media:description", namespaces=NS) or "")[:200].replace("\n", " "),
            "views": int(stats.get("views", 0)) if stats is not None else None,
            "duration": None,
        })
    return vids


def is_short(video_id):
    """youtube.com/shorts/<id> returns 200 for Shorts and redirects for normal videos."""
    try:
        r = requests.head(
            f"https://www.youtube.com/shorts/{video_id}",
            allow_redirects=False, timeout=10, headers={"User-Agent": UA},
        )
        return r.status_code == 200
    except Exception:
        return False


def enrich_with_api(videos, key):
    """Optional: exact duration and view counts via the YouTube Data API."""
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


def collect_youtube(state, errors):
    cfg = CFG["youtube"]
    channels = load_channels()
    if not channels:
        errors.append("YouTube: no channels found (add subscriptions.csv or channels.txt).")
        return []
    seen = state["seen"]["youtube"]
    now = time.time()
    videos, failed = [], 0

    def safe(ch):
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
        errors.append(f"YouTube: {failed} of {len(channels)} channel feeds failed to load.")

    max_age = cfg.get("max_age_hours", 96) * 3600
    videos = [v for v in videos if v["id"] not in seen and now - v["published"] <= max_age]
    with ThreadPoolExecutor(8) as ex:
        flags = list(ex.map(lambda v: is_short(v["id"]), videos))
    videos = [v for v, s in zip(videos, flags) if not s]

    key = os.getenv("YOUTUBE_API_KEY")
    if key and videos:
        try:
            enrich_with_api(videos, key)
        except Exception as e:
            errors.append(f"YouTube API enrichment failed: {e}")
    min_s = cfg.get("min_minutes", 0) * 60
    videos = [v for v in videos if v["duration"] is None or v["duration"] >= max(min_s, 1)]
    videos.sort(key=lambda v: v["published"], reverse=True)
    for v in videos:
        v["url"] = f"https://www.youtube.com/watch?v={v['id']}"
    return videos[: cfg.get("max_candidates", 150)]


# ------------------------------------------------------------ selection
def fmt_num(n):
    if n is None:
        return "?"
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)


def fmt_age(ts):
    h = (time.time() - ts) / 3600
    return f"{int(h)}h ago" if h < 48 else f"{int(h // 24)}d ago"


def fmt_dur(sec):
    if not sec:
        return ""
    h, rem = divmod(sec, 3600)
    return f"{h}:{rem // 60:02d}:{rem % 60:02d}" if h else f"{rem // 60}:{rem % 60:02d}"


def pick(kind, cands, n, taste, describe, extra_rules=""):
    """Ask Claude for the best n candidates. Returns [(candidate, reason)]."""
    if not cands or n <= 0:
        return []
    listing = "\n".join(f"[{i}] {describe(c)}" for i, c in enumerate(cands))
    prompt = (
        f"You are curating a personal {kind} digest. From the numbered candidates below, "
        f"choose the {n} best for this reader.\n\nReader's taste:\n{taste}\n\n"
        f"Rules: quality and relevance over raw popularity; keep variety. {extra_rules}\n"
        'For each pick give a short reason (max 15 words) on why this reader would care. '
        'Return ONLY a JSON array, best first, like [{"i": 3, "why": "..."}]. No markdown.\n\n'
        f"Candidates:\n{listing}"
    )
    try:
        import anthropic

        msg = anthropic.Anthropic().messages.create(
            model=MODEL, max_tokens=2000, messages=[{"role": "user", "content": prompt}]
        )
        text = "".join(b.text for b in msg.content if b.type == "text")
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
        print(f"LLM selection failed for {kind}: {e}")
    return [(c, "") for c in cands[:n]]  # fallback: order as given


# --------------------------------------------------------------- render
CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--mut:#6b6b6b;--link:#0b57d0;--line:#e5e5e5}
@media(prefers-color-scheme:dark){:root{--bg:#161616;--fg:#e8e8e8;--mut:#9a9a9a;--link:#8ab4f8;--line:#2c2c2c}}
body{font:16px/1.5 -apple-system,system-ui,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg);max-width:760px;margin:0 auto;padding:24px 16px 64px}
h1{margin:0 0 4px}h2{margin:32px 0 8px;border-bottom:1px solid var(--line);padding-bottom:4px}
a{color:var(--link);text-decoration:none}a:hover{text-decoration:underline}
.sub{color:var(--mut);font-size:14px}ul{list-style:none;padding:0}
li{padding:10px 0;border-bottom:1px solid var(--line)}.meta{color:var(--mut);font-size:13px}
.why{font-size:14px;margin-top:2px}.err{color:var(--mut);font-size:13px;margin-top:32px}
"""


def render_item(title, url, meta, why):
    out = f'<li><a href="{html.escape(url)}">{html.escape(title)}</a>'
    out += f'<div class="meta">{html.escape(meta)}</div>'
    if why:
        out += f'<div class="why">{html.escape(why)}</div>'
    return out + "</li>"


def render_page(date_label, reddit, youtube, errors, archive_href, home_href=None):
    r_items = "".join(
        render_item(
            ("📌 " if c["pinned"] else "") + c["title"], c["url"],
            f"r/{c['sub']} · {fmt_num(c['score'])} pts · {c['comments']} comments · {fmt_age(c['created'])}", why)
        for c, why in reddit
    ) or '<li class="meta">Nothing new this time.</li>'
    y_items = "".join(
        render_item(
            c["title"], c["url"],
            " · ".join(x for x in [c["channel"], fmt_dur(c["duration"]),
                                   f"{fmt_num(c['views'])} views" if c["views"] is not None else "",
                                   fmt_age(c["published"])] if x), why)
        for c, why in youtube
    ) or '<li class="meta">Nothing new this time.</li>'
    err = ""
    if errors:
        err = '<div class="err">Notes:<br>' + "<br>".join(html.escape(e) for e in errors) + "</div>"
    nav = f'<a href="{archive_href}">Past digests</a>'
    if home_href:
        nav = f'<a href="{home_href}">Latest</a> · ' + nav
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>Social digest {html.escape(date_label)}</title><style>{CSS}</style></head><body>"
        f'<h1>Social digest</h1><div class="sub">{html.escape(date_label)} · {nav}</div>'
        f"<h2>Reddit ({len(reddit)})</h2><ul>{r_items}</ul>"
        f"<h2>YouTube ({len(youtube)})</h2><ul>{y_items}</ul>{err}</body></html>"
    )


def write_site(now, reddit, youtube, errors):
    arch = DOCS / "archive"
    arch.mkdir(parents=True, exist_ok=True)
    local = now.astimezone(TZ)
    label = local.strftime("%A, %B %d, %Y").replace(" 0", " ")
    stamp = local.strftime("%Y-%m-%d")
    (DOCS / "index.html").write_text(
        render_page(label, reddit, youtube, errors, "archive/index.html"), encoding="utf-8")
    (arch / f"{stamp}.html").write_text(
        render_page(label, reddit, youtube, errors, "index.html", "../index.html"), encoding="utf-8")
    pages = sorted((p.stem for p in arch.glob("2*.html")), reverse=True)
    links = "".join(f'<li><a href="{p}.html">{p}</a></li>' for p in pages)
    (arch / "index.html").write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<meta name="robots" content="noindex"><title>Past digests</title><style>{CSS}</style></head><body>'
        f'<h1>Past digests</h1><div class="sub"><a href="../index.html">Latest</a></div><ul>{links}</ul></body></html>',
        encoding="utf-8")


# ----------------------------------------------------------------- main
def main():
    now = datetime.now(timezone.utc)
    state = load_state()
    if os.getenv("FORCE") != "1" and state["last_run"]:
        last = datetime.fromisoformat(state["last_run"])
        if now - last < timedelta(hours=CFG.get("min_hours_between_runs", 46)):
            print(f"Last run {last.isoformat()} is recent; skipping.")
            return

    errors = []
    r_cands = collect_reddit(state, errors)
    y_cands = collect_youtube(state, errors)
    print(f"Candidates: {len(r_cands)} reddit, {len(y_cands)} youtube")

    r_cfg, y_cfg = CFG["reddit"], CFG["youtube"]
    pinned = [(c, "Weekly chapter discussion") for c in r_cands if c["pinned"]]
    rest = [c for c in r_cands if not c["pinned"]]
    reddit = pinned + pick(
        "Reddit", rest, r_cfg["count"] - len(pinned), r_cfg["taste"],
        lambda c: f"r/{c['sub']} | {c['score']} pts, {c['comments']} comments, {fmt_age(c['created'])} | {c['title']}",
        "Avoid more than 3 from one subreddit unless exceptional. Scores are not comparable across subreddits.",
    )
    youtube = pick(
        "YouTube", y_cands, y_cfg["count"], y_cfg["taste"],
        lambda c: " | ".join(x for x in [c["channel"], c["title"], fmt_dur(c["duration"]),
                                         f"{fmt_num(c['views'])} views" if c["views"] is not None else "",
                                         fmt_age(c["published"]), c["desc"]] if x),
        "Avoid more than 2 from one channel unless exceptional.",
    )

    t = time.time()
    state["seen"]["reddit"].update({c["id"]: t for c, _ in reddit})
    state["seen"]["youtube"].update({c["id"]: t for c, _ in youtube})
    state["last_run"] = now.isoformat()
    save_state(state)
    write_site(now, reddit, youtube, errors)
    print(f"Wrote digest with {len(reddit)} reddit, {len(youtube)} youtube items.")


if __name__ == "__main__":
    main()
