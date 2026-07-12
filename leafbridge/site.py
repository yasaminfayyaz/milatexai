"""The public marketing site served at milatexai.com/.

Design goals:
* One source of truth for copy: a per-language content dict (same shape for every
  language) produced by the site-content workflow and stored in
  ``site_content.json`` next to this module. If that file is missing we fall back
  to a small built-in English dict so the site still renders.
* SEO-friendly: the default language (English) is server-rendered into the HTML,
  so crawlers and no-JS visitors see real content.
* Instant language switching with no reload: every translatable node carries a
  ``data-i18n="dot.path"`` attribute; the full set of languages is embedded as a
  JSON blob and a tiny script swaps text by path (and flips to RTL for Arabic).

No external assets — all CSS/JS is inline, so it works behind the same strict
single-origin setup as the rest of the app.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

_CONTENT_PATH = Path(__file__).with_name("site_content.json")

# Endonyms for the language switcher (label shown in each language's own script).
LANG_NAMES = {
    "en": "English", "zh": "中文", "fr": "Français", "es": "Español",
    "de": "Deutsch", "ja": "日本語", "pt": "Português", "ar": "العربية",
    "hi": "हिन्दी", "ko": "한국어", "ru": "Русский", "it": "Italiano",
}
RTL_LANGS = {"ar"}

# --- content loading -------------------------------------------------------

_FALLBACK_EN = {
    "brand_tagline": "Edit your Overleaf papers by talking to your AI.",
    "nav": {"features": "Features", "how": "How it works", "pricing": "Pricing",
            "faq": "FAQ", "privacy": "Privacy", "terms": "Terms",
            "get_started": "Get started", "manage_subscription": "Manage subscription"},
    "hero": {"badge": "Works with Claude & ChatGPT",
             "title": "Your AI can now edit your real Overleaf projects.",
             "subtitle": "MiLatexAI connects Claude or ChatGPT to your Overleaf LaTeX projects over Git. Ask in plain language; changes go live in Overleaf.",
             "cta_primary": "Get started", "cta_secondary": "See how it works",
             "note": "Your Overleaf token is entered on a secure web page — never in the chat."},
    "trust": "Encrypted tokens · Open-source (AGPL) · Your documents are never stored.",
    "features": [
        {"title": "Edit by conversation", "desc": "Ask your AI to rewrite a section, fix a table, or add a figure — it edits the actual .tex and pushes to Overleaf."},
        {"title": "Real Git commits", "desc": "Every change is a commit you can see and revert in your Overleaf history."},
        {"title": "Claude and ChatGPT", "desc": "Add one connector; use whichever assistant you prefer."},
        {"title": "Token stays out of chat", "desc": "You paste your Overleaf Git token into a secure web form; it is encrypted and never appears in the transcript."},
        {"title": "Reads are free", "desc": "Reading files, sections and history is unlimited. Only writes count toward your plan."},
        {"title": "Compile check", "desc": "Ask it to verify the project still builds before you trust an edit."},
    ],
    "how": {"title": "Three steps", "subtitle": "You're editing in minutes.",
            "steps": [
                {"title": "Add the connector", "desc": "Add MiLatexAI in Claude or ChatGPT and sign in."},
                {"title": "Connect a project", "desc": "Run start_connect, open the secure link, and paste your Overleaf Git token — it's encrypted, never shown in chat."},
                {"title": "Ask for edits", "desc": "Tell your AI what to change. It edits the LaTeX and pushes it live to Overleaf."},
            ]},
    "platforms": {"title": "One connector, two assistants", "subtitle": "MiLatexAI speaks the Model Context Protocol (MCP).",
                  "claude": "Add as a custom connector in Claude.",
                  "chatgpt": "Add as a custom connector in ChatGPT."},
    "pricing": {"title": "Simple pricing", "subtitle": "Start free. Upgrade when you need more.",
                "free": {"name": "Free", "price": "$0", "period": "/month",
                         "features": ["1 connected project", "25 write-commits / month", "Unlimited reads & compile checks"],
                         "cta": "Get started"},
                "pro": {"name": "Pro", "price": "Coming soon", "period": "", "badge": "Most popular",
                        "features": ["Unlimited projects", "Unlimited commits", "Priority support"],
                        "cta": "Get notified"},
                "note": "Reads are always free on every plan."},
    "faq": {"title": "Frequently asked questions", "items": [
        {"q": "Is my Overleaf token safe?", "a": "You enter it on a secure web page, not in the chat. It's encrypted at rest and only used to sync your connected projects over Git."},
        {"q": "Do you see my documents?", "a": "We don't store your document contents. Edits flow through Overleaf's Git integration; we keep only your account email, your encrypted token, and a commit counter."},
        {"q": "Which assistants work?", "a": "Anthropic Claude and OpenAI ChatGPT, via the Model Context Protocol."},
        {"q": "What happens at the free limit?", "a": "Reads stay free and unlimited. New write-commits resume next month, or you can upgrade to Pro."},
        {"q": "Is it open source?", "a": "Yes — the code is public under the AGPL license, so you can audit exactly what it does."},
        {"q": "Can I disconnect?", "a": "Yes, any time. Disconnecting deletes the stored token for that project."},
    ]},
    "security": {"title": "Built to be trusted", "subtitle": "Security and transparency first.",
                 "points": [
                     {"title": "Encrypted tokens", "desc": "Your Overleaf Git token is encrypted at rest with authenticated encryption."},
                     {"title": "No document storage", "desc": "We never store your paper's contents — only account metadata and an encrypted token."},
                     {"title": "Only your projects", "desc": "MiLatexAI can only touch the specific Overleaf projects you connect."},
                     {"title": "Open source", "desc": "The full source is public under the AGPL, so anyone can verify it."},
                 ]},
    "cta": {"title": "Ready to write faster?", "subtitle": "Add MiLatexAI and connect your first project in minutes.",
            "button": "Get started"},
    "footer": {"blurb": "MiLatexAI connects your AI assistant to your Overleaf projects.",
               "rights": "© 2026 MiLatexAI. All rights reserved.",
               "language_label": "Language", "contact_label": "Contact",
               "contact_value": "support@milatexai.com"},
    "privacy": {"title": "Privacy Policy", "updated_label": "Last updated: July 2026",
                "intro": "This policy explains what MiLatexAI collects and how it is handled.",
                "sections": [
                    {"heading": "What we collect", "body": "Your account email (from single sign-on), an encrypted copy of the Overleaf Git token you provide, and a monthly commit counter."},
                    {"heading": "What we do not collect", "body": "We do not store the contents of your Overleaf documents."},
                    {"heading": "How tokens are secured", "body": "Tokens are encrypted at rest and decrypted only transiently to sync the projects you connect."},
                    {"heading": "Third parties", "body": "Sign-in is handled by WorkOS; hosting is on Microsoft Azure; edits sync through Overleaf's Git service."},
                    {"heading": "Retention and deletion", "body": "Disconnecting a project deletes its stored token. Contact us to delete your account."},
                    {"heading": "Contact", "body": "Questions? support@milatexai.com."},
                ]},
    "terms": {"title": "Terms of Service", "updated_label": "Last updated: July 2026",
              "intro": "By using MiLatexAI you agree to these terms.",
              "sections": [
                  {"heading": "The service", "body": "MiLatexAI lets AI assistants read and edit the Overleaf projects you connect."},
                  {"heading": "Your account and tokens", "body": "You are responsible for your Overleaf account and the tokens you provide."},
                  {"heading": "Acceptable use", "body": "Use the service lawfully and only with projects you are authorized to edit."},
                  {"heading": "Availability and warranty", "body": "The service is provided “as is” without warranty of any kind."},
                  {"heading": "Open-source code", "body": "The software is licensed under the AGPL; the source is publicly available."},
                  {"heading": "Contact", "body": "support@milatexai.com."},
              ]},
}


def load_content() -> dict:
    """Return ``{lang: content}``. Falls back to a built-in English dict."""
    if _CONTENT_PATH.is_file():
        try:
            data = json.loads(_CONTENT_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("en"):
                return data
        except (ValueError, OSError):
            pass
    return {"en": _FALLBACK_EN}


# --- rendering -------------------------------------------------------------

def _resolve(obj, path: str):
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            cur = cur[int(part)]
        else:
            cur = cur[part]
    return cur


def _t(en: dict, path: str) -> str:
    """Server-rendered English text for ``path`` (HTML-escaped)."""
    try:
        return html.escape(str(_resolve(en, path)))
    except (KeyError, IndexError, ValueError):
        return ""


def _node(en: dict, tag: str, path: str, cls: str = "") -> str:
    cls_attr = f" class='{cls}'" if cls else ""
    return f"<{tag} data-i18n=\"{path}\"{cls_attr}>{_t(en, path)}</{tag}>"


def render_site(content: dict | None = None, default_lang: str = "en") -> str:
    content = content or load_content()
    en = content.get("en", _FALLBACK_EN)
    langs = [l for l in LANG_NAMES if l in content] or ["en"]

    n_features = len(en.get("features", []))
    n_steps = len(en.get("how", {}).get("steps", []))
    n_free = len(en.get("pricing", {}).get("free", {}).get("features", []))
    n_pro = len(en.get("pricing", {}).get("pro", {}).get("features", []))
    n_faq = len(en.get("faq", {}).get("items", []))
    n_sec = len(en.get("security", {}).get("points", []))
    n_priv = len(en.get("privacy", {}).get("sections", []))
    n_terms = len(en.get("terms", {}).get("sections", []))

    # nav + language switcher
    options = "".join(
        f"<option value='{l}'{' selected' if l == default_lang else ''}>{html.escape(LANG_NAMES[l])}</option>"
        for l in langs
    )
    lang_select = (
        "<label class='lang'><span class='sr-only' data-i18n='footer.language_label'>"
        f"{_t(en, 'footer.language_label')}</span>"
        f"<select id='lang' aria-label='Language'>{options}</select></label>"
    )

    header = f"""<header class='nav'>
  <a class='brand' href='#top'>Mi<span>LaTeX</span>AI</a>
  <nav class='links'>
    <a href='#features' data-i18n='nav.features'>{_t(en,'nav.features')}</a>
    <a href='#how' data-i18n='nav.how'>{_t(en,'nav.how')}</a>
    <a href='#pricing' data-i18n='nav.pricing'>{_t(en,'nav.pricing')}</a>
    <a href='#faq' data-i18n='nav.faq'>{_t(en,'nav.faq')}</a>
    <a href='/account' data-i18n='nav.manage_subscription'>{_t(en,'nav.manage_subscription')}</a>
  </nav>
  <div class='navactions'>{lang_select}
    <a class='btn btn-sm' href='#get-started' data-i18n='nav.get_started'>{_t(en,'nav.get_started')}</a>
  </div>
