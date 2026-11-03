#!/usr/bin/env python3
"""Build today's edition: fetch RSS/Atom feeds, filter, de-duplicate, render static HTML.

Usage:
    python build.py            # fetch live feeds and write the site into docs/
    python build.py --sample   # offline preview using sample_data.json
"""
import argparse
import hashlib
import html
import json
import math
import re
import shutil
import sys
import urllib.parse
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


FONTS = ('<link rel="preconnect" href="https://fonts.googleapis.com">\n'
         '<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@700;900&family=Source+Serif+4:'
         'opsz,wght@8..60,400;8..60,600&family=Inter:wght@500;600&family=Noto+Serif+Devanagari:wght@400;700'
         '&display=swap" rel="stylesheet">')


def story_id(item):
    return hashlib.sha1(item["link"].encode("utf-8")).hexdigest()[:12]


def story_href(item, prefix):
    if item.get("own"):
        return f"{prefix}news/{item['slug']}.html"
    return f"{prefix}story/{story_id(item)}.html"


def render_story(i, tz, prefix, lead=False):
    cls = "story lead" if lead else "story"
    summary = f'<p class="dek">{esc(i["summary"])}</p>' if i["summary"] else ""
    badge = '<span class="original">IrishIndiaTimes original</span>' if i.get("own") else ""
    when = (fmt_time(i["published"], tz) if not i.get("own") else long_date(i["published"].astimezone(tz)))
    byline = f"By {i['author']}" if i.get("own") else i["source"]
    return f"""
      <article class="{cls}">
        {badge}<h3><a href="{story_href(i, prefix)}">{esc(i['title'])}</a></h3>
        {summary}
        <p class="meta"><span class="src">{esc(byline)}</span><time>{when}</time></p>
      </article>"""


def page_head(title, prefix, description="", robots=""):
    desc = f'<meta name="description" content="{esc(description)}">\n' if description else ""
    rob = f'<meta name="robots" content="{robots}">\n' if robots else ""
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
{desc}{rob}{FONTS}
<link rel="stylesheet" href="{prefix}style.css">
</head>
<body>
"""


def masthead(site, prefix, left, middle="", nav="", small=False):
    radio = (f'<a href="{esc(site["radio_url"])}">&#9654; Irish India Radio</a> · ' if site.get("radio_url") else "")
    mid = f"<span>{middle}</span>" if middle else ""
    tagline = "" if small else f'<p class="tagline">{esc(site["tagline"])}</p>'
    navhtml = f"<nav>{nav}</nav>" if nav else ""
    return f"""<header class="masthead{' small' if small else ''}">
  <div class="topline"><span>{left}</span>{mid}<span class="links">{radio}<a href="{prefix}archive/">Archive</a></span></div>
  <div class="title-row">{IRELAND_FLAG}<h1><a href="{prefix}index.html">{esc(site['title'])}</a></h1>{INDIA_FLAG}</div>
  {tagline}
  {navhtml}
</header>
"""


def footer(site, tz, sources=None):
    src_block = ""
    if sources is not None:
        src_list = "".join(
            f'<li><a href="{esc(u)}" target="_blank" rel="noopener">{esc(n)}</a></li>' if u else f"<li>{esc(n)}</li>"
            for n, u in sorted(sources.items(), key=lambda kv: kv[0].lower())
        ) or "<li>None</li>"
        src_block = f'<div><h4>Sources cited on this page</h4><ul class="sources">{src_list}</ul></div>'
    return f"""<footer>
  <h2>Sources &amp; Disclaimer</h2>
  <div class="cols">
    {src_block}
    <div class="disclaimer">
      <p><strong>Disclaimer.</strong> {esc(site['title'])} is a news digest. Headlines and short excerpts from other
      publishers are reproduced from their public RSS feeds, and every one of those stories links to its original source.
      All rights in that content belong to the respective publishers. We do not write, edit or independently verify their
      reports, and their inclusion does not imply endorsement. Please read the full article at the source before relying
      on it. Articles marked <em>IrishIndiaTimes original</em> are written by our own contributors.</p>
      <p><a href="{site.get('site_url', '').rstrip('/') + '/' if site.get('site_url') else ''}advertise.html">Advertise with {esc(site['title'])}</a></p>
      <p>Publishers who want a feed removed or corrected can contact
      <a href="mailto:{esc(site['contact_email'])}">{esc(site['contact_email'])}</a>.</p>
      <p class="built">Page generated {datetime.now(tz).strftime('%d %b %Y, %H:%M %Z')}.</p>
    </div>
  </div>
