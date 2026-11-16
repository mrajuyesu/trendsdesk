#!/usr/bin/env python3
"""AI autoblog: trends -> categorize -> research -> write -> quality gate
-> save by category (draft) -> approve -> build static site.

Usage:
  python autoblog.py run               # find trends, write drafts into posts/<category>/
  python autoblog.py list              # show all posts and their status
  python autoblog.py approve <slug>    # publish one draft (or: approve --all)
  python autoblog.py build             # generate the website into site/
Free AI: set GEMINI_API_KEY (Google AI Studio). Paid: pip install anthropic + ANTHROPIC_API_KEY.
"""
import os, sys, re, json, html, datetime, urllib.request, urllib.parse, urllib.error, time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).parent
POSTS, SITE = ROOT / "posts", ROOT / "site"
OWNER = os.getenv("OWNER_NAME", "Rajuyesu")   # shown as publisher/author
SITE_NAME = os.getenv("BLOG_NAME", "Trenddesk")   # not SITE_NAME: Netlify overwrites that one
GEO = os.getenv("TRENDS_GEO", "IN")                 # country code for trends
MODEL = os.getenv("MODEL", "claude-sonnet-5-5")
MAX_POSTS = int(os.getenv("MAX_POSTS", "3"))        # drafts per run
MIN_SCORE = 7                                       # quality gate (1-10)

CATEGORIES = {
    "technology": "Gadgets, software, AI, internet and science news",
    "health": "General wellness, sleep, nutrition, fitness and mental health explainers (information only)",
    "business": "Companies, markets, startups, economy (explain, never give investment advice)",
    "entertainment": "Films, series, music, gaming, pop culture",
    "sports": "Matches, tournaments, athletes, results",
    "lifestyle": "Travel, food, shopping, everyday how-tos",
}
# Auto-skip: tragedies, unverified rumours, personal medical/legal/financial advice (general explainers are fine), adult or hateful topics.
AVOID = "breaking tragedies or deaths, unverified rumours, medical/legal/financial advice, adult or hateful content"

STYLE = """You are a staff writer for a trend-explainer site. Write like a sharp, friendly human editor:
- Open with why this matters right now, not with filler.
- Concrete facts, names, dates and numbers from the sources only. Never invent quotes or stats.
- One clear point of view or takeaway; vary sentence length; short paragraphs.
- Use ## subheadings, 600-900 words, end with a short 'What to watch' section.
- Cite sources inline as markdown links. Do not copy source wording.
- Health topics: general information only, from reputable bodies (WHO, CDC, NHS). No diagnoses, doses or cures; say when to see a doctor."""


WORKING = {"model": None}


def gem(prompt, system=""):
    """Free Gemini API. Tries several model names (Google retires them often) and remembers the one that works."""
    key = os.environ["GEMINI_API_KEY"].strip()
    models = [m for m in (WORKING["model"], os.getenv("GEMINI_MODEL"), "gemini-flash-latest", "gemini-3-flash-preview",
                          "gemini-2.5-flash", "gemini-3.5-flash-lite") if m]
    err = "no model tried"
    for m in dict.fromkeys(models):
        body = {"contents": [{"parts": [{"text": prompt}]}]}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": key})
        try:
            r = json.load(urllib.request.urlopen(req, timeout=90))
            WORKING["model"] = m
            print(f"  (model {m})", flush=True)
            return "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])
        except urllib.error.HTTPError as e:
            err = f"{m}: HTTP {e.code} {e.read()[:150]!r}"
            print("  !", err, flush=True)
            if e.code == 429:
                time.sleep(10)
        except Exception as e:
            err = f"{m}: {e}"
            print("  !", err, flush=True)
    raise RuntimeError("Gemini failed - " + err)


def llm(prompt, system="", max_tokens=4000):
    if os.getenv("GEMINI_API_KEY"):          # free option
        return gem(prompt, system)
    import anthropic                          # paid option
    r = anthropic.Anthropic().messages.create(model=MODEL, max_tokens=max_tokens, system=system,
                                              messages=[{"role": "user", "content": prompt}])
    return "".join(b.text for b in r.content if b.type == "text")


def jload(text):
    return json.loads(re.search(r"\{.*\}|\[.*\]", text, re.S).group(0))