</header>"""

    hero = f"""<section class='hero' id='top'>
  <span class='badge' data-i18n='hero.badge'>{_t(en,'hero.badge')}</span>
  {_node(en,'h1','hero.title','hero-title')}
  {_node(en,'p','hero.subtitle','hero-sub')}
  <div class='hero-cta'>
    <a class='btn' href='#get-started' data-i18n='hero.cta_primary'>{_t(en,'hero.cta_primary')}</a>
    <a class='btn btn-ghost' href='#how' data-i18n='hero.cta_secondary'>{_t(en,'hero.cta_secondary')}</a>
  </div>
  {_node(en,'p','hero.note','hero-note')}
  {_node(en,'p','trust','trust')}
</section>"""

    feature_cards = "".join(
        f"<div class='card'>{_node(en,'h3',f'features.{i}.title')}{_node(en,'p',f'features.{i}.desc','muted')}</div>"
        for i in range(n_features)
    )
    features = f"""<section class='section' id='features'>
  <div class='grid'>{feature_cards}</div>
</section>"""

    steps = "".join(
        f"<li class='step'><span class='num'>{i+1}</span><div>{_node(en,'h3',f'how.steps.{i}.title')}{_node(en,'p',f'how.steps.{i}.desc','muted')}</div></li>"
        for i in range(n_steps)
    )
    how = f"""<section class='section alt' id='how'>
  {_node(en,'h2','how.title','h2')}
  {_node(en,'p','how.subtitle','sub')}
  <ol class='steps'>{steps}</ol>