</footer>
</body>
</html>
"""


def first_sentence(text, limit=170):
    """The first sentence of a feed excerpt, kept short."""
    if not text:
        return ""
    m = re.match(r"(.+?[.!?।])(\s|$)", text)
    out = m.group(1) if m else text
    if len(out) > limit:
        out = out[:limit].rsplit(" ", 1)[0].rstrip(",.;:-") + "…"
    return out


def render_briefing(cfg, chosen, prefix):
    """'Today at a glance': the top story from each section, one line each. Free, no AI needed."""
    picks = []
    for s in cfg["sections"]:
        items = chosen.get(s["id"], [])
        take = 2 if s["id"] == "community" else 1
        picks += [(s, i) for i in items[:take]]
    picks = picks[:cfg["site"].get("briefing_items", 10)]
    if len(picks) < 3:
        return ""
    rows, words = [], 0
    for s, i in picks:
        line = first_sentence(i["summary"])
        words += len(i["title"].split()) + len(line.split())
        lang = f' lang="{esc(s["lang"])}"' if s.get("lang") else ""
        rows.append(
            f'<li{lang}><span class="sec">{esc(s.get("nav", s["title"].split(":")[0]))}</span>'
            f'<a href="{story_href(i, prefix)}">{esc(i["title"])}</a>'
            + (f' <span class="line">{esc(line)}</span>' if line else "")
            + f' <span class="src">{esc(i["source"])}</span></li>')
    minutes = max(1, round(words / 200 + 0.4))
    return (f'<section id="glance" class="glance"><h2><span>Today at a glance</span>'
            f'<em>{minutes}-minute read</em></h2><ol>{"".join(rows)}</ol></section>')


# ------------------------------------------------------------------- ads ---
def pick_ad(cfg, today, slot):
    """Rotate house ads by day and slot so readers see a different one each day."""
    ads = cfg.get("ads", [])
    if not ads:
        return None
    return ads[(today.toordinal() + slot) % len(ads)]


def render_ad(ad, prefix, wide=False):
    book = ad.get("shape") == "book"
    w, h = (60, 90) if book else (72, 72)
    img = (f'<img class="{"book" if book else "icon"}" src="{prefix}{esc(ad["image"])}" alt="" '
           f'width="{w}" height="{h}" loading="lazy">' if ad.get("image") else "")
    tlang = f' lang="{esc(ad["title_lang"])}"' if ad.get("title_lang") else ""
    style = f'--ad-bg:{esc(ad.get("color", "#0B6E4F"))};--ad-accent:{esc(ad.get("accent", "#E0861F"))}'
    return f"""<aside class="ad{' wide' if wide else ''}" style="{style}" aria-label="Advertisement">
  <span class="ad-label">Advertisement</span>
  <a class="ad-body" href="{esc(ad['url'])}" target="_blank" rel="sponsored noopener">
    {img}
    <span class="ad-text">
      <span class="ad-kicker">{esc(ad.get('kicker', ''))}</span>
      <strong{tlang}>{esc(ad['title'])}</strong>
      <span class="ad-copy">{esc(ad['text'])}</span>
    </span>
    <span class="ad-cta">{esc(ad['cta'])} &rarr;</span>
  </a>
</aside>"""


def render_advertise_slot(cfg, prefix):
    if not cfg.get("advertise", {}).get("enabled"):
        return ""
    return f"""<aside class="ad yours" aria-label="Advertise with us">
  <span class="ad-label">Your ad here</span>
  <a class="ad-body" href="{prefix}advertise.html">
    <span class="ad-text">
      <span class="ad-kicker">Advertise with {esc(cfg['site']['title'])}</span>
      <strong>Put your business in front of our readers</strong>
      <span class="ad-copy">{esc(cfg['advertise'].get('pitch', ''))}</span>
    </span>
    <span class="ad-cta">See ad options &rarr;</span>
  </a>
