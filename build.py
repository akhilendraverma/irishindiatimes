#!/usr/bin/env python3
"""Build today's edition: fetch RSS/Atom feeds, filter, de-duplicate, render static HTML.

Usage:
    python build.py            # fetch live feeds and write the site into docs/
    python build.py --sample   # offline preview using sample_data.json
"""
import argparse
import html
import json
import math
import re
import shutil
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "docs"
UA = "Mozilla/5.0 (compatible; DailyNewsAggregator/1.0)"
ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/elements/1.1/}"
TAG_RE = re.compile(r"<[^>]+>")
esc = html.escape


def _india_flag():
    spokes = "".join(
        f'<line x1="45" y1="30" x2="{45 + 8.5 * math.cos(math.radians(a * 15)):.2f}" '
        f'y2="{30 + 8.5 * math.sin(math.radians(a * 15)):.2f}"/>' for a in range(24))
    return ('<svg class="flag" viewBox="0 0 90 60" role="img" aria-label="Flag of India">'
            '<rect width="90" height="20" fill="#FF9933"/><rect y="20" width="90" height="20" fill="#fff"/>'
            '<rect y="40" width="90" height="20" fill="#138808"/>'
            '<g stroke="#000080" stroke-width="0.7" fill="none"><circle cx="45" cy="30" r="8.5" stroke-width="1.2"/>'
            + spokes + '</g><circle cx="45" cy="30" r="1.6" fill="#000080"/></svg>')


IRELAND_FLAG = ('<svg class="flag" viewBox="0 0 90 60" role="img" aria-label="Flag of Ireland">'
                '<rect width="30" height="60" fill="#169B62"/><rect x="30" width="30" height="60" fill="#fff"/>'
                '<rect x="60" width="30" height="60" fill="#FF883E"/></svg>')
INDIA_FLAG = _india_flag()