</section>"""

    get_started = f"""<section class='section' id='get-started'>
  {_node(en,'h2','platforms.title','h2')}
  {_node(en,'p','platforms.subtitle','sub')}
  <div class='grid two'>
    <div class='card'><h3>Claude</h3>{_node(en,'p','platforms.claude','muted')}</div>
    <div class='card'><h3>ChatGPT</h3>{_node(en,'p','platforms.chatgpt','muted')}</div>
  </div>
  <div class='connect-url'>
    <code>https://milatexai.com/mcp</code>
    <button class='btn btn-sm' onclick="navigator.clipboard&amp;&amp;navigator.clipboard.writeText('https://milatexai.com/mcp')">Copy</button>
  </div>
</section>"""

    free_feats = "".join(f"<li data-i18n='pricing.free.features.{i}'>{_t(en,f'pricing.free.features.{i}')}</li>" for i in range(n_free))
    pro_feats = "".join(f"<li data-i18n='pricing.pro.features.{i}'>{_t(en,f'pricing.pro.features.{i}')}</li>" for i in range(n_pro))
    pricing = f"""<section class='section alt' id='pricing'>
  {_node(en,'h2','pricing.title','h2')}
  {_node(en,'p','pricing.subtitle','sub')}
  <div class='grid two plans'>
    <div class='plan'>
      {_node(en,'h3','pricing.free.name')}
      <div class='price'><span data-i18n='pricing.free.price'>{_t(en,'pricing.free.price')}</span><small data-i18n='pricing.free.period'>{_t(en,'pricing.free.period')}</small></div>
      <ul>{free_feats}</ul>
      <a class='btn' href='#get-started' data-i18n='pricing.free.cta'>{_t(en,'pricing.free.cta')}</a>
    </div>
    <div class='plan featured'>
      <span class='ribbon' data-i18n='pricing.pro.badge'>{_t(en,'pricing.pro.badge')}</span>
      {_node(en,'h3','pricing.pro.name')}
      <div class='price'><span data-i18n='pricing.pro.price'>{_t(en,'pricing.pro.price')}</span><small data-i18n='pricing.pro.period'>{_t(en,'pricing.pro.period')}</small></div>
      <ul>{pro_feats}</ul>
      <a class='btn' href='mailto:support@milatexai.com?subject=MiLatexAI%20Pro' data-i18n='pricing.pro.cta'>{_t(en,'pricing.pro.cta')}</a>
    </div>
  </div>
  {_node(en,'p','pricing.note','sub center')}