</aside>"""


def ad_row(cfg, today, slot, prefix):
    ad = pick_ad(cfg, today, slot)
    yours = render_advertise_slot(cfg, prefix)
    cards = (render_ad(ad, prefix) if ad else "") + yours
    return f'<div class="adrow">{cards}</div>' if cards else ""


def render_advertise_page(cfg, tz, sample):
    site = cfg["site"]
    p = ""
    email = site["contact_email"]
    subject = f"Advertising on {site['title']}"
    mailto = f"mailto:{email}?subject={urllib.parse.quote(subject)}"
    body = f"""<main class="storypage">
  <p class="kicker">Advertise with us</p>
  <h1 class="headline">Advertise to the Indian community in Ireland</h1>
  <p class="excerpt">{esc(cfg.get('advertise', {}).get('pitch', ''))}</p>
  <div class="articlebody">
    <h3>Where your ad appears</h3>
    <ul>
      <li><strong>Front-page banner</strong>: a wide box directly under "Today at a glance", the first thing readers see.</li>
      <li><strong>In-page boxes</strong>: between the news sections on the front page.</li>
      <li><strong>Story pages</strong>: on the page readers open for every headline.</li>
      <li><strong>Irish India Radio</strong>: ask about a short spoken mention on air.</li>
    </ul>
    <h3>Who reads {esc(site['title'])}</h3>
    <p>Members of the Indian and wider Asian community across Ireland, with daily news on immigration and visas,
    Irish and Indian news, and community events from Dublin, Cork, Galway and Limerick.</p>
    <h3>What we need from you</h3>
    <p>Your business name, a logo or square image, one or two lines of text and the web page to link to.
    Every ad is clearly labelled "Advertisement". We don't accept ads for gambling, alcohol, payday loans,
    or anything misleading.</p>
  </div>
  <p><a class="readbtn" href="{esc(mailto)}">Email us about advertising</a></p>
  <p class="note">Or write to {esc(email)}. We'll reply with available dates and prices.</p>