# ---------- 1. trends + their real news sources (no paid search needed) ----------
def get(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def strip_html(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def _txt(node, name):
    for c in node.iter():
        if c.tag.split("}")[-1] == name and c.text:
            return strip_html(c.text)
    return ""


def fetch_trends():
    """-> [{"title": str, "sources": [{"title","url","snippet"}]}]"""
    out = []
    try:
        root = ET.fromstring(get(f"https://trends.google.com/trending/rss?geo={GEO}"))
        for it in root.iter("item"):
            srcs = [{"title": _txt(n, "news_item_title"), "url": _txt(n, "news_item_url"), "snippet": _txt(n, "news_item_snippet")}
                    for n in it if n.tag.split("}")[-1] == "news_item"]
            if it.findtext("title"):
                out.append({"title": it.findtext("title"), "sources": [s for s in srcs if s["url"]]})
    except Exception as e:
        print("! Google Trends unavailable:", e, flush=True)
    if not out:                                   # fallback: Google News headlines
        for topic in ("TECHNOLOGY", "BUSINESS", "ENTERTAINMENT", "SPORTS", "HEALTH"):
            try:
                root = ET.fromstring(get(f"https://news.google.com/rss/headlines/section/topic/{topic}?hl=en-{GEO}&gl={GEO}&ceid={GEO}:en"))
                for it in list(root.iter("item"))[:6]:
                    t = strip_html(it.findtext("title"))
                    out.append({"title": t, "sources": [{"title": t, "url": it.findtext("link") or "", "snippet": strip_html(it.findtext("description"))}]})
            except Exception as e:
                print("! news feed failed:", topic, e, flush=True)
    return out


def page_text(url):
    try:
        raw = get(url, 12)[:300000].decode("utf-8", "ignore")
        raw = re.sub(r"(?is)<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", raw)
        return strip_html(raw)[:2500]
    except Exception:
        return ""


# ---------- 2. pick + categorize ----------
def categorize(trends):
    cand = [t for t in trends if len(t["sources"]) >= 2] or [t for t in trends if t["sources"]]
    if not cand:
        return []
    listing = "\n".join(f'{i}: {t["title"]}' for i, t in enumerate(cand))
    prompt = (f"Categories: {json.dumps(CATEGORIES)}\nAvoid: {AVOID}\nTrending searches (id: topic):\n{listing}\n"
              f"Pick up to {MAX_POSTS} that fit a category and can support a useful, factual explainer. "
              'Return a JSON list: [{"id":0,"category":"","angle":"one-line original angle"}]. JSON only.')
    return [(cand[int(x["id"])], x) for x in jload(llm(prompt))[:MAX_POSTS] if 0 <= int(x["id"]) < len(cand)]


# ---------- 3-5. research (real sources), write, quality gate ----------
def write_post(trend, pick):
    srcs = trend["sources"][:4]
    pack = []
    for s in srcs:
        s["text"] = page_text(s["url"])
        pack.append(f'SOURCE: {s["title"]}\nURL: {s["url"]}\nSNIPPET: {s["snippet"]}\nPAGE TEXT: {s["text"]}')
    material = "\n\n".join(pack)
    if not any(s["snippet"] or s["text"] for s in srcs):
        return None                              # never write without real source material
    grounded = sum(1 for s in srcs if s["snippet"] or s["text"]) >= 2
    draft = llm("Topic: " + trend["title"] + "\nCategory: " + pick["category"] + "\nAngle: " + str(pick.get("angle", "")) +
                "\nWrite the article using ONLY the source material below. Do not add facts that the sources do not support. "
                'If the material is too thin for a useful article, return {"skip": true}. Otherwise return JSON only: '
                '{"title":"","description":"max 155 chars","tags":["",""],"body":"markdown"}\n\n' + material,
                system=STYLE, max_tokens=6000)
    post = jload(draft)
    if post.get("skip"):
        return None
    post["sources"] = [{"title": s["title"] or s["url"], "url": s["url"]} for s in srcs]
    review = jload(llm("Review this draft as a strict editor against the sources. Score 1-10 for accuracy (every claim supported), "
                       'originality and usefulness. Return JSON only: {"score":0,"problems":[""]}\n\nSOURCES:\n' + material +
                       "\n\nDRAFT:\n" + post["body"]))
    post["score"] = review["score"] if grounded else min(review["score"], 7)   # thin sources -> stays a draft
    return post if review["score"] >= MIN_SCORE else None


# ---------- 6. save by category ----------
def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def save(post, category, trend_title=""):
    d = POSTS / category
    d.mkdir(parents=True, exist_ok=True)
    date = datetime.date.today().isoformat()
    slug = f"{date}-{slugify(post['title'])}"
    if (d / f"{slug}.md").exists():
        return slug + " (already exists, kept)"
    meta = {"title": post["title"], "description": post["description"], "category": category,
            "date": date, "tags": post["tags"], "sources": post["sources"],
            "score": post["score"], "trend": trend_title, "status": "draft"}
    text = "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in meta.items())
    (d / f"{slug}.md").write_text(f"---\n{text}---\n{post['body']}\n", encoding="utf-8")
    return slug


def check():
    """Test everything: python autoblog.py check"""
    print("Writing test:", llm("Reply with the single word OK.").strip()[:40], flush=True)
    t = fetch_trends()
    print(f"Trends found: {len(t)} (with 2+ news sources: {sum(1 for x in t if len(x['sources']) >= 2)})", flush=True)


def run():
    seen = {p.get("trend") for p in all_posts()}
    try:
        trends = [t for t in fetch_trends() if t["title"] not in seen]
        picks = categorize(trends)
    except Exception as e:
        print("! could not pick topics:", e, flush=True)
        return
    for trend, pick in picks:
        if pick.get("category") not in CATEGORIES:
            continue
        print("writing:", trend["title"], flush=True)
        post = None
        for attempt in (1, 2):
            try:
                post = write_post(trend, pick)
                break
            except Exception as e:
                print(f"! attempt {attempt} failed: {e}", flush=True)
        print(("saved draft: " + save(post, pick["category"], trend["title"])) if post else f"skipped: {trend['title']}", flush=True)


# ---------- posts io ----------
def load_post(path):
    raw = path.read_text(encoding="utf-8")
    head, body = raw.split("---\n", 2)[1:]
    meta = {}
    for line in head.splitlines():
        k, v = line.split(": ", 1)
        meta[k] = json.loads(v)
    meta["body"], meta["slug"], meta["path"] = body, path.stem, path
    return meta


def all_posts():
    return sorted((load_post(p) for p in POSTS.rglob("*.md")), key=lambda p: p["date"], reverse=True)


def approve(arg):
    for p in all_posts():
        if p["status"] == "draft" and (arg == "--all" or p["slug"] == arg or (arg == "--good" and p["score"] >= 8)):
            t = p["path"].read_text(encoding="utf-8").replace('status: "draft"', 'status: "published"', 1)
            p["path"].write_text(t, encoding="utf-8")
            print("published:", p["slug"])


# ---------- 7. build site ----------
def inline(s):
    s = html.escape(s)
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    return re.sub(r"\[(.+?)\]\((https?://[^)\s]+)\)", r'<a href="\2" rel="noopener">\1</a>', s)


def md(text):
    out, items = [], []
    flush = lambda: (out.append("<ul>" + "".join(items) + "</ul>"), items.clear()) if items else None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("- ", "* ")):
            items.append(f"<li>{inline(line[2:])}</li>")
            continue
        flush()
        if line.startswith("### "): out.append(f"<h3>{inline(line[4:])}</h3>")
        elif line.startswith("## "): out.append(f"<h2>{inline(line[3:])}</h2>")
        elif line: out.append(f"<p>{inline(line)}</p>")
    flush()
    return "\n".join(out)


URL = (os.getenv("SITE_URL") or os.getenv("URL") or "https://your-site.netlify.app").rstrip("/")
COLORS = {"technology": ("#2563eb", "#7c3aed", "💻"), "business": ("#059669", "#0d9488", "📈"),
          "entertainment": ("#db2777", "#f59e0b", "🎬"), "sports": ("#ea580c", "#dc2626", "🏆"),
          "lifestyle": ("#7c3aed", "#ec4899", "✨"), "health": ("#0d9488", "#22c55e", "🩺")}

CSS = """:root{--bg:#fff;--fg:#14161a;--mut:#667085;--ac:#2563eb;--line:#e5e7eb;--card:#f8fafc}
[data-theme=dark]{--bg:#0e1116;--fg:#eceef2;--mut:#9aa3b2;--ac:#7aa7ff;--line:#262b34;--card:#151a22}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{display:flex;flex-direction:column;min-height:100vh;margin:0;background:var(--bg);color:var(--fg);font:17px/1.7 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
a{color:inherit;text-decoration:none}.w{width:100%;max-width:1100px;margin:auto;padding:0 20px}main.w{flex:1;margin:0 auto}.sl{display:none}
header{position:sticky;top:0;z-index:9;background:var(--bg);border-bottom:1px solid var(--line)}
.bar{display:flex;align-items:center;gap:18px;height:64px}.logo{font-weight:800;font-size:22px;letter-spacing:-.5px}
.logo b{color:var(--ac)}nav{display:flex;gap:4px;overflow-x:auto;flex:1}
nav a{padding:6px 12px;border-radius:99px;font-size:15px;color:var(--mut);text-transform:capitalize;white-space:nowrap}
nav a:hover{background:var(--card);color:var(--fg)}
.sf input{border:1px solid var(--line);background:var(--card);color:var(--fg);padding:8px 14px;border-radius:99px;width:170px;font:inherit;font-size:14px}
#tg{border:1px solid var(--line);background:var(--card);color:var(--fg);width:38px;height:38px;border-radius:50%;cursor:pointer;font-size:16px}
.hero{padding:36px 0 8px}.hero h1{font-size:clamp(28px,5vw,46px);line-height:1.15;letter-spacing:-1px;margin:0 0 8px}
.hero p{color:var(--mut);margin:0}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:24px;margin:24px 0}
.card{border:1px solid var(--line);border-radius:16px;overflow:hidden;background:var(--card);transition:.2s}
.card:hover{transform:translateY(-3px);box-shadow:0 10px 30px rgba(0,0,0,.12)}
.card.big{display:grid;grid-template-columns:1.1fr 1fr;margin:24px 0}.card.big .th{height:100%;min-height:260px}.card.big h3{font-size:28px}
.th{height:160px;display:flex;align-items:center;justify-content:center}.th span{font-size:54px;filter:drop-shadow(0 4px 8px rgba(0,0,0,.25))}
.cb{padding:18px}.cb h3{margin:6px 0;font-size:20px;line-height:1.3}.cb p{margin:0 0 10px;color:var(--mut);font-size:15px}
.tag{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--ac)}
.meta{font-size:13px;color:var(--mut)}.sec{display:flex;justify-content:space-between;align-items:baseline;margin-top:40px}
.sec h2{margin:0;font-size:24px;text-transform:capitalize}.sec a{color:var(--ac);font-size:14px}
article.post{max-width:720px;margin:36px auto}article.post h1{font-size:clamp(28px,5vw,42px);line-height:1.15;letter-spacing:-1px;margin:8px 0}
.post h2{margin-top:2em;font-size:26px}.post p,.post li{font-size:18px;line-height:1.8}.post a{color:var(--ac);text-decoration:underline}
.crumb{font-size:14px;color:var(--mut)}.share{display:flex;gap:10px;margin:24px 0}
.share a,.share button{border:1px solid var(--line);background:var(--card);color:var(--fg);padding:7px 14px;border-radius:99px;font:inherit;font-size:14px;cursor:pointer}
.note{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px;font-size:14px;color:var(--mut);margin:28px 0}
.card2{padding:16px 0;border-bottom:1px solid var(--line)}#q{width:100%;padding:12px 16px;border:1px solid var(--line);border-radius:12px;background:var(--card);color:var(--fg);font:inherit;margin:16px 0}
footer{border-top:1px solid var(--line);margin-top:60px;padding:40px 0 24px;background:var(--card);color:var(--mut);font-size:14px}footer .fg{display:grid;grid-template-columns:2fr 1fr 1fr;gap:30px}footer p{margin:8px 0;max-width:340px}footer h4{margin:0 0 8px;color:var(--fg);font-size:13px;text-transform:uppercase;letter-spacing:.08em}footer a{display:block;margin:6px 0}footer a:hover{color:var(--ac)}footer .fb{display:flex;justify-content:space-between;border-top:1px solid var(--line);margin-top:28px;padding-top:16px}
@media(max-width:900px){.card.big{grid-template-columns:1fr}.card.big .th{min-height:180px}.sf{display:none}.sl{display:block}footer .fg{grid-template-columns:1fr 1fr}}"""


CSS += """
footer{border-top:1px solid var(--line);margin-top:60px;padding:40px 0 24px;color:var(--mut);font-size:14px;background:var(--card)}
.fg{display:grid;grid-template-columns:2fr 1fr 1fr;gap:32px}footer h4{margin:0 0 10px;color:var(--fg);font-size:15px}
footer .fg a{display:block;padding:3px 0}footer a:hover{color:var(--ac)}footer p{margin:8px 0 0;max-width:340px}
.fb{margin-top:28px;padding-top:18px;border-top:1px solid var(--line)}
.fm{display:grid;gap:12px;max-width:560px}.fm input,.fm textarea{width:100%;padding:12px 14px;border:1px solid var(--line);border-radius:10px;background:var(--card);color:var(--fg);font:inherit}
.fm button{justify-self:start;background:var(--ac);color:#fff;border:0;padding:12px 24px;border-radius:99px;font:inherit;cursor:pointer}
@media(max-width:900px){.fg{grid-template-columns:1fr 1fr}}"""

CSS += ".cv{width:100%;height:170px;object-fit:cover;display:block}.card.big .cv{height:100%;min-height:260px}.hero-img{width:100%;border-radius:16px;margin:18px 0;display:block}"

FAVICON = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">'
           '<stop offset="0" stop-color="#2563eb"/><stop offset="1" stop-color="#7c3aed"/></linearGradient></defs>'
           '<rect width="64" height="64" rx="14" fill="url(#g)"/><path d="M16 17h32v8H36v22h-8V25H16z" fill="#fff"/></svg>')