</section>"""

    sec_points = "".join(
        f"<div class='card'>{_node(en,'h3',f'security.points.{i}.title')}{_node(en,'p',f'security.points.{i}.desc','muted')}</div>"
        for i in range(n_sec)
    )
    security = f"""<section class='section' id='security'>
  {_node(en,'h2','security.title','h2')}
  {_node(en,'p','security.subtitle','sub')}
  <div class='grid'>{sec_points}</div>
</section>"""

    faq_items = "".join(
        f"<details class='faq'><summary data-i18n='faq.items.{i}.q'>{_t(en,f'faq.items.{i}.q')}</summary>{_node(en,'p',f'faq.items.{i}.a','muted')}</details>"
        for i in range(n_faq)
    )
    faq = f"""<section class='section alt' id='faq'>
  {_node(en,'h2','faq.title','h2')}
  <div class='faqs'>{faq_items}</div>
</section>"""

    cta = f"""<section class='section cta'>
  {_node(en,'h2','cta.title','h2')}
  {_node(en,'p','cta.subtitle','sub')}
  <a class='btn btn-lg' href='#get-started' data-i18n='cta.button'>{_t(en,'cta.button')}</a>
</section>"""

    def _legal(kind: str, count: int) -> str:
        secs = "".join(
            f"<div class='legal-sec'>{_node(en,'h3',f'{kind}.sections.{i}.heading')}{_node(en,'p',f'{kind}.sections.{i}.body','muted pre')}</div>"
            for i in range(count)
        )
        return f"""<section class='section legal' id='{kind}'>
  {_node(en,'h2',f'{kind}.title','h2')}
  {_node(en,'p',f'{kind}.updated_label','sub small')}
  {_node(en,'p',f'{kind}.intro','muted')}
  {secs}
</section>"""

    privacy = _legal("privacy", n_priv)
    terms = _legal("terms", n_terms)

    footer = f"""<footer class='site-footer'>
  <div class='foot-brand'>Mi<span>LaTeX</span>AI</div>
  {_node(en,'p','footer.blurb','muted')}
  <nav class='foot-links'>
    <a href='#privacy' data-i18n='nav.privacy'>{_t(en,'nav.privacy')}</a>
    <a href='#terms' data-i18n='nav.terms'>{_t(en,'nav.terms')}</a>
    <a href='https://github.com/yasaminfayyaz/milatexai' target='_blank' rel='noopener'>GitHub</a>
    <a href='mailto:support@milatexai.com'><span data-i18n='footer.contact_label'>{_t(en,'footer.contact_label')}</span></a>
  </nav>
  {_node(en,'p','footer.rights','muted small')}
