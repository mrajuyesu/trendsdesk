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
import os, sys, re, json, html, datetime, urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).parent
POSTS, SITE = ROOT / "posts", ROOT / "site"
SITE_NAME = os.getenv("SITE_NAME", "Trend Desk")
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


def gem(prompt, system, search):
    body = {"contents": [{"parts": [{"text": prompt}]}]}
    if system: body["systemInstruction"] = {"parts": [{"text": system}]}
    if search: body["tools"] = [{"google_search": {}}]
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{os.getenv('GEMINI_MODEL', 'gemini-2.5-flash')}:generateContent",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"]})
    r = json.load(urllib.request.urlopen(req, timeout=180))
    return "".join(p.get("text", "") for p in r["candidates"][0]["content"]["parts"])


def llm(prompt, system="", search=False, max_tokens=4000):
    if os.getenv("GEMINI_API_KEY"):          # free option
        return gem(prompt, system, search)
    import anthropic
    kw = {}
    if search:
        kw["tools"] = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}]
    r = anthropic.Anthropic().messages.create(
        model=MODEL, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": prompt}], **kw)
    return "".join(b.text for b in r.content if b.type == "text")


def jload(text):
    return json.loads(re.search(r"\{.*\}|\[.*\]", text, re.S).group(0))


# ---------- 1. trends ----------
def fetch_trends():
    url = f"https://trends.google.com/trending/rss?geo={GEO}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    root = ET.fromstring(urllib.request.urlopen(req, timeout=20).read())
    return [i.findtext("title") for i in root.iter("item") if i.findtext("title")]


# ---------- 2. pick + categorize ----------
def categorize(trends):
    prompt = (f"Categories: {json.dumps(CATEGORIES)}\nAvoid: {AVOID}\n"
              f"Trending searches: {json.dumps(trends)}\n"
              f"Pick up to {MAX_POSTS} that fit a category and can support a useful, factual explainer. "
              'Return JSON list: [{"topic":"","category":"","angle":"one-line original angle"}]. JSON only.')
    return jload(llm(prompt))[:MAX_POSTS]


# ---------- 3-5. research, write, quality gate ----------
def write_post(item):
    draft = llm(
        f"Topic: {item['topic']}\nCategory: {item['category']}\nAngle: {item['angle']}\n"
        "Search the web, read several credible sources, then write the article. Return JSON only: "
        '{"title":"","description":"max 155 chars","tags":["",""],"sources":[{"title":"","url":""}],"body":"markdown"}',
        system=STYLE, search=True, max_tokens=6000)
    post = jload(draft)
    review = jload(llm(
        "Review this draft as a strict editor. Score 1-10 for accuracy (claims supported by cited sources), "
        "originality, and usefulness. Return JSON only: "
        '{"score":0,"problems":[""]}\n\n' + json.dumps(post)))
    post["score"] = review["score"]
    return post if review["score"] >= MIN_SCORE else None


# ---------- 6. save by category ----------
def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def save(post, category):
    d = POSTS / category
    d.mkdir(parents=True, exist_ok=True)
    date = datetime.date.today().isoformat()
    slug = f"{date}-{slugify(post['title'])}"
    meta = {"title": post["title"], "description": post["description"], "category": category,
            "date": date, "tags": post["tags"], "sources": post["sources"],
            "score": post["score"], "status": "draft"}
    text = "".join(f"{k}: {json.dumps(v, ensure_ascii=False)}\n" for k, v in meta.items())
    (d / f"{slug}.md").write_text(f"---\n{text}---\n{post['body']}\n", encoding="utf-8")
    return slug


def run():
    seen = {p.stem for p in POSTS.rglob("*.md")}
    done = " ".join(seen)
    trends = [t for t in fetch_trends() if slugify(t) not in done]
    for item in categorize(trends):
        if item["category"] not in CATEGORIES:
            continue
        try:
            post = write_post(item)
        except Exception as e:
            print(f"! failed: {item['topic']} ({e})")
            continue
        print(("saved draft: " + save(post, item["category"])) if post else f"discarded (low quality): {item['topic']}")


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


URL = os.getenv("SITE_URL", "https://your-site.netlify.app").rstrip("/")
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


def footer_html():
    cats = "".join(f'<a href="/{c}/">{c.title()}</a>' for c in CATEGORIES)
    return (f'<footer><div class="w fg"><div><div class="logo">{SITE_NAME}<b>.</b></div><p>Sourced, plain-English explainers on what the world is searching for. Researched with AI assistance and reviewed before publishing.</p></div>'
            f'<div><h4>Categories</h4>{cats}</div><div><h4>Site</h4><a href="/about.html">About</a><a href="/search.html">Search</a>'
            f'<a href="/rss.xml">RSS feed</a><a href="/privacy.html">Privacy</a><a href="/disclaimer.html">Disclaimer</a></div></div>'
            f'<div class="w fb"><span>© {datetime.date.today().year} {SITE_NAME}. All rights reserved.</span><a href="#top">Back to top ↑</a></div></footer>')


def rt(p):
    return max(1, len(p["body"].split()) // 200)


def page(title, body, desc="", path="/", kind="website", ld=""):
    t = html.escape(title if title == SITE_NAME else f"{title} | {SITE_NAME}")
    d = html.escape(desc or f"{SITE_NAME}: clear, sourced explainers on what the world is searching for.")
    nav = "".join(f'<a href="/{c}/">{c}</a>' for c in CATEGORIES) + '<a class="sl" href="/search.html">Search</a>'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1"><title>{t}</title>'
            f'<meta name="description" content="{d}"><link rel="canonical" href="{URL}{path}">'
            f'<meta property="og:title" content="{t}"><meta property="og:description" content="{d}">'
            f'<meta property="og:type" content="{kind}"><meta name="twitter:card" content="summary">'
            f'<link rel="alternate" type="application/rss+xml" href="/rss.xml">'
            f'<link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>📰</text></svg>">'
            f'<link rel="stylesheet" href="/style.css">'
            "<script>try{document.documentElement.dataset.theme=localStorage.theme||(matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light')}catch(e){}</script>"
            f'{ld}</head><body id="top"><header><div class="w bar"><a class="logo" href="/">{SITE_NAME}<b>.</b></a><nav>{nav}</nav>'
            f'<form class="sf" action="/search.html"><input name="q" placeholder="Search…" aria-label="Search"></form>'
            "<button id=tg aria-label=\"Toggle dark mode\" onclick=\"var d=document.documentElement;d.dataset.theme=d.dataset.theme=='dark'?'light':'dark';try{localStorage.theme=d.dataset.theme}catch(e){}\">◐</button>"
            f'</div></header><main class="w">{body}</main>{footer_html()}</body></html>')


def card(p, big=False):
    a, b, e = COLORS[p["category"]]
    u = f'/{p["category"]}/{p["slug"]}.html'
    return (f'<article class="card{" big" if big else ""}"><a href="{u}"><div class="th" style="background:linear-gradient(135deg,{a},{b})"><span>{e}</span></div></a>'
            f'<div class="cb"><a class="tag" href="/{p["category"]}/">{p["category"]}</a><h3><a href="{u}">{html.escape(p["title"])}</a></h3>'
            f'<p>{html.escape(p["description"])}</p><div class="meta">{p["date"]} · {rt(p)} min read</div></div></article>')


def grid(ps):
    return f'<div class="grid">{"".join(card(p) for p in ps)}</div>' if ps else '<p class="meta">Fresh articles are on the way.</p>'


def write(path, text):
    f = SITE / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


def build():
    live = [p for p in all_posts() if p["status"] == "published"]
    write("style.css", CSS)
    hero = f'<section class="hero"><h1>What the world is searching for, explained.</h1><p>Sourced, plain-English explainers on today\'s trending topics.</p></section>'
    home = hero + (card(live[0], True) if live else "") + f'<div class="sec"><h2>Latest</h2></div>{grid(live[1:7])}'
    for c in CATEGORIES:
        ps = ([p for p in live if p["category"] == c and p not in live[:7]] or [p for p in live if p["category"] == c])[:3]
        if ps:
            home += f'<div class="sec"><h2>{c}</h2><a href="/{c}/">View all →</a></div>{grid(ps)}'
    write("index.html", page(SITE_NAME, home))
    for c in CATEGORIES:
        ps = [p for p in live if p["category"] == c]
        write(f"{c}/index.html", page(c.title(), f'<section class="hero"><h1>{c.title()}</h1><p>{CATEGORIES[c]}</p></section>{grid(ps)}',
                                      CATEGORIES[c], f"/{c}/"))
    for p in live:
        rel = [r for r in live if r["category"] == p["category"] and r is not p][:3]
        src = "".join(f'<li><a href="{s["url"]}" rel="noopener nofollow">{html.escape(s["title"])}</a></li>' for s in p["sources"])
        u = f'/{p["category"]}/{p["slug"]}.html'
        ld = ('<script type="application/ld+json">' + json.dumps({"@context": "https://schema.org", "@type": "Article",
              "headline": p["title"], "description": p["description"], "datePublished": p["date"],
              "mainEntityOfPage": URL + u, "publisher": {"@type": "Organization", "name": SITE_NAME}}) + "</script>")
        share = (f'<div class="share"><a href="https://wa.me/?text={URL+u}">WhatsApp</a>'
                 f'<a href="https://twitter.com/intent/tweet?url={URL+u}">Post on X</a>'
                 f'<button onclick="navigator.clipboard.writeText(location.href);this.textContent=\'Copied!\'">Copy link</button></div>')
        body = (f'<article class="post"><div class="crumb"><a href="/">Home</a> / <a href="/{p["category"]}/">{p["category"].title()}</a></div>'
                f'<h1>{html.escape(p["title"])}</h1><div class="meta">{p["date"]} · {rt(p)} min read</div>{share}{md(p["body"])}'
                + (f'<h2>Sources</h2><ul>{src}</ul>' if src else "")
                + ('<div class="note"><b>Health information only.</b> This is general information, not medical advice. Speak to a doctor or qualified professional about your own situation.</div>' if p["category"] == "health" else "")
                + '<div class="note">This article was researched and drafted with AI assistance and reviewed before publishing. '
                  'Spot an error? Tell us and we will fix it.</div></article>'
                + (f'<div class="sec"><h2>Related</h2></div>{grid(rel)}' if rel else ""))
        write(u.lstrip("/"), page(p["title"], body, p["description"], u, "article", ld))
    idx = [{"t": html.escape(p["title"]), "d": html.escape(p["description"]), "g": " ".join(p["tags"]), "c": p["category"],
            "dt": p["date"], "u": f'/{p["category"]}/{p["slug"]}.html'} for p in live]
    write("search.json", json.dumps(idx))
    js = ("<script>fetch('/search.json').then(r=>r.json()).then(d=>{const q=document.getElementById('q'),r=document.getElementById('r');"
          "function go(){const t=q.value.toLowerCase().trim();r.innerHTML=d.filter(p=>!t||(p.t+' '+p.d+' '+p.g).toLowerCase().includes(t))"
          ".map(p=>`<div class=card2><a href=\"${p.u}\"><b>${p.t}</b></a><div class=meta>${p.c} · ${p.dt}</div><p>${p.d}</p></div>`).join('')||'<p>No results.</p>'}"
          "q.value=new URLSearchParams(location.search).get('q')||'';q.oninput=go;go()})</script>")
    write("search.html", page("Search", f'<section class="hero"><h1>Search</h1></section><input id=q placeholder="Search articles…" autofocus><div id=r></div>{js}', path="/search.html"))
    write("about.html", page("About", f'<article class="post"><h1>About {SITE_NAME}</h1><p>{SITE_NAME} explains what people are searching for right now across technology, health, business, entertainment, sports and lifestyle.</p><h2>How we work</h2><p>Every article is researched from public sources, drafted with AI assistance, checked by a second review pass, and approved before publishing. Sources are listed on each article.</p><h2>Corrections</h2><p>If you find a mistake, please contact us and we will correct it promptly.</p></article>', path="/about.html"))
    write("privacy.html", page("Privacy Policy", f'<article class="post"><h1>Privacy Policy</h1><p>{SITE_NAME} does not require accounts and does not collect personal data directly. Your theme preference is stored only in your own browser. If advertising or analytics are added later, this page will be updated to describe them.</p></article>', path="/privacy.html"))
    write("disclaimer.html", page("Disclaimer", f'<article class="post"><h1>Disclaimer</h1><p>{SITE_NAME} publishes general information for education. It is not medical, legal, financial or other professional advice, and it does not replace a qualified professional.</p><p>Articles are drafted with AI assistance and reviewed, but errors can happen. Please check important facts with official sources and tell us about mistakes so we can correct them.</p><p>Health articles are for general awareness only. If you feel unwell or are worried about your health, contact a doctor or local emergency services.</p></article>', path="/disclaimer.html"))
    write("404.html", page("Page not found", '<section class="hero"><h1>Page not found</h1><p><a href="/">Back to the home page</a></p></section>'))
    write("robots.txt", f"User-agent: *\nAllow: /\nSitemap: {URL}/sitemap.xml\n")
    urls = ["/", "/about.html", "/disclaimer.html", "/privacy.html"] + [f"/{c}/" for c in CATEGORIES] + [f'/{p["category"]}/{p["slug"]}.html' for p in live]
    write("sitemap.xml", '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
          + "".join(f"<url><loc>{URL}{u}</loc></url>" for u in urls) + "</urlset>")
    items = "".join(f'<item><title>{html.escape(p["title"])}</title><link>{URL}/{p["category"]}/{p["slug"]}.html</link>'
                    f'<description>{html.escape(p["description"])}</description></item>' for p in live[:20])
    write("rss.xml", f'<?xml version="1.0"?><rss version="2.0"><channel><title>{SITE_NAME}</title><link>{URL}</link><description>Latest articles</description>{items}</channel></rss>')
    print(f"built {len(live)} posts -> {SITE}/  (preview: cd site && python -m http.server)")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "run": run()
    elif cmd == "list": [print(f'{p["status"]:9} {p["category"]:14} {p["score"]}/10  {p["slug"]}') for p in all_posts()]
    elif cmd == "approve" and len(sys.argv) > 2: approve(sys.argv[2])
    elif cmd == "build": build()
    elif cmd == "auto": run(); approve("--good"); build()   # for scheduled/Netlify builds
    else: print(__doc__)