def icon_png(w, h):
    """Pure-python PNG: blue-purple gradient with a white T (no image libraries needed)."""
    import zlib, struct
    s = min(w, h)
    ox, oy = (w - s) / 2, (h - s) / 2
    rows = []
    for y in range(h):
        row = bytearray(b"\x00")
        v = (y - oy) / s
        for x in range(w):
            u = (x - ox) / s
            if (0.25 <= u <= 0.75 and 0.265 <= v <= 0.39) or (0.4375 <= u <= 0.5625 and 0.39 <= v <= 0.73):
                row += b"\xff\xff\xff"
            else:
                t = (x / w + y / h) / 2
                row += bytes((int(37 + 87 * t), int(99 - 41 * t), int(235 + 2 * t)))
        rows.append(bytes(row))
    def chunk(k, d):
        c = struct.pack(">I", len(d)) + k + d
        return c + struct.pack(">I", zlib.crc32(k + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def footer_html():
    secs = "".join(f'<a href="/{c}/">{c.title()}</a>' for c in CATEGORIES)
    pages = [("about", "About us"), ("contact", "Contact"), ("privacy", "Privacy Policy"),
             ("terms", "Terms &amp; Conditions"), ("disclaimer", "Disclaimer")]
    co = "".join(f'<a href="/{u}.html">{n}</a>' for u, n in pages) + '<a href="/rss.xml">RSS feed</a>'
    return (f'<footer><div class="w fg"><div><div class="logo">{SITE_NAME}<b>.</b></div>'
            f'<p>Sourced, plain-English explainers on what the world is searching for. Researched with AI assistance and reviewed before publishing.</p></div>'
            f'<div><h4>Sections</h4>{secs}</div><div><h4>Company</h4>{co}</div></div>'
            f'<div class="w fb">© {datetime.date.today().year} {SITE_NAME}. All rights reserved. Published by {html.escape(OWNER)}. '
            f'Health and finance articles are general information, not professional advice.</div></footer>')


def rt(p):
    return max(1, len(p["body"].split()) // 200)


def page(title, body, desc="", path="/", kind="website", ld="", noindex=False):
    t = html.escape(title if title == SITE_NAME else f"{title} | {SITE_NAME}")
    d = html.escape(desc or f"{SITE_NAME}: clear, sourced explainers on what the world is searching for.")
    nav = "".join(f'<a href="/{c}/">{c}</a>' for c in CATEGORIES) + '<a class="sl" href="/search.html">Search</a>'
    robots = "noindex,follow" if noindex else "index,follow,max-image-preview:large"
    img = f"{URL}/og-image.png"
    head = (f'<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{t}</title>'
            f'<meta name="description" content="{d}"><meta name="robots" content="{robots}"><meta name="theme-color" content="#2563eb">'
            f'<link rel="canonical" href="{URL}{path}">'
            f'<meta property="og:site_name" content="{html.escape(SITE_NAME)}"><meta property="og:title" content="{t}">'
            f'<meta property="og:description" content="{d}"><meta property="og:type" content="{kind}">'
            f'<meta property="og:url" content="{URL}{path}"><meta property="og:image" content="{img}">'
            f'<meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="{t}">'
            f'<meta name="twitter:description" content="{d}"><meta name="twitter:image" content="{img}">'
            f'<link rel="icon" href="/favicon.svg" type="image/svg+xml"><link rel="icon" href="/favicon-32.png" sizes="32x32" type="image/png">'
            f'<link rel="apple-touch-icon" href="/apple-touch-icon.png"><link rel="manifest" href="/site.webmanifest">'
            f'<link rel="alternate" type="application/rss+xml" title="{html.escape(SITE_NAME)}" href="/rss.xml">'
            f'<link rel="stylesheet" href="/style.css">')
    theme = "<script>try{document.documentElement.dataset.theme=localStorage.theme||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light')}catch(e){}</script>"
    toggle = ("<button id=tg aria-label=\"Toggle dark mode\" onclick=\"var d=document.documentElement;d.dataset.theme=d.dataset.theme=='dark'?'light':'dark';"
              "try{localStorage.theme=d.dataset.theme}catch(e){}\">◐</button>")
    return (f'<!doctype html><html lang="en"><head>{head}{theme}{ld}</head><body id="top"><header><div class="w bar">'
            f'<a class="logo" href="/">{SITE_NAME}<b>.</b></a><nav>{nav}</nav>'
            f'<form class="sf" action="/search.html"><input name="q" placeholder="Search…" aria-label="Search"></form>{toggle}'
            f'</div></header><main class="w">{body}</main>{footer_html()}</body></html>')


def cover_svg(p):
    import random
    r = random.Random(p["slug"])
    a, b, e = COLORS[p["category"]]
    shapes = "".join(f'<circle cx="{r.randint(0, 1200)}" cy="{r.randint(0, 630)}" r="{r.randint(60, 260)}" fill="#fff" opacity="{r.choice([.07, .11, .15])}"/>' for _ in range(7))
    x2, y2 = r.choice([(1, 1), (1, 0), (0, 1)])
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 630"><defs><linearGradient id="g" x1="0" y1="0" x2="{x2}" y2="{y2}">'
            f'<stop offset="0" stop-color="{a}"/><stop offset="1" stop-color="{b}"/></linearGradient></defs>'
            f'<rect width="1200" height="630" fill="url(#g)"/>{shapes}<text x="600" y="400" font-size="220" text-anchor="middle">{e}</text></svg>')


def card(p, big=False):
    u = f'/{p["category"]}/{p["slug"]}.html'
    cls = "card big" if big else "card"
    return (f'<article class="{cls}"><a href="{u}"><img class="cv" src="/covers/{p["slug"]}.svg" alt="{html.escape(p["title"])}" loading="lazy"></a>'
            f'<div class="cb"><a class="tag" href="/{p["category"]}/">{p["category"]}</a><h3><a href="{u}">{html.escape(p["title"])}</a></h3>'
            f'<p>{html.escape(p["description"])}</p><div class="meta">{p["date"]} · {rt(p)} min read</div></div></article>')


def grid(ps):
    if not ps:
        return '<p class="meta">Fresh articles are on the way.</p>'
    return '<div class="grid">' + "".join(card(p) for p in ps) + "</div>"


def write(path, content):
    f = SITE / path
    f.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        f.write_bytes(content)
        return
    base = urllib.parse.urlparse(URL).path.rstrip("/")
    if base and str(path).endswith(".html"):
        content = re.sub(r'(href|src|action)="/(?!/)', rf'\1="{base}/', content).replace("fetch('/search.json')", f"fetch('{base}/search.json')")
    f.write_text(content, encoding="utf-8")


def info_page(slug, title, desc, inner):
    today = datetime.date.today().strftime("%B %d, %Y")
    body = f'<article class="post"><h1>{title}</h1><p class="meta">Last updated: {today}</p>{inner}</article>'
    write(f"{slug}.html", page(title, body, desc, f"/{slug}.html"))


def build_pages():
    n = html.escape(SITE_NAME)
    info_page("about", f"About {n}", f"About {SITE_NAME}: who we are, how we research and write, and how we handle corrections.", f"""
<p>{n} is published and edited by <strong>{html.escape(OWNER)}</strong>. It explains what people are searching for right now, in plain English. We cover technology, health, business, entertainment, sports and lifestyle, and our goal is simple: give you a clear, useful answer in a few minutes, with sources you can check.</p>
<h2>How we make articles</h2>
<p>Every article starts from a real trending topic. We read several public sources, write a clear explainer, and run a second review that checks claims against the sources. Articles that do not meet our quality bar are discarded, not published.</p>
<h2>Our use of AI</h2>
<p>We use AI tools to help research and draft articles, and we say so openly. AI assistance does not replace editorial standards: we do not invent quotes or statistics, and we list sources on articles wherever we use them.</p>
<h2>Health and money topics</h2>
<p>Health and finance articles are general education only. They are not medical or investment advice. Please read our <a href="/disclaimer.html">Disclaimer</a>.</p>
<h2>Corrections</h2>
<p>If you spot a mistake, <a href="/contact.html">tell us</a> and we will review it and correct it quickly.</p>""")
    info_page("privacy", "Privacy Policy", f"How {SITE_NAME} handles your information, cookies and third-party services.", f"""
<p>Your privacy matters. {n}, published by {html.escape(OWNER)}, explains in this policy what information it collects and how it is used.</p>
<h2>Information we collect</h2>
<p>You do not need an account to read {n}. If you send a message through our <a href="/contact.html">contact form</a>, we receive the name, email address and message you type, and we use them only to reply to you.</p>
<h2>Log data</h2>
<p>Like most websites, our hosting provider may automatically record technical data such as IP address, browser type and pages visited, for security and performance.</p>
<h2>Cookies and local storage</h2>
<p>We store one preference in your browser (light or dark mode). It never leaves your device. If we add analytics or advertising in future, we will update this policy first. Where ads are shown, third-party vendors, including Google, may use cookies to serve ads based on your visits to this and other sites. You can opt out of personalised advertising in your Google Ads Settings.</p>
<h2>External links</h2>
<p>Our articles link to other websites. We are not responsible for their content or privacy practices.</p>
<h2>Children</h2>
<p>{n} is not directed to children under 13, and we do not knowingly collect their personal information.</p>
<h2>Your choices</h2>
<p>You can ask us to delete any message you sent us by writing through the <a href="/contact.html">contact page</a>.</p>
<h2>Changes</h2>
<p>We may update this policy and will change the date above when we do.</p>""")
    info_page("terms", "Terms &amp; Conditions", f"The terms for using {SITE_NAME}.", f"""
<p>By using {n} you agree to these terms. If you do not agree, please do not use the site.</p>
<h2>Information only</h2>
<p>Our content is provided for general information and education. It is not professional medical, legal, financial or investment advice. See our <a href="/disclaimer.html">Disclaimer</a>.</p>
<h2>Accuracy</h2>
<p>Articles are researched and drafted with AI assistance and reviewed before publishing. We work hard to be accurate, but we cannot guarantee that everything is complete, current or error-free. Please check important facts with official sources.</p>
<h2>Intellectual property</h2>
<p>The articles, design and branding of {n} belong to {html.escape(OWNER)}, the publisher of the site. You may share links and short excerpts with clear credit and a link back. You may not copy whole articles or republish them without permission.</p>
<h2>Acceptable use</h2>
<p>Do not misuse the site: no unlawful activity, no attempts to disrupt or hack it, and no automated scraping that harms performance.</p>
<h2>Third-party links</h2>
<p>Links to other sites are provided for convenience. We do not control or endorse their content.</p>
<h2>No warranty and limitation of liability</h2>
<p>The site is provided "as is" without warranties of any kind. To the fullest extent allowed by law, {n} is not liable for any loss arising from your use of the site or reliance on its content.</p>
<h2>Changes and governing law</h2>
<p>We may update these terms from time to time. These terms are governed by the laws of India.</p>
<h2>Contact</h2>
<p>Questions? Visit our <a href="/contact.html">contact page</a>.</p>""")
    info_page("disclaimer", "Disclaimer", f"{SITE_NAME} provides general information only. Read our health, finance and accuracy disclaimer.", f"""
<h2>Not professional advice</h2>
<p>Everything on {n} is general information. Health articles are not medical advice and do not replace a doctor. Business and money articles are not financial or investment advice. Always consult a qualified professional about your own situation.</p>
<h2>AI-assisted content</h2>
<p>Articles are drafted with AI assistance and checked before publishing. Mistakes can still happen. If you notice one, please <a href="/contact.html">tell us</a>.</p>
<h2>External sources</h2>
<p>We link to third-party sources and are not responsible for their content.</p>""")
    form = ('<form class="fm" name="contact" method="POST" action="/thanks.html" data-netlify="true" netlify-honeypot="bot-field">'
            '<input type="hidden" name="form-name" value="contact"><p hidden><label>Leave empty: <input name="bot-field"></label></p>'
            '<input name="name" placeholder="Your name" required><input type="email" name="email" placeholder="Your email" required>'
            '<textarea name="message" rows="6" placeholder="How can we help? Corrections, questions, feedback..." required></textarea>'
            '<button type="submit">Send message</button></form>')
    info_page("contact", "Contact us", f"Contact {SITE_NAME} for corrections, questions or feedback.",
              f"<p>Found an error, have a question or want to give feedback? Send us a message and we will reply as soon as we can.</p>{form}")
    write("thanks.html", page("Thank you", '<section class="hero"><h1>Thank you!</h1><p>Your message was sent. We will get back to you soon. <a href="/">Back to home</a></p></section>', path="/thanks.html", noindex=True))


def build():
    live = [p for p in all_posts() if p["status"] == "published"]
    write("style.css", CSS)
    for p in live:
        write(f'covers/{p["slug"]}.svg', cover_svg(p))
    write("favicon.svg", FAVICON)
    for name, (w, h) in {"favicon-32.png": (32, 32), "apple-touch-icon.png": (180, 180), "icon-192.png": (192, 192),
                         "icon-512.png": (512, 512), "og-image.png": (1200, 630)}.items():
        write(name, icon_png(w, h))
    write("site.webmanifest", json.dumps({"name": SITE_NAME, "short_name": SITE_NAME, "start_url": "/", "display": "standalone",
          "background_color": "#ffffff", "theme_color": "#2563eb",
          "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"}, {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]}))
    write("_headers", "/*\n  X-Content-Type-Options: nosniff\n  X-Frame-Options: SAMEORIGIN\n  Referrer-Policy: strict-origin-when-cross-origin\n/*.png\n  Cache-Control: public, max-age=604800\n")
    # home
    hero = ('<section class="hero"><h1>What the world is searching for, explained.</h1>'
            '<p>Sourced, plain-English explainers on today\'s trending topics.</p></section>')
    home = hero + (card(live[0], True) if live else "") + f'<div class="sec"><h2>Latest</h2></div>{grid(live[1:7])}'
    for c in CATEGORIES:
        ps = [p for p in live if p["category"] == c][:3]
        if ps:
            home += f'<div class="sec"><h2>{c}</h2><a href="/{c}/">View all →</a></div>{grid(ps)}'
    site_ld = json.dumps({"@context": "https://schema.org", "@graph": [
        {"@type": "Organization", "name": SITE_NAME, "url": URL, "logo": f"{URL}/icon-512.png"},
        {"@type": "WebSite", "name": SITE_NAME, "url": URL, "potentialAction": {"@type": "SearchAction",
         "target": f"{URL}/search.html?q={{search_term_string}}", "query-input": "required name=search_term_string"}}]})
    write("index.html", page(SITE_NAME, home, ld=f'<script type="application/ld+json">{site_ld}</script>'))
    for c in CATEGORIES:
        ps = [p for p in live if p["category"] == c]
        write(f"{c}/index.html", page(c.title(), f'<section class="hero"><h1>{c.title()}</h1><p>{CATEGORIES[c]}</p></section>{grid(ps)}',
                                      f"{c.title()} articles: {CATEGORIES[c]}", f"/{c}/"))
    # articles
    for p in live:
        rel = [r for r in live if r["category"] == p["category"] and r is not p][:3]
        src = "".join(f'<li><a href="{s["url"]}" rel="noopener nofollow">{html.escape(s["title"])}</a></li>' for s in p["sources"])
        u = f'/{p["category"]}/{p["slug"]}.html'
        art = {"@context": "https://schema.org", "@type": "Article", "headline": p["title"], "description": p["description"],
               "datePublished": p["date"], "dateModified": p["date"], "image": [f"{URL}/og-image.png"],
               "author": {"@type": "Person", "name": OWNER}, "mainEntityOfPage": URL + u,
               "publisher": {"@type": "Organization", "name": SITE_NAME, "logo": {"@type": "ImageObject", "url": f"{URL}/icon-512.png"}}}
        crumbs = {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Home", "item": URL + "/"},
            {"@type": "ListItem", "position": 2, "name": p["category"].title(), "item": f'{URL}/{p["category"]}/'},
            {"@type": "ListItem", "position": 3, "name": p["title"], "item": URL + u}]}
        ld = "".join(f'<script type="application/ld+json">{json.dumps(x)}</script>' for x in (art, crumbs))
        share = (f'<div class="share"><a href="https://wa.me/?text={URL + u}">WhatsApp</a>'
                 f'<a href="https://twitter.com/intent/tweet?url={URL + u}">Post on X</a>'
                 "<button onclick=\"navigator.clipboard.writeText(location.href);this.textContent='Copied!'\">Copy link</button></div>")
        note = "This article was researched and drafted with AI assistance and reviewed before publishing. Spot an error? <a href=\"/contact.html\">Tell us</a>."
        if p["category"] == "health":
            note += " This is general information, not medical advice; talk to a doctor about your own health."
        body = (f'<article class="post"><div class="crumb"><a href="/">Home</a> / <a href="/{p["category"]}/">{p["category"].title()}</a></div>'
                f'<h1>{html.escape(p["title"])}</h1><div class="meta">By {html.escape(OWNER)} · {p["date"]} · {rt(p)} min read</div><img class="hero-img" src="/covers/{p["slug"]}.svg" alt="{html.escape(p["title"])}">{share}{md(p["body"])}'
                + (f"<h2>Sources</h2><ul>{src}</ul>" if src else "") + f'<div class="note">{note}</div></article>'
                + (f'<div class="sec"><h2>Related</h2></div>{grid(rel)}' if rel else ""))
        write(u.lstrip("/"), page(p["title"], body, p["description"], u, "article", ld))
    # search, pages, feeds
    idx = [{"t": html.escape(p["title"]), "d": html.escape(p["description"]), "g": " ".join(p["tags"]), "c": p["category"],
            "dt": p["date"], "u": f'/{p["category"]}/{p["slug"]}.html'} for p in live]
    write("search.json", json.dumps(idx))
    js = ("<script>fetch('/search.json').then(r=>r.json()).then(d=>{const q=document.getElementById('q'),r=document.getElementById('r');"
          "function go(){const t=q.value.toLowerCase().trim();r.innerHTML=d.filter(p=>!t||(p.t+' '+p.d+' '+p.g).toLowerCase().includes(t))"
          ".map(p=>`<div class=card2><a href=\"${p.u}\"><b>${p.t}</b></a><div class=meta>${p.c} · ${p.dt}</div><p>${p.d}</p></div>`).join('')||'<p>No results.</p>'}"
          "q.value=new URLSearchParams(location.search).get('q')||'';q.oninput=go;go()})</script>")
    write("search.html", page("Search", f'<section class="hero"><h1>Search</h1></section><input id=q placeholder="Search articles…" autofocus><div id=r></div>{js}',
                              path="/search.html", noindex=True))
    build_pages()
    write("404.html", page("Page not found", '<section class="hero"><h1>Page not found</h1><p><a href="/">Back to the home page</a></p></section>', noindex=True))
    write("robots.txt", f"User-agent: *\nAllow: /\nSitemap: {URL}/sitemap.xml\n")
    today = datetime.date.today().isoformat()
    urls = [("/", today)] + [(f"/{x}.html", today) for x in ("about", "contact", "privacy", "terms", "disclaimer")] \
        + [(f"/{c}/", today) for c in CATEGORIES] + [(f'/{p["category"]}/{p["slug"]}.html', p["date"]) for p in live]
    write("sitemap.xml", '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
          + "".join(f"<url><loc>{URL}{u}</loc><lastmod>{d}</lastmod></url>" for u, d in urls) + "</urlset>")
    items = "".join(f'<item><title>{html.escape(p["title"])}</title><link>{URL}/{p["category"]}/{p["slug"]}.html</link>'
                    f'<description>{html.escape(p["description"])}</description></item>' for p in live[:20])
    write("rss.xml", f'<?xml version="1.0"?><rss version="2.0"><channel><title>{html.escape(SITE_NAME)}</title><link>{URL}</link>'
                     f'<description>Latest articles</description>{items}</channel></rss>')
    print(f"built {len(live)} posts -> {SITE}/")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "run": run()
    elif cmd == "list": [print(f'{p["status"]:9} {p["category"]:14} {p["score"]}/10  {p["slug"]}') for p in all_posts()]
    elif cmd == "approve" and len(sys.argv) > 2: approve(sys.argv[2])
    elif cmd == "check": check()
    elif cmd == "build": build()
    elif cmd == "auto": run(); approve("--good"); build()   # for scheduled/Netlify builds
    else: print(__doc__)