</footer>"""

    i18n_json = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    rtl_json = json.dumps(sorted(RTL_LANGS))

    script = f"""<script>
const I18N = {i18n_json};
const RTL = new Set({rtl_json});
function resolve(o, path) {{
  return path.split('.').reduce((a, k) => (a == null ? a : a[k]), o);
}}
function applyLang(lang) {{
  const dict = I18N[lang] || I18N.en;
  document.documentElement.lang = lang;
  document.documentElement.dir = RTL.has(lang) ? 'rtl' : 'ltr';
  document.querySelectorAll('[data-i18n]').forEach(el => {{
    const v = resolve(dict, el.getAttribute('data-i18n'));
    if (typeof v === 'string') el.textContent = v;
  }});
  try {{ localStorage.setItem('milatexai_lang', lang); }} catch (e) {{}}
}}
(function () {{
  const sel = document.getElementById('lang');
  const supported = new Set(Object.keys(I18N));
  let lang = 'en';
  try {{ lang = localStorage.getItem('milatexai_lang') || ''; }} catch (e) {{}}
  if (!supported.has(lang)) {{
    const nav = (navigator.language || 'en').slice(0, 2).toLowerCase();
    lang = supported.has(nav) ? nav : 'en';
  }}
  if (sel) {{ sel.value = lang; sel.addEventListener('change', e => applyLang(e.target.value)); }}
  if (lang !== 'en') applyLang(lang);
}})();
</script>"""

    desc = html.escape(str(en.get("hero", {}).get("subtitle", "")))
    return f"""<!doctype html><html lang='en'><head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>MiLatexAI — {html.escape(str(en.get('brand_tagline','')))}</title>
<meta name='description' content='{desc}'>
<style>{_CSS}</style>
</head><body>
{header}
<main>
{hero}
{features}
{how}
{get_started}
{security}
{pricing}
{faq}
{cta}
{privacy}
{terms}
</main>
{footer}
{script}
</body></html>"""


def render_account_placeholder() -> str:
    """A minimal /account page until Stripe billing lands."""
    return """<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Manage subscription · MiLatexAI</title><style>""" + _CSS + """</style></head>