</main>
"""
    return (page_head(f"Advertise | {site['title']}", p, "Advertise to the Indian community in Ireland.")
            + (SAMPLE_BANNER if sample else "")
            + masthead(site, p, '<a href="index.html">&larr; Today\'s edition</a>', small=True)
            + body + footer(site, tz))


SAMPLE_BANNER = '<div class="sample-banner">PREVIEW: built with sample data. These are not real news stories.</div>'


def render_page(cfg, chosen, newsroom, today, tz, edition_no, prefix, sample):
    site = cfg["site"]
    nav_items = [(s["id"], s.get("nav", s["title"].split(":")[0])) for s in cfg["sections"]]
    if newsroom:
        nav_items.insert(0, ("newsroom", "Our stories"))
    nav_items.insert(0, ("glance", "At a glance"))
    nav = "".join(f'<a href="#{i}">{esc(t)}</a>' for i, t in nav_items)

    sections = []
    glance = render_briefing(cfg, chosen, prefix)
    if glance:
        sections.append(glance)
    top_ad = pick_ad(cfg, today, 0)
    if top_ad:
        sections.append(render_ad(top_ad, prefix, wide=True))
    if newsroom:
        body = render_story(newsroom[0], tz, prefix, lead=True)
        body += '<div class="grid">' + "".join(render_story(i, tz, prefix) for i in newsroom[1:]) + "</div>"
        body += f'<p class="more-link"><a href="{prefix}news/">All IrishIndiaTimes stories &rarr;</a></p>'
        sections.append(f'<section id="newsroom" class="newsroom"><h2><span>From our newsroom</span></h2>{body}</section>')
    for n, s in enumerate(cfg["sections"]):
        if n in (2, 5):
            sections.append(ad_row(cfg, today, n, prefix))
        items = chosen.get(s["id"], [])
        if items:
            body = render_story(items[0], tz, prefix, lead=True)
            body += '<div class="grid">' + "".join(render_story(i, tz, prefix) for i in items[1:]) + "</div>"
        else:
            body = '<p class="empty">No new stories in this section today.</p>'
        lang = f' lang="{esc(s["lang"])}"' if s.get("lang") else ""
        sections.append(f'<section id="{s["id"]}"{lang}><h2><span>{esc(s["title"])}</span></h2>{body}</section>')

    sources = {}
    for items in chosen.values():
        for i in items:
            sources.setdefault(i["source"], i.get("source_url", ""))

    date_long = long_date(today)
    return (page_head(f"{site['title']}: {date_long}", prefix, site["tagline"])
            + (SAMPLE_BANNER if sample else "")
            + masthead(site, prefix, date_long, f"{esc(site['city'])} Edition · No. {edition_no}", nav)
            + f"<main>\n{''.join(sections)}\n</main>\n"
            + footer(site, tz, sources))


def render_story_page(cfg, item, section, siblings, tz, sample):
    """A page on our own site for one headline: excerpt, link to the full story, more from the section."""
    site = cfg["site"]
    p = "../"
    excerpt = (f'<p class="excerpt">{esc(item["summary"])}</p>' if item["summary"] else
               f'<p class="excerpt muted">{esc(item["source"])} didn\'t include a summary with this headline.</p>')
    others = [o for o in siblings if o["link"] != item["link"]][:6]
    more = "".join(
        f'<li><a href="{story_href(o, p)}">{esc(o["title"])}</a> <span class="src">{esc(o["source"])}</span></li>'
        for o in others)
    more_block = (f'<section class="more"><h2><span>More from {esc(section["title"])}</span></h2><ul>{more}</ul></section>'
                  if more else "")
    lang = f' lang="{esc(section["lang"])}"' if section.get("lang") else ""
    slot = int(story_id(item)[:4], 16)  # different house ad on different story pages
    ad = pick_ad(cfg, datetime.now(tz), slot)
    ad_html = render_ad(ad, p, wide=True) if ad else ""
    body = f"""<main class="storypage">
  <p class="kicker"><a href="{p}index.html#{esc(section['id'])}">{esc(section['title'])}</a></p>
  <article{lang}>
    <h1 class="headline">{esc(item['title'])}</h1>
    <p class="meta"><span class="src">{esc(item['source'])}</span><time>{fmt_time(item['published'], tz)}</time></p>
    {excerpt}
    <p><a class="readbtn" href="{esc(item['link'])}" target="_blank" rel="noopener">Read the full story on {esc(item['source'])} &#8599;</a></p>
    <p class="note">This headline and excerpt come from {esc(item['source'])}'s public news feed. The full article is on their website.</p>
  </article>
  {ad_html}
  {more_block}
  <p class="back"><a href="{p}index.html">&larr; Back to today's edition</a></p>
</main>
"""
    return (page_head(f"{item['title']} | {site['title']}", p, item["summary"] or item["title"], robots="noindex, follow")
            + (SAMPLE_BANNER if sample else "")
            + masthead(site, p, f'<a href="{p}index.html">&larr; Today\'s edition</a>', small=True)
            + body
            + footer(site, tz, {item["source"]: item.get("source_url", "")}))


# ---------------------------------------------------------------- newsroom ---
INLINE = [
    (re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+|[^)\s:]+)\)"), r'<a href="\2">\1</a>'),
    (re.compile(r"\*\*(.+?)\*\*"), r"<strong>\1</strong>"),
    (re.compile(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])"), r"<em>\1</em>"),
]


def inline_md(text):
    out = esc(text, quote=False)
    for rx, rep in INLINE:
        out = rx.sub(rep, out)
    return out


def markdown_to_html(md):
    """Small Markdown subset: paragraphs, ## headings, - bullet lists, > quotes, **bold**, *italic*, [links](url)."""
    html, para, items = [], [], []

    def flush():
        if para:
            html.append("<p>" + inline_md(" ".join(para)) + "</p>")
            para.clear()
        if items:
            html.append("<ul>" + "".join(f"<li>{inline_md(x)}</li>" for x in items) + "</ul>")
            items.clear()

    for line in md.splitlines():
        t = line.strip()
        if not t:
            flush()
        elif t.startswith("### "):
            flush(); html.append(f"<h4>{inline_md(t[4:])}</h4>")
        elif t.startswith("## ") or t.startswith("# "):
            flush(); html.append(f"<h3>{inline_md(t.lstrip('#').strip())}</h3>")
        elif t.startswith(("- ", "* ")):
            if para:
                html.append("<p>" + inline_md(" ".join(para)) + "</p>"); para.clear()
            items.append(t[2:])
        elif t.startswith("> "):
            flush(); html.append(f"<blockquote>{inline_md(t[2:])}</blockquote>")
        else:
            if items:
                flush()
            para.append(t)
    flush()
    return "\n".join(html)