# ---------------------------------------------------------------- fetching ---
class _Redirect(urllib.request.HTTPRedirectHandler):
    """Follow 308 Permanent Redirect too (older Pythons don't)."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, 307 if code == 308 else code, msg, headers, newurl)

    http_error_308 = urllib.request.HTTPRedirectHandler.http_error_302


OPENER = urllib.request.build_opener(_Redirect)


def fetch(url, timeout=20):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
    })
    with OPENER.open(req, timeout=timeout) as r:
        return r.read()


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def clean_text(s, limit=240):
    if not s:
        return ""
    s = re.sub(r"\s+", " ", html.unescape(TAG_RE.sub(" ", s))).strip()
    if len(s) > limit:
        s = s[:limit].rsplit(" ", 1)[0].rstrip(",.;:-") + "…"
    return s


def child_text(el, tag):
    c = el.find(tag)
    return (c.text or "").strip() if c is not None and c.text else ""


def parse_feed(raw, feed):
    root = ET.fromstring(raw)
    items = []
    for it in root.iter("item"):  # RSS 2.0
        src = it.find("source")
        items.append({
            "title": child_text(it, "title"),
            "link": child_text(it, "link"),
            "summary": child_text(it, "description"),
            "published": parse_date(child_text(it, "pubDate") or child_text(it, DC + "date")),
            "source": (src.text or "").strip() if src is not None and src.text else feed["source"],
            "source_url": (src.get("url") if src is not None and src.get("url") else feed.get("homepage", "")),
        })
    for e in root.iter(ATOM + "entry"):  # Atom
        link = ""
        for l in e.findall(ATOM + "link"):
            if l.get("rel", "alternate") == "alternate":
                link = l.get("href", "")
                break
        items.append({
            "title": child_text(e, ATOM + "title"),
            "link": link,
            "summary": child_text(e, ATOM + "summary") or child_text(e, ATOM + "content"),
            "published": parse_date(child_text(e, ATOM + "published") or child_text(e, ATOM + "updated")),
            "source": feed["source"],
            "source_url": feed.get("homepage", ""),
        })
    return items


def normalise(item):
    title = clean_text(item["title"], 300)
    # Google News titles look like "Headline - Publisher"
    suffix = " - " + item["source"]
    if title.endswith(suffix):
        title = title[: -len(suffix)]
    summary = clean_text(item["summary"])
    if summary.lower().startswith(title.lower()[:40]):  # feed repeats the headline
        summary = ""
    return {**item, "title": title, "summary": summary}


def matches(item, keywords):
    if not keywords:
        return True
    hay = f'{item["title"]} {item["summary"]}'
    return any(re.search(r"\b" + re.escape(k), hay, re.I) for k in keywords)


def collect_live(cfg, now):
    jobs = [(s, f) for s in cfg["sections"] for f in s["feeds"]]

    cache = {}

    def get(url, f):
        if url not in cache:
            cache[url] = parse_feed(fetch(url), f)
        return cache[url]

    def run(job):
        s, f = job
        err = None
        try:
            items = get(f["url"], f)
            if items or not f.get("fallback"):
                return job, items, None
            err = "feed was empty"
        except Exception as ex:  # one broken feed must not stop the paper
            err = ex
        if f.get("fallback"):
            print(f"note: {f['source']} direct feed failed ({err}); using Google News backup", file=sys.stderr)
            try:
                return job, get(f["fallback"], f), None
            except Exception as ex:
                err = ex
        return job, [], err

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(run, jobs))

    raw = {s["id"]: [] for s in cfg["sections"]}
    for (s, f), items, err in results:
        if err:
            print(f"warning: {f['source']} ({f['url'][:60]}…) failed: {err}", file=sys.stderr)
            continue
        kws = f.get("keywords", s.get("keywords"))
        raw[s["id"]] += [i for i in map(normalise, items) if i["title"] and i["link"] and matches(i, kws)]
    return raw


def collect_sample(now):
    data = json.loads((ROOT / "sample_data.json").read_text(encoding="utf-8"))
    out = {}
    for sid, items in data.items():
        out[sid] = [{**i, "published": now - timedelta(hours=i.pop("hours_ago", 1))} for i in items]
    return out


def select(cfg, raw, now):
    """Age filter, de-duplicate across sections (earlier sections win), sort, cap."""
    max_age = timedelta(hours=cfg["site"].get("max_age_hours", 36))
    cap = cfg["site"].get("max_items_per_section", 10)
    per_source = cfg["site"].get("max_per_source", 99)
    seen = set()
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    out = {}
    for s in cfg["sections"]:
        picked = []
        counts = {}
        for i in sorted(raw.get(s["id"], []), key=lambda i: i["published"] or epoch, reverse=True):
            if i["published"] and now - i["published"] > max_age:
                continue
            key = re.sub(r"[^a-z0-9]", "", i["title"].lower())[:80]
            if key in seen or i["link"] in seen:
                continue
            if counts.get(i["source"], 0) >= per_source:
                continue
            counts[i["source"]] = counts.get(i["source"], 0) + 1
            seen.update({key, i["link"]})
            picked.append(i)
            if len(picked) >= cap:
                break
        out[s["id"]] = picked
    return out


# --------------------------------------------------------------- rendering ---
def long_date(d):
    # Portable (Windows-safe) "Thursday, 29 October 2026"
    return f"{d:%A}, {d.day} {d:%B %Y}"


def fmt_time(d, tz):
    return d.astimezone(tz).strftime("%d %b, %H:%M") if d else ""


def render_story(i, tz, lead=False):
    cls = "story lead" if lead else "story"
    summary = f'<p class="dek">{esc(i["summary"])}</p>' if i["summary"] else ""
    return f"""
      <article class="{cls}">
        <h3><a href="{esc(i['link'])}" target="_blank" rel="noopener">{esc(i['title'])}</a></h3>
        {summary}
        <p class="meta"><span class="src">{esc(i['source'])}</span><time>{fmt_time(i['published'], tz)}</time></p>
      </article>"""


def render_page(cfg, chosen, today, tz, edition_no, prefix, sample):
    site = cfg["site"]
    nav = "".join(f'<a href="#{s["id"]}">{esc(s.get("nav", s["title"].split(":")[0]))}</a>' for s in cfg["sections"])
    sections = []
    for s in cfg["sections"]:
        items = chosen.get(s["id"], [])
        if items:
            body = render_story(items[0], tz, lead=True)
            body += '<div class="grid">' + "".join(render_story(i, tz) for i in items[1:]) + "</div>"
        else:
            body = '<p class="empty">No new stories in this section today.</p>'
        lang = f' lang="{esc(s["lang"])}"' if s.get("lang") else ""
        sections.append(f'<section id="{s["id"]}"{lang}><h2><span>{esc(s["title"])}</span></h2>{body}</section>')

    # Sources actually cited in this edition
    sources = {}
    for items in chosen.values():
        for i in items:
            sources.setdefault(i["source"], i.get("source_url", ""))
    src_list = "".join(
        f'<li><a href="{esc(u)}" target="_blank" rel="noopener">{esc(n)}</a></li>' if u else f"<li>{esc(n)}</li>"
        for n, u in sorted(sources.items(), key=lambda kv: kv[0].lower())
    ) or "<li>None</li>"

    banner = ('<div class="sample-banner">PREVIEW: built with sample data. These are not real news stories.</div>'
              if sample else "")
    date_long = long_date(today)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(site['title'])}: {date_long}</title>
<meta name="description" content="{esc(site['tagline'])}">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=Inter:wght@500;600&family=Noto+Serif+Devanagari:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="{prefix}style.css">
</head>
<body>
{banner}
<header class="masthead">
  <div class="topline"><span>{date_long}</span><span>{esc(site['city'])} Edition · No. {edition_no}</span><a href="{prefix}archive/">Archive</a></div>
  <div class="title-row">{IRELAND_FLAG}<h1><a href="{prefix}index.html">{esc(site['title'])}</a></h1>{INDIA_FLAG}</div>
  <p class="tagline">{esc(site['tagline'])}</p>
  <nav>{nav}</nav>
</header>
<main>
{''.join(sections)}
</main>
<footer>
  <h2>Sources &amp; Disclaimer</h2>
  <div class="cols">
    <div>
      <h4>Sources cited in this edition</h4>
      <ul class="sources">{src_list}</ul>
    </div>
    <div class="disclaimer">
      <p><strong>Disclaimer.</strong> {esc(site['title'])} is an automated news digest. Headlines and short excerpts are
      reproduced from the publishers' public RSS feeds, and every story links to its original source. All rights in that
      content belong to the respective publishers. We do not write, edit or independently verify these reports, and their
      inclusion does not imply endorsement. Please read the full article at the source before relying on it.</p>
      <p>Publishers who want a feed removed or corrected can contact
      <a href="mailto:{esc(site['contact_email'])}">{esc(site['contact_email'])}</a>.</p>
      <p class="built">Edition generated {datetime.now(tz).strftime('%d %b %Y, %H:%M %Z')}.</p>
    </div>
  </div>
</footer>
</body>
</html>
"""


