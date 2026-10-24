#!/usr/bin/env python3
"""Reddit lead finder: watches subreddit RSS feeds and posts matches to Discord.

Alert-only. It never posts, votes or replies on Reddit; you write every reply.
Standard library only, Python 3.11+. Run it on your own machine: Reddit often
blocks cloud IPs.

    python3 bot.py              # poll forever
    python3 bot.py --once       # one pass (for cron / Task Scheduler)
    python3 bot.py --dry-run    # print alerts instead of sending them
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
ATOM = "{http://www.w3.org/2005/Atom}"
MAX_SEEN = 20000
TZ = timezone.utc  # set from settings.timezone in main()


# ---------- config ----------

@dataclass
class Product:
    name: str
    webhook_env: str
    keyword_subreddits: list[str]
    every_post_subreddits: list[str]
    keywords: dict[str, list[str]]  # group -> phrases
    exclude: list[str]
    search_all: bool = False
    search_minutes: int = 30
    search_skip_subreddits: list[str] = field(default_factory=list)
    search_skip_keywords: list[str] = field(default_factory=list)
    every_post_exclude: list[str] = field(default_factory=list)
    reply_notes: list[str] = field(default_factory=list)
    every_post_digest_at: str = ""
    every_post_reply_notes: list[str] = field(default_factory=list)
    _kw_patterns: list[tuple[str, str, re.Pattern]] = field(default_factory=list, repr=False)
    _ex_patterns: list[re.Pattern] = field(default_factory=list, repr=False)

    def __post_init__(self):
        self._kw_patterns = [
            (group, kw, phrase_pattern(kw))
            for group, kws in self.keywords.items() for kw in kws
        ]
        self._ex_patterns = [phrase_pattern(w) for w in self.exclude]
        self._every_post_ex = [phrase_pattern(w) for w in self.every_post_exclude]
        self._search_skip = {normalize(k).lower() for k in self.search_skip_keywords}

    def watches(self, sub: str, kind: str) -> str | None:
        """Return the mode this product uses for a feed, or None."""
        sub = sub.lower()
        if sub in (s.lower() for s in self.every_post_subreddits) and kind == "new":
            return "every_post"
        if sub in (s.lower() for s in self.keyword_subreddits):
            return "keyword"
        return None


def phrase_pattern(phrase: str) -> re.Pattern:
    words = [re.escape(w) for w in normalize(phrase).split()]
    return re.compile(r"(?<!\w)" + r"\s+".join(words) + r"(?!\w)", re.IGNORECASE)


def normalize(text: str) -> str:
    text = text.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip()


def load_config(path: Path) -> tuple[dict, list[Product]]:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    settings = {
        "user_agent": "lead-finder/0.1",
        "poll_minutes": 5,
        "seconds_between_requests": 6,
        "max_age_hours": 24,
        "ignore_authors": [],
        "timezone": "UTC",
        "quiet_hours": [],
        **raw.get("settings", {}),
    }
    products = [
        Product(
            name=p["name"],
            webhook_env=p["webhook_env"],
            keyword_subreddits=p.get("keyword_subreddits", []),
            every_post_subreddits=p.get("every_post_subreddits", []),
            keywords=p.get("keywords", {}),
            exclude=p.get("exclude", {}).get("words", []),
            search_all=p.get("search_all", False),
            search_minutes=p.get("search_minutes", 30),
            search_skip_subreddits=p.get("search_skip_subreddits", []),
            search_skip_keywords=p.get("search_skip_keywords", []),
            every_post_exclude=p.get("exclude", {}).get("every_post", []),
            reply_notes=p.get("reply_notes", []),
            every_post_digest_at=p.get("every_post_digest_at", ""),
            every_post_reply_notes=p.get("every_post_reply_notes", []),
        )
        for p in raw.get("product", [])
    ]
    return settings, products


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


# ---------- feeds ----------

@dataclass
class Item:
    id: str
    kind: str          # "post" or "comment"
    subreddit: str
    title: str         # thread title
    body: str          # plain text
    link: str
    author: str
    published: datetime | None


def feed_url(sub: str, kind: str) -> str:
    path = "new" if kind == "new" else "comments"
    return f"https://www.reddit.com/r/{sub}/{path}/.rss?limit=100"


def search_url(query: str) -> str:
    q = urllib.parse.quote(query)
    return f"https://www.reddit.com/search.rss?q={q}&sort=new&t=day&limit=100"


def search_queries(product: Product, max_len: int = 250) -> list[str]:
    """Keyword phrases OR-ed together, a few per request, across all of Reddit."""
    queries, cur = [], ""
    for kws in product.keywords.values():
        for kw in kws:
            if normalize(kw).lower() in product._search_skip:
                continue
            term = f'"{kw}"'
            if cur and len(cur) + len(term) + 4 > max_len:
                queries.append(cur)
                cur = ""
            cur = f"{cur} OR {term}" if cur else term
    if cur:
        queries.append(cur)
    return queries


def strip_html(fragment: str) -> str:
    text = html.unescape(fragment)
    text = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    text = re.sub(r"<(br|/p|/li|/div)\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text)
    # Reddit appends "submitted by /u/x [link] [comments]" to posts.
    text = re.split(r"\s*submitted by\s+/u/", text)[0]
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", text)).strip()


def parse_feed(xml_text: str, sub: str, kind: str) -> list[Item]:
    root = ET.fromstring(xml_text)
    items = []
    for e in root.findall(f"{ATOM}entry"):
        eid = (e.findtext(f"{ATOM}id") or "").strip()
        title = (e.findtext(f"{ATOM}title") or "").strip()
        link_el = e.find(f"{ATOM}link")
        link = link_el.get("href", "") if link_el is not None else ""
        author = (e.findtext(f"{ATOM}author/{ATOM}name") or "").strip()
        author = author.removeprefix("/u/")
        content = e.findtext(f"{ATOM}content") or ""
        cat = e.find(f"{ATOM}category")
        item_sub = cat.get("term", sub) if cat is not None else sub
        stamp = e.findtext(f"{ATOM}published") or e.findtext(f"{ATOM}updated")
        published = None
        if stamp:
            try:
                published = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            except ValueError:
                pass
        is_comment = kind == "comments" or eid.startswith("t1_")
        if is_comment:
            # Comment titles look like "/u/name on Thread title".
            title = re.sub(r"^/u/\S+\s+on\s+", "", title)
        items.append(Item(
            id=eid, kind="comment" if is_comment else "post", subreddit=item_sub,
            title=title, body=strip_html(content), link=link, author=author,
            published=published,
        ))
    return items


class RateLimited(Exception):
    """Reddit refused twice in a row; stop this pass and try next time."""


def http_get(url: str, user_agent: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": user_agent})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            if attempt == 1:
                raise RateLimited("still rate limited after waiting") from e
            wait = min(int(e.headers.get("Retry-After") or 60), 120)
            log(f"rate limited by Reddit, waiting {wait}s")
            time.sleep(wait)
    raise RuntimeError("unreachable")


# ---------- matching ----------

@dataclass
class Match:
    product: Product
    item: Item
    mode: str
    keyword: str | None
    group: str | None


def match_item(product: Product, item: Item, mode: str) -> Match | None:
    # For comments, only the comment itself counts: the thread title is shared
    # by every comment and would make one keyword in a title alert many times.
    text = normalize(item.body if item.kind == "comment" else f"{item.title}\n{item.body}")
    if any(p.search(text) for p in product._ex_patterns):
        return None
    if mode == "every_post":
        if any(p.search(text) for p in product._every_post_ex):
            return None
        return Match(product, item, mode, None, None)
    for group, kw, pat in product._kw_patterns:
        if mode == "search" and normalize(kw).lower() in product._search_skip:
            continue
        if pat.search(text):
            return Match(product, item, mode, kw, group)
    return None


# ---------- Discord ----------

def snippet(text: str, limit: int = 300) -> str:
    lines = [l for l in text.splitlines() if l.strip()][:3]
    s = "\n".join(lines)
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def reply_notes(lines: list[str]) -> str:
    notes = "\n".join(f"• {n}" for n in lines)
    return notes if len(notes) <= 1024 else notes[:1023] + "…"  # Discord field limit


def discord_payload(m: Match) -> dict:
    it = m.item
    why = f"`{m.keyword}` ({m.group.replace('_', ' ')})" if m.keyword else "new post (every-post sub)"
    if m.mode == "search":
        why += ", Reddit-wide search"
    return {
        "username": "Lead finder",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": (("💬 " if it.kind == "comment" else "📝 ") + it.title)[:256],
            "url": it.link,
            "description": snippet(it.body) or "_(no text)_",
            "color": 0x5865F2 if it.kind == "post" else 0x57F287,
            "fields": [
                {"name": "Subreddit", "value": f"r/{it.subreddit}", "inline": True},
                {"name": "Match", "value": why, "inline": True},
                {"name": "Type", "value": it.kind, "inline": True},
                *([{"name": "Reply notes", "value": reply_notes(m.product.reply_notes)}]
                  if m.product.reply_notes else []),
            ],
            "footer": {"text": f"u/{it.author}" if it.author else ""},
            **({"timestamp": it.published.isoformat()} if it.published else {}),
        }],
    }


def send_discord(webhook: str, payload: dict) -> None:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        webhook, data=data, method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "lead-finder/0.1"},
    )
    for attempt in range(3):
        try:
            urllib.request.urlopen(req, timeout=30).close()
            time.sleep(1)  # stay well under Discord's per-webhook limit
            return
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 2:
                try:
                    wait = float(json.loads(e.read()).get("retry_after", 2))
                except Exception:
                    wait = 2.0
                time.sleep(wait + 0.5)
                continue
            raise


# ---------- state ----------

class Seen:
    def __init__(self, path: Path):
        self.path = path
        self.ids: dict[str, float] = {}
        self.fresh = not path.exists()
        if not self.fresh:
            self.ids = json.loads(path.read_text(encoding="utf-8"))

    def __contains__(self, key: str) -> bool:
        return key in self.ids

    def add(self, key: str) -> None:
        self.ids[key] = time.time()

    def save(self) -> None:
        if len(self.ids) > MAX_SEEN:
            keep = sorted(self.ids.items(), key=lambda kv: kv[1])[-MAX_SEEN:]
            self.ids = dict(keep)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.ids), encoding="utf-8")
        tmp.replace(self.path)


# ---------- main loop ----------

def log(msg: str) -> None:
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S %Z}] {msg}", flush=True)


def is_quiet(now: datetime, quiet_hours: list[str]) -> bool:
    """True inside quiet_hours ["23:00", "08:00"], which may wrap past midnight."""
    if not quiet_hours:
        return False
    start, end = (datetime.strptime(t, "%H:%M").time() for t in quiet_hours)
    t = now.time()
    return start <= t < end if start < end else t >= start or t < end


class Outbox:
    """Alerts held during quiet hours, sent together when they end."""

    def __init__(self, path: Path):
        self.path = path
        self.held: list[dict] = []
        if path.exists():
            self.held = json.loads(path.read_text(encoding="utf-8"))

    def hold(self, webhook_env: str, payload: dict) -> None:
        self.held.append({"webhook_env": webhook_env, "payload": payload})
        self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps(self.held), encoding="utf-8")

    def flush(self, quiet_hours: list[str]) -> int:
        """Send everything held, each channel opening with a one-line summary."""
        sent = 0
        for env in dict.fromkeys(h["webhook_env"] for h in self.held):
            webhook = os.environ.get(env)
            if not webhook:
                log(f"{env} is not set; keeping held alerts")
                continue
            batch = [h for h in self.held if h["webhook_env"] == env]
            span = "–".join(quiet_hours)
            try:
                send_discord(webhook, {
                    "username": "Lead finder",
                    "content": f"☀️ {len(batch)} lead(s) from quiet hours ({span} {datetime.now(TZ):%Z}):",
                })
                for h in batch:
                    send_discord(webhook, h["payload"])
                    self.held.remove(h)
                    sent += 1
            except Exception as e:
                log(f"sending held alerts failed, will retry: {e}")
            finally:
                self.save()
        return sent


@dataclass
class Feed:
    url: str
    label: str
    kind: str  # "new", "comments" or "search"
    mode_for: Callable[[Product, Item], str | None]
    sub: str = ""


def feeds_for(products: list[Product], per_feed: int = 15) -> list[Feed]:
    """Every subreddit any product needs, combined into multi-subreddit feeds
    (r/a+b+c/new), so a pass is a few requests instead of one per subreddit.
    Reddit rate-limits unauthenticated requests hard, especially from cloud IPs."""
    subs: dict[str, dict[str, str]] = {"new": {}, "comments": {}}
    for p in products:
        for s in p.keyword_subreddits:
            subs["new"].setdefault(s.lower(), s)
            subs["comments"].setdefault(s.lower(), s)
        for s in p.every_post_subreddits:
            subs["new"].setdefault(s.lower(), s)
    feeds = []
    for kind, names in subs.items():
        names = list(names.values())
        for i in range(0, len(names), per_feed):
            chunk = names[i:i + per_feed]
            sub = "+".join(chunk)
            label = f"{kind} in {len(chunk)} subreddits"
            feeds.append(Feed(feed_url(sub, kind), label, kind,
                              lambda p, it, k=kind: p.watches(it.subreddit, k)))
    return feeds


def search_feeds(product: Product) -> list[Feed]:
    """Reddit-wide keyword search for one product, covering subreddits it
    doesn't already watch. Results are re-checked locally, since Reddit's
    search is looser than whole-phrase matching."""
    skip = {s.lower() for s in product.search_skip_subreddits}

    def mode_for(p: Product, it: Item) -> str | None:
        if p is not product or it.subreddit.lower() in skip or p.watches(it.subreddit, "new"):
            return None
        return "search"

    return [
        Feed(search_url(q), f"{product.name} search {i + 1}", "search", mode_for)
        for i, q in enumerate(search_queries(product))
    ]


def search_due(product: Product, seen: Seen) -> bool:
    if not product.search_all:
        return False
    last = seen.ids.get(f"_last_search:{product.name}", 0)
    return time.time() - last >= product.search_minutes * 60


class Digest:
    """Every-post matches collected through the day and sent as one list at
    every_post_digest_at, instead of one alert each."""

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {"last_sent": {}, "items": {}}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def add(self, product: Product, it: Item) -> None:
        items = self.data["items"].setdefault(product.name, [])
        if any(i["link"] == it.link for i in items):
            return
        items.append({"title": it.title, "link": it.link, "subreddit": it.subreddit})
        self.save()

    def save(self) -> None:
        self.path.write_text(json.dumps(self.data), encoding="utf-8")

    def due(self, product: Product, now: datetime) -> bool:
        if not product.every_post_digest_at:
            return False
        at = datetime.strptime(product.every_post_digest_at, "%H:%M").time()
        return now.time() >= at and self.data["last_sent"].get(product.name) != now.date().isoformat()

    def send(self, product: Product, now: datetime) -> int:
        items = self.data["items"].get(product.name, [])
        if items:
            webhook = os.environ.get(product.webhook_env)
            if not webhook:
                log(f"{product.name}: {product.webhook_env} is not set; keeping the digest")
                return 0
            for message in digest_messages(product, items):
                send_discord(webhook, message)
        self.data["items"][product.name] = []
        self.data["last_sent"][product.name] = now.date().isoformat()
        self.save()
        return len(items)


def digest_messages(product: Product, items: list[dict]) -> list[dict]:
    subs = ", ".join(f"r/{s}" for s in product.every_post_subreddits)
    header = {
        "username": "Lead finder",
        "allowed_mentions": {"parse": []},
        "content": f"📋 **{len(items)} new post(s)** in {subs} since the last digest. "
                   "Pick the few you can give real feedback on.",
    }
    if product.every_post_reply_notes:
        header["embeds"] = [{"title": "How to reply",
                             "description": reply_notes(product.every_post_reply_notes)}]
    lines = []
    for i in items:
        title = i["title"].replace("[", "(").replace("]", ")")
        title = title if len(title) <= 100 else title[:99] + "…"
        lines.append(f"• [{title}]({i['link']}) · r/{i['subreddit']}")
    chunks, cur = [], ""
    for line in lines:
        if cur and len(cur) + len(line) + 1 > 4000:  # embed description limit is 4096
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return [header] + [
        {"username": "Lead finder", "embeds": [{"description": c, "color": 0x5865F2}]}
        for c in chunks
    ]


def run_pass(settings: dict, products: list[Product], seen: Seen, dry_run: bool, alert: bool,
             outbox: Outbox | None = None, digest: Digest | None = None) -> tuple[int, int, int]:
    """One pass over every feed. Returns (alerts sent, feeds failed, feeds total)."""
    ignore = {a.lower() for a in settings["ignore_authors"]}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=settings["max_age_hours"])
    sent = failed = 0
    feeds = feeds_for(products)
    for p in products:
        if search_due(p, seen):
            feeds += search_feeds(p)
            seen.add(f"_last_search:{p.name}")
    for i, feed in enumerate(feeds):
        if i:
            time.sleep(settings["seconds_between_requests"])
        try:
            items = parse_feed(http_get(feed.url, settings["user_agent"]), feed.sub, feed.kind)
        except RateLimited as e:
            left = len(feeds) - i
            log(f"{feed.label}: {e}; skipping the {left} remaining feed(s) until next pass")
            failed += left
            break
        except Exception as e:  # one bad feed shouldn't stop the pass
            log(f"{feed.label}: {e}")
            failed += 1
            continue
        log(f"{feed.label}: {len(items)} item(s)")
        for it in items:
            for p in products:
                key = f"{p.name}:{it.id}"
                mode = feed.mode_for(p, it)
                if key in seen or (feed.kind == "search" and not mode):
                    continue
                seen.add(key)
                if not alert or not mode or it.author.lower() in ignore:
                    continue
                if it.published and it.published < cutoff:
                    continue
                m = match_item(p, it, mode)
                if not m:
                    continue
                if digest and m.mode == "every_post" and p.every_post_digest_at:
                    digest.add(p, it)
                    log(f"{p.name}: r/{it.subreddit} post -> today's digest")
                    sent += 1
                    continue
                payload = discord_payload(m)
                if dry_run:
                    print(json.dumps(payload, indent=2, ensure_ascii=False))
                elif outbox and is_quiet(datetime.now(TZ), settings["quiet_hours"]):
                    outbox.hold(p.webhook_env, payload)
                    log(f"{p.name}: quiet hours, holding r/{it.subreddit} {it.kind} until they end")
                    sent += 1
                    continue
                else:
                    webhook = os.environ.get(p.webhook_env)
                    if not webhook:
                        log(f"{p.name}: {p.webhook_env} is not set; see .env.example")
                        continue
                    try:
                        send_discord(webhook, payload)
                    except Exception as e:
                        log(f"{p.name}: Discord send failed: {e}")
                        seen.ids.pop(key, None)  # retry next pass
                        continue
                sent += 1
                log(f"{p.name}: r/{it.subreddit} {it.kind} -> {m.keyword or 'every post'}")
        seen.save()
    return sent, failed, len(feeds)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(HERE / "products.toml"))
    ap.add_argument("--state", default=str(HERE / "seen.json"))
    ap.add_argument("--once", action="store_true", help="run one pass and exit")
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    ap.add_argument("--backfill", action="store_true",
                    help="on the first run, alert on what's already in the feeds instead of skipping it")
    args = ap.parse_args(argv)

    load_dotenv(HERE / ".env")
    settings, products = load_config(Path(args.config))
    global TZ
    try:
        TZ = ZoneInfo(settings["timezone"])
    except Exception:
        log(f"unknown timezone {settings['timezone']!r}, using UTC")
    if not products:
        log("no [[product]] blocks in config")
        return 1
    if "YOUR_REDDIT_USERNAME" in settings["user_agent"]:
        log("set user_agent in products.toml to include your Reddit username")
    seen = Seen(Path(args.state))
    outbox = None if args.dry_run else Outbox(Path(args.state).with_name("pending.json"))
    digest = None if args.dry_run else Digest(Path(args.state).with_name("digest.json"))

    first = seen.fresh and not args.backfill
    if first:
        log("first run: recording what's already in the feeds without alerting")
    while True:
        if outbox and outbox.held and not is_quiet(datetime.now(TZ), settings["quiet_hours"]):
            log(f"quiet hours over, sent {outbox.flush(settings['quiet_hours'])} held alert(s)")
        now = datetime.now(TZ)
        for p in products:
            if digest and digest.due(p, now) and not is_quiet(now, settings["quiet_hours"]):
                try:
                    log(f"{p.name}: sent today's digest of {digest.send(p, now)} post(s)")
                except Exception as e:
                    log(f"{p.name}: sending the digest failed, will retry: {e}")
        sent, failed, total = run_pass(settings, products, seen, args.dry_run, alert=not first,
                                       outbox=outbox, digest=digest)
        if outbox:
            outbox.save()
        log(f"pass done, {sent} alert(s), {failed}/{total} feed(s) failed")
        first = False
        if args.once:
            if failed == total:
                log("every feed failed: Reddit may be blocking this machine's IP")
                return 2
            return 0
        time.sleep(settings["poll_minutes"] * 60)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)