def load_newsroom(tz):
    """Read our own articles from stories/*.md. Bad files are skipped with a warning, never stop the paper."""
    folder = ROOT / "stories"
    out = []
    if not folder.is_dir():
        return out
    for f in sorted(folder.glob("*.md")):
        if f.name.startswith(("_", ".")) or f.name.lower() == "readme.md":
            continue
        try:
            text = f.read_text(encoding="utf-8").replace("\r\n", "\n")
            head, sep, body = text.partition("\n---\n")
            if not sep:
                raise ValueError("missing the --- line between the details and the article")
            meta = {}
            for line in head.splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip().lower()] = v.strip()
            for need in ("title", "date", "author"):
                if not meta.get(need):
                    raise ValueError(f"missing '{need}:'")
            day = datetime.strptime(meta["date"], "%Y-%m-%d").replace(hour=9, tzinfo=tz)
            slug = re.sub(r"[^a-z0-9]+", "-", f.stem.lower()).strip("-")
            image = meta.get("image", "")
            if image and not (folder / "images" / image).is_file():
                print(f"warning: {f.name}: image '{image}' not found in stories/images/", file=sys.stderr)
                image = ""
            out.append({
                "own": True, "slug": slug, "title": meta["title"], "author": meta["author"],
                "summary": meta.get("summary", ""), "image": image, "caption": meta.get("caption", ""),
                "published": day, "body": body.strip(), "source": "IrishIndiaTimes",
                "link": f"news/{slug}.html", "source_url": "",
            })
        except Exception as ex:
            print(f"warning: skipped stories/{f.name}: {ex}", file=sys.stderr)
    out.sort(key=lambda a: a["published"], reverse=True)
    return out


def render_article(cfg, a, tz, sample):
    site = cfg["site"]
    p = "../"
    img = ""
    if a["image"]:
        cap = f"<figcaption>{esc(a['caption'])}</figcaption>" if a["caption"] else ""
        img = f'<figure><img src="images/{esc(a["image"])}" alt="{esc(a["caption"] or a["title"])}">{cap}</figure>'
    dek = f'<p class="excerpt">{esc(a["summary"])}</p>' if a["summary"] else ""
    body = f"""<main class="storypage">
  <p class="kicker"><a href="./">From our newsroom</a></p>
  <article class="own">
    <span class="original">IrishIndiaTimes original</span>
    <h1 class="headline">{esc(a['title'])}</h1>
    {dek}
    <p class="meta"><span class="src">By {esc(a['author'])}</span><time>{long_date(a['published'])}</time></p>
    {img}
    <div class="articlebody">{markdown_to_html(a['body'])}</div>
  </article>
  <p class="back"><a href="./">More IrishIndiaTimes stories</a> · <a href="{p}index.html">Today's edition</a></p>
</main>
"""
    return (page_head(f"{a['title']} | {site['title']}", p, a["summary"] or a["title"])
            + (SAMPLE_BANNER if sample else "")
            + masthead(site, p, f'<a href="{p}index.html">&larr; Today\'s edition</a>', small=True)
            + body + footer(site, tz))


def render_newsroom_index(cfg, articles, tz, sample):
    site = cfg["site"]
    p = "../"
    rows = "".join(
        f'<li><a href="{a["slug"]}.html">{esc(a["title"])}</a>'
        f'<span class="src">{long_date(a["published"])} · By {esc(a["author"])}</span>'
        + (f'<p>{esc(a["summary"])}</p>' if a["summary"] else "") + "</li>"
        for a in articles) or '<li class="empty">No stories yet.</li>'
    body = f"""<main class="storypage">
  <h1 class="headline">From our newsroom</h1>
  <p class="excerpt">Original reporting by IrishIndiaTimes contributors.</p>
  <ul class="newslist">{rows}</ul>
</main>
"""
    return (page_head(f"Our stories | {site['title']}", p, "Original reporting by IrishIndiaTimes contributors.")
            + (SAMPLE_BANNER if sample else "")
            + masthead(site, p, f'<a href="{p}index.html">&larr; Today\'s edition</a>', small=True)
            + body + footer(site, tz))