def render_archive(cfg, dates):
    site = cfg["site"]
    rows = "".join(
        f'<li><a href="{d}.html">{long_date(datetime.strptime(d, "%Y-%m-%d"))}</a></li>'
        for d in sorted(dates, reverse=True))
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Archive: {esc(site['title'])}</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=Inter:wght@500;600&family=Noto+Serif+Devanagari:wght@400;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="../style.css"></head>
<body><header class="masthead"><div class="topline"><span>Archive</span><a href="../index.html">Today's edition</a></div>
<div class="title-row">{IRELAND_FLAG}<h1><a href="../index.html">{esc(site['title'])}</a></h1>{INDIA_FLAG}</div><p class="tagline">Past editions</p></header>
<main><ul class="archive">{rows}</ul></main></body></html>
"""


# -------------------------------------------------------------------- main ---
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="build from sample_data.json (offline preview)")
    args = ap.parse_args()

    cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    tz = ZoneInfo(cfg["site"].get("timezone", "Europe/Dublin"))
    now = datetime.now(timezone.utc)
    today = now.astimezone(tz)

    raw = collect_sample(now) if args.sample else collect_live(cfg, now)
    chosen = select(cfg, raw, now)
    total = sum(len(v) for v in chosen.values())
    if total == 0:
        sys.exit("error: no stories collected; keeping the previous edition online.")

    (OUT / "archive").mkdir(parents=True, exist_ok=True)
    shutil.copy(ROOT / "static" / "style.css", OUT / "style.css")
    (OUT / ".nojekyll").touch()

    stamp = today.strftime("%Y-%m-%d")
    dates = {p.stem for p in (OUT / "archive").glob("????-??-??.html")} | {stamp}
    edition_no = len(dates)

    (OUT / "index.html").write_text(render_page(cfg, chosen, today, tz, edition_no, "", args.sample), encoding="utf-8")
    (OUT / "archive" / f"{stamp}.html").write_text(render_page(cfg, chosen, today, tz, edition_no, "../", args.sample), encoding="utf-8")
    (OUT / "archive" / "index.html").write_text(render_archive(cfg, dates), encoding="utf-8")
    print(f"Built edition {edition_no} for {stamp}: {total} stories -> {OUT}")


if __name__ == "__main__":
    main()
