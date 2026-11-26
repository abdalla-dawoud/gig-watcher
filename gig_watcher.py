#!/usr/bin/env python3
"""
gig_watcher.py — watch remote-job feeds and build a filtered digest.

Pure Python standard library: no pip install needed.

What it does:
  1. Fetches public RSS/Atom feeds (RemoteOK, WeWorkRemotely, ...).
  2. Filters posts by your keywords (e.g. python, junior, arabic, ai).
  3. De-duplicates against a local SQLite database so every post is
     reported once, ever.
  4. Writes a clean Markdown digest (and optionally HTML) you can read,
     or that a scheduled GitHub Action can commit back to the repo.

Usage:
  python gig_watcher.py                      # run with defaults
  python gig_watcher.py --keywords python ai --since-hours 48
  python gig_watcher.py --out digest.md --html digest.html
  python gig_watcher.py --demo               # print digest to stdout

Author: Abdalla Dawoud — https://github.com/abdalla-dawoud
License: MIT
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
import textwrap
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

# ---------------------------------------------------------------- config

# (name, url, kind) — kind is "rss" (RSS 2.0 / Atom) or "json" (Remotive API)
DEFAULT_SOURCES = [
    ("WeWorkRemotely — Programming", "https://weworkremotely.com/categories/remote-programming-jobs.rss", "rss"),
    ("Himalayas", "https://himalayas.app/jobs/rss", "rss"),
    ("Remotive", "https://remotive.com/api/remote-jobs?limit=100", "json"),
]

DEFAULT_KEYWORDS = [
    "python", "junior", "entry", "ai", "machine learning", "llm",
    "arabic", "backend", "automation", "data", "chatbot",
]

USER_AGENT = "gig-watcher/1.0 (+https://github.com/abdalla-dawoud/gig-watcher)"
DB_PATH = Path("seen.sqlite3")


# ---------------------------------------------------------------- model

@dataclass
class Post:
    source: str
    title: str
    link: str
    published: datetime | None
    summary: str = ""
    matched: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- feeds

def fetch(url: str, timeout: int = 25) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_date(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)  # RFC 822 (RSS pubDate)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def strip_tags(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_feed(source: str, xml_bytes: bytes) -> list[Post]:
    """Parse RSS 2.0 or Atom into Post objects."""
    root = ET.fromstring(xml_bytes)
    posts: list[Post] = []

    # RSS 2.0: <channel><item>...
    for item in root.iter("item"):
        def tag(name: str) -> str:
            el = item.find(name)
            return (el.text or "").strip() if el is not None else ""
        posts.append(Post(
            source=source,
            title=strip_tags(tag("title")),
            link=tag("link"),
            published=parse_date(tag("pubDate") or tag("date")),
            summary=strip_tags(tag("description"))[:400],
        ))

    # Atom: <entry>...
    ns = {"a": "http://www.w3.org/2005/Atom"}
    for entry in root.iter("{http://www.w3.org/2005/Atom}entry"):
        title = entry.findtext("a:title", default="", namespaces=ns)
        link_el = entry.find("a:link", ns)
        published = entry.findtext("a:published", default="", namespaces=ns) or \
            entry.findtext("a:updated", default="", namespaces=ns)
        posts.append(Post(
            source=source,
            title=strip_tags(title),
            link=link_el.get("href", "") if link_el is not None else "",
            published=parse_date(published),
            summary=strip_tags(entry.findtext("a:summary", default="", namespaces=ns))[:400],
        ))

    return [p for p in posts if p.title and p.link]


def parse_remotive(source: str, raw: bytes) -> list[Post]:
    """Parse the Remotive JSON API (https://remotive.com/api/documentation)."""
    data = json.loads(raw.decode("utf-8", "replace"))
    posts = []
    for job in data.get("jobs", []):
        posts.append(Post(
            source=source,
            title=f"{job.get('title', '')} — {job.get('company_name', '')}".strip(" —"),
            link=job.get("url", ""),
            published=parse_date(job.get("publication_date", "")),
            summary=strip_tags(job.get("description", ""))[:400],
        ))
    return [p for p in posts if p.title and p.link]


# ---------------------------------------------------------------- filter

def match_keywords(post: Post, keywords: list[str]) -> list[str]:
    """Substring match for phrases; whole-word match for short keywords
    (so 'ai' does not fire on 'email')."""
    haystack = f"{post.title} {post.summary}".lower()
    hits = []
    for k in keywords:
        kl = k.lower()
        if len(kl) <= 3:
            if re.search(r"\b" + re.escape(kl) + r"\b", haystack):
                hits.append(k)
        elif kl in haystack:
            hits.append(k)
    return hits


# ---------------------------------------------------------------- dedupe

def seen_init(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE IF NOT EXISTS seen (link TEXT PRIMARY KEY, first_seen TEXT)")
    return conn


def seen_filter(conn: sqlite3.Connection, posts: list[Post]) -> list[Post]:
    fresh = []
    now = datetime.now(timezone.utc).isoformat()
    for p in posts:
        cur = conn.execute("INSERT OR IGNORE INTO seen (link, first_seen) VALUES (?, ?)", (p.link, now))
        if cur.rowcount == 1:  # first time we see this link
            fresh.append(p)
    conn.commit()
    return fresh


# ---------------------------------------------------------------- digest

def build_markdown(posts: list[Post], keywords: list[str], errors: list[str]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = [f"# Gig digest — {now}\n",
           f"{len(posts)} new match(es) for: {', '.join(keywords)}\n"]
    if not posts:
        out.append("\n_No new matching posts since last run._\n")
    by_source: dict[str, list[Post]] = {}
    for p in posts:
        by_source.setdefault(p.source, []).append(p)
    for source, items in by_source.items():
        out.append(f"\n## {source} ({len(items)})\n")
        for p in sorted(items, key=lambda x: x.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True):
            when = p.published.strftime("%Y-%m-%d") if p.published else "n/a"
            tags = ", ".join(f"`{m}`" for m in p.matched)
            out.append(f"- [{p.title}]({p.link}) — {when} — {tags}")
    if errors:
        out.append("\n## Fetch errors (sources skipped)\n")
        out += [f"- {e}" for e in errors]
    out.append("")
    return "\n".join(out)


def build_html(markdown_text: str) -> str:
    body_lines = []
    for line in markdown_text.splitlines():
        if line.startswith("# "):
            body_lines.append(f"<h1>{html.escape(line[2:])}</h1>")
        elif line.startswith("## "):
            body_lines.append(f"<h2>{html.escape(line[3:])}</h2>")
        elif line.startswith("- ["):
            m = re.match(r"- \[(.*?)\]\((.*?)\)(.*)", line)
            if m:
                body_lines.append(
                    f'<li><a href="{html.escape(m.group(2))}">{html.escape(m.group(1))}</a>{html.escape(m.group(3))}</li>'
                )
        elif line.strip():
            body_lines.append(f"<p>{html.escape(line)}</p>")
    return textwrap.dedent(f"""\
        <!DOCTYPE html><html><head><meta charset="utf-8">
        <title>Gig digest</title>
        <style>body{{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;line-height:1.6}}
        a{{color:#0ea5e9}} li{{margin:.4rem 0}}</style></head>
        <body>{''.join(body_lines)}</body></html>""")


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Watch remote-job feeds and build a filtered digest.")
    ap.add_argument("--keywords", nargs="+", default=DEFAULT_KEYWORDS, help="keywords to match")
    ap.add_argument("--since-hours", type=float, default=0, help="only posts newer than N hours (0 = all)")
    ap.add_argument("--out", default="digest.md", help="markdown output path")
    ap.add_argument("--html", dest="html_out", default=None, help="optional HTML output path")
    ap.add_argument("--db", default=str(DB_PATH), help="SQLite de-dupe database path")
    ap.add_argument("--demo", action="store_true", help="print digest to stdout instead of writing files")
    ap.add_argument("--no-cache", action="store_true", help="do not record posts as seen (repeat runs re-report)")
    args = ap.parse_args(argv)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=args.since_hours) if args.since_hours else None

    all_posts: list[Post] = []
    errors: list[str] = []
    for source, url, kind in DEFAULT_SOURCES:
        try:
            raw = fetch(url)
            all_posts.extend(parse_remotive(source, raw) if kind == "json" else parse_feed(source, raw))
        except Exception as e:  # one bad feed must not kill the run
            errors.append(f"{source}: {e}")

    matched = [p for p in all_posts if match_keywords(p, args.keywords)]
    for p in matched:
        p.matched = match_keywords(p, args.keywords)

    if cutoff:
        matched = [p for p in matched if not p.published or p.published >= cutoff]

    if not args.no_cache and not args.demo:
        matched = seen_filter(seen_init(Path(args.db)), matched)

    md = build_markdown(matched, args.keywords, errors)

    if args.demo:
        print(md)
    else:
        Path(args.out).write_text(md, encoding="utf-8")
        if args.html_out:
            Path(args.html_out).write_text(build_html(md), encoding="utf-8")
        print(f"Wrote {args.out}: {len(matched)} new match(es) from {len(DEFAULT_SOURCES)} sources"
              + (f"; {len(errors)} source error(s)" if errors else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