def render_404(cfg, tz):
    site = cfg["site"]
    p = site.get("site_url", "/").rstrip("/") + "/"
    body = f"""<main class="storypage">
  <h1 class="headline">Page not found</h1>
  <p class="excerpt">This page isn't on {esc(site['title'])} any more. Story pages for other publishers' headlines are
  kept for {cfg['site'].get('story_days', 30)} days.</p>
  <p><a class="readbtn" href="{p}">Go to today's edition</a></p>
</main>
"""
    return (page_head(f"Page not found | {site['title']}", p, robots="noindex")
            + masthead(site, p, "Page not found", small=True) + body + footer(site, tz))


def render_archive(cfg, dates):
    site = cfg["site"]
    rows = "".join(
        f'<li><a href="{d}.html">{long_date(datetime.strptime(d, "%Y-%m-%d"))}</a></li>'
        for d in sorted(dates, reverse=True))
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Archive: {esc(site['title'])}</title>
{FONTS}
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
    if (ROOT / "static" / "ads").is_dir():
        shutil.rmtree(OUT / "ads", ignore_errors=True)
        shutil.copytree(ROOT / "static" / "ads", OUT / "ads")
    (OUT / "advertise.html").write_text(render_advertise_page(cfg, tz, args.sample), encoding="utf-8")
    (OUT / ".nojekyll").touch()
    shutil.rmtree(OUT / "radio", ignore_errors=True)  # radio now lives on its own site

    # Our own articles (stories/*.md) -> docs/news/
    articles = load_newsroom(tz)
    news_dir = OUT / "news"
    shutil.rmtree(news_dir, ignore_errors=True)
    if articles:
        news_dir.mkdir(parents=True)
        if (ROOT / "stories" / "images").is_dir():
            shutil.copytree(ROOT / "stories" / "images", news_dir / "images")
        for a in articles:
            (news_dir / f"{a['slug']}.html").write_text(render_article(cfg, a, tz, args.sample), encoding="utf-8")
        (news_dir / "index.html").write_text(render_newsroom_index(cfg, articles, tz, args.sample), encoding="utf-8")
    recent = now - timedelta(days=cfg["site"].get("newsroom_days", 14))
    newsroom = [a for a in articles if a["published"] >= recent][:cfg["site"].get("max_items_per_section", 10)]

    # A page on our site for every headline -> docs/story/ (kept for story_days)
    story_dir = OUT / "story"
    story_dir.mkdir(exist_ok=True)
    manifest_path = story_dir / "index.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        manifest = {}
    stamp = today.strftime("%Y-%m-%d")
    for s in cfg["sections"]:
        items = chosen.get(s["id"], [])
        for i in items:
            sid = story_id(i)
            (story_dir / f"{sid}.html").write_text(render_story_page(cfg, i, s, items, tz, args.sample), encoding="utf-8")
            manifest.setdefault(sid, stamp)
    keep_after = (today - timedelta(days=cfg["site"].get("story_days", 30))).strftime("%Y-%m-%d")
    for sid, day in list(manifest.items()):
        if day < keep_after:
            (story_dir / f"{sid}.html").unlink(missing_ok=True)
            del manifest[sid]
    manifest_path.write_text(json.dumps(manifest, indent=0, sort_keys=True), encoding="utf-8")
    (OUT / "404.html").write_text(render_404(cfg, tz), encoding="utf-8")

    dates = {p.stem for p in (OUT / "archive").glob("????-??-??.html")} | {stamp}
    edition_no = len(dates)

    (OUT / "index.html").write_text(render_page(cfg, chosen, newsroom, today, tz, edition_no, "", args.sample), encoding="utf-8")
    (OUT / "archive" / f"{stamp}.html").write_text(render_page(cfg, chosen, newsroom, today, tz, edition_no, "../", args.sample), encoding="utf-8")
    (OUT / "archive" / "index.html").write_text(render_archive(cfg, dates), encoding="utf-8")
    print(f"Built edition {edition_no} for {stamp}: {total} headlines, {len(newsroom)} newsroom stories -> {OUT}")


if __name__ == "__main__":
    main()