<body><main class='section' style='max-width:640px;margin:0 auto;text-align:center;min-height:70vh;display:flex;flex-direction:column;justify-content:center'>
<a class='brand' href='/'>Mi<span>LaTeX</span>AI</a>
<h2 class='h2'>Manage your subscription</h2>
<p class='muted'>Billing is on the way. Right now everyone is on the free plan
(1 project, 25 commits/month, unlimited reads). When Pro launches you'll manage
your plan and payment here.</p>
<p class='muted'>Questions? <a href='mailto:support@milatexai.com'>support@milatexai.com</a></p>
<p><a class='btn' href='/'>Back to home</a></p>
</main></body></html>"""


_CSS = """
:root{color-scheme:light dark;--bg:#ffffff;--fg:#0f1420;--muted:#5b6472;--line:#e7eaf0;--card:#f7f8fb;--accent:#2f6df6;--accent2:#6a3df6;--radius:14px}
@media (prefers-color-scheme:dark){:root{--bg:#0b0d12;--fg:#e9ecf2;--muted:#9aa3b2;--line:#20242e;--card:#12151c}}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif}
a{color:inherit;text-decoration:none}
.sr-only{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.nav{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:20px;padding:14px 24px;background:color-mix(in srgb,var(--bg) 88%,transparent);backdrop-filter:blur(10px);border-bottom:1px solid var(--line)}
.brand{font-weight:800;font-size:19px;letter-spacing:-.02em}
.brand span{color:var(--accent)}
.links{display:flex;gap:20px;margin-inline-start:auto;font-size:14.5px}
.links a{color:var(--muted)}.links a:hover{color:var(--fg)}
.navactions{display:flex;align-items:center;gap:12px}
.lang select{background:var(--card);color:var(--fg);border:1px solid var(--line);border-radius:8px;padding:6px 8px;font:inherit;font-size:14px}
.btn{display:inline-block;background:var(--accent);color:#fff;padding:12px 20px;border-radius:10px;font-weight:600;border:0;cursor:pointer;font-size:15px}
.btn:hover{background:#245ce0}
.btn-sm{padding:8px 14px;font-size:14px}
.btn-lg{padding:15px 30px;font-size:17px}
.btn-ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
.btn-ghost:hover{background:var(--card)}
.hero{max-width:820px;margin:0 auto;padding:72px 24px 40px;text-align:center}
.badge{display:inline-block;background:color-mix(in srgb,var(--accent) 14%,transparent);color:var(--accent);font-weight:600;font-size:13px;padding:6px 12px;border-radius:999px;margin-bottom:20px}
.hero-title{font-size:clamp(32px,5vw,52px);line-height:1.08;letter-spacing:-.03em;margin:0 0 16px;background:linear-gradient(120deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}
.hero-sub{font-size:clamp(17px,2.2vw,20px);color:var(--muted);max-width:640px;margin:0 auto 26px}
.hero-cta{display:flex;gap:12px;justify-content:center;flex-wrap:wrap}
.hero-note{margin:22px auto 0;font-size:13.5px;color:var(--muted)}
.trust{font-size:13px;color:var(--muted);margin-top:6px;opacity:.85}
.section{max-width:1040px;margin:0 auto;padding:56px 24px}
.section.alt{background:var(--card);max-width:none}
.section.alt>*{max-width:1040px;margin-inline:auto}
.h2{font-size:clamp(24px,3.4vw,34px);letter-spacing:-.02em;text-align:center;margin:0 0 8px}
.sub{color:var(--muted);text-align:center;margin:0 auto 34px;max-width:600px}
.sub.small{font-size:13px}.center{text-align:center}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:18px}
.grid.two{grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--radius);padding:22px}
.section.alt .card{background:var(--bg)}
.card h3{margin:0 0 8px;font-size:17px}
.muted{color:var(--muted);margin:0}
.pre{white-space:pre-line}
.steps{list-style:none;padding:0;margin:34px auto 0;max-width:720px;display:grid;gap:16px}
.step{display:flex;gap:16px;align-items:flex-start;background:var(--bg);border:1px solid var(--line);border-radius:var(--radius);padding:18px}
.num{flex:0 0 34px;height:34px;border-radius:50%;background:var(--accent);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700}
.step h3{margin:0 0 4px;font-size:16px}
.connect-url{display:flex;gap:10px;align-items:center;justify-content:center;margin-top:26px;flex-wrap:wrap}
.connect-url code{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 14px;font-size:15px}
.plans{align-items:stretch}
.plan{position:relative;background:var(--bg);border:1px solid var(--line);border-radius:var(--radius);padding:26px;display:flex;flex-direction:column}
.section.alt .plan{background:var(--card)}
.plan.featured{border-color:var(--accent);box-shadow:0 8px 40px color-mix(in srgb,var(--accent) 18%,transparent)}
.ribbon{position:absolute;top:-11px;inset-inline-end:20px;background:var(--accent);color:#fff;font-size:12px;font-weight:600;padding:4px 10px;border-radius:999px}
.plan h3{font-size:18px;margin:0 0 6px}
.price{font-size:30px;font-weight:800;margin:6px 0 14px}
.price small{font-size:14px;font-weight:500;color:var(--muted)}
.plan ul{list-style:none;padding:0;margin:0 0 20px;display:grid;gap:9px;flex:1}
.plan li{padding-inline-start:26px;position:relative;color:var(--muted)}
.plan li::before{content:'✓';position:absolute;inset-inline-start:0;color:var(--accent);font-weight:700}
.faqs{max-width:760px;margin:0 auto;display:grid;gap:10px}
.faq{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:4px 18px}
.section.alt .faq{background:var(--card)}
.faq summary{cursor:pointer;font-weight:600;padding:14px 0;list-style:none}
.faq summary::-webkit-details-marker{display:none}
.faq[open] summary{border-bottom:1px solid var(--line)}
.faq p{padding:12px 0}
.cta{text-align:center}
.legal{max-width:760px}
.legal .h2{text-align:start}.legal-sec{margin-top:22px}.legal-sec h3{font-size:17px;margin:0 0 6px}
.site-footer{border-top:1px solid var(--line);padding:40px 24px;text-align:center;color:var(--muted)}
.foot-brand{font-weight:800;font-size:18px}.foot-brand span{color:var(--accent)}
.foot-links{display:flex;gap:20px;justify-content:center;flex-wrap:wrap;margin:16px 0;font-size:14px}
.foot-links a{color:var(--muted)}.foot-links a:hover{color:var(--fg)}
.small{font-size:13px}
@media(max-width:640px){.links{display:none}}
"""
