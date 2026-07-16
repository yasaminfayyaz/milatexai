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

No external assets, all CSS/JS is inline, so it works behind the same strict
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
             "note": "Your Overleaf token is entered on a secure web page, never in the chat."},
    "trust": "Encrypted tokens · Open-source (AGPL) · Your documents are never stored.",
    "features": [
        {"title": "Edit by conversation", "desc": "Ask your AI to rewrite a section, fix a table, or add a figure, it edits the actual .tex and pushes to Overleaf."},
        {"title": "Real Git commits", "desc": "Every change is a commit you can see and revert in your Overleaf history."},
        {"title": "Claude and ChatGPT", "desc": "Add one connector; use whichever assistant you prefer."},
        {"title": "Token stays out of chat", "desc": "You paste your Overleaf Git token into a secure web form; it is encrypted and never appears in the transcript."},
        {"title": "Reads are free", "desc": "Reading files, sections and history is unlimited. Only writes count toward your plan."},
        {"title": "Compile check", "desc": "Ask it to verify the project still builds before you trust an edit."},
    ],
    "how": {"title": "Three steps", "subtitle": "You're editing in minutes.",
            "steps": [
                {"title": "Add the connector", "desc": "Add MiLatexAI in Claude or ChatGPT and sign in."},
                {"title": "Connect a project", "desc": "Run start_connect, open the secure link, and paste your Overleaf Git token, it's encrypted, never shown in chat."},
                {"title": "Ask for edits", "desc": "Tell your AI what to change. It edits the LaTeX and pushes it live to Overleaf."},
            ]},
    "platforms": {"title": "One connector, two assistants", "subtitle": "MiLatexAI speaks the Model Context Protocol (MCP).",
                  "claude": "Add as a custom connector in Claude.",
                  "chatgpt": "Add as a custom connector in ChatGPT."},
    "setup": {"title": "Add MiLatexAI in two minutes",
              "subtitle": "The same connector works in Claude and ChatGPT.",
              "url_label": "Connector URL", "docs_label": "Official setup guide →",
              "claude_title": "In Claude",
              "claude_steps": [
                  "Open Settings → Connectors → Add custom connector.",
                  "Paste the connector URL above and sign in.",
                  "Run start_connect and paste your Overleaf Git token on the secure page.",
                  "Ask Claude to read a section or edit your paper."],
              "chatgpt_title": "In ChatGPT",
              "chatgpt_steps": [
                  "In Settings → Connectors, enable Developer mode, then Add custom connector.",
                  "Paste the connector URL above and authorize access.",
                  "Run start_connect and paste your token on the secure page.",
                  "Ask ChatGPT to edit your paper."],
              "chatgpt_note": "Adding a custom connector needs ChatGPT Plus or higher; full editing works best on Business or Enterprise. See the guide.",
              "claude_docs_url": "https://support.claude.com/en/articles/11175166-getting-started-with-custom-connectors-using-remote-mcp",
              "chatgpt_docs_url": "https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt"},
    "pricing": {"title": "Simple pricing", "subtitle": "Start free. Upgrade when you need more.",
                "free": {"name": "Free", "price": "$0", "period": "/mo",
                         "features": ["1 connected project", "25 write-commits / month", "Unlimited reads & compile checks"],
                         "cta": "Get started",
                         "availability": "Best-effort · subject to capacity"},
                "pro": {"name": "Pro", "price": "$4.99", "period": "/mo", "badge": "Unlimited",
                        "features": ["Unlimited projects", "Unlimited commits", "Priority support"],
                        "cta": "Get started",
                        "availability": "Guaranteed · always on"},
                "note": "Reads are always free on every plan.",
                "capacity_note": "The free tier runs on spare capacity and may pause briefly when demand is high. Pro guarantees uninterrupted access, you are never turned away."},
    "faq": {"title": "Frequently asked questions", "items": [
        {"q": "Is my Overleaf token safe?", "a": "You enter it on a secure web page, not in the chat. It's encrypted at rest and only used to sync your connected projects over Git."},
        {"q": "Do you see my documents?", "a": "We don't store your document contents. Edits flow through Overleaf's Git integration; we keep only your account email, your encrypted token, and a commit counter."},
        {"q": "Which assistants work?", "a": "Anthropic Claude and OpenAI ChatGPT, via the Model Context Protocol."},
        {"q": "What happens at the free limit?", "a": "Reads stay free and unlimited. New write-commits resume next month, or you can upgrade to Pro."},
        {"q": "Is it open source?", "a": "Yes, the code is public under the AGPL license, so you can audit exactly what it does."},
        {"q": "Can I disconnect?", "a": "Yes, any time. Disconnecting deletes the stored token for that project."},
    ]},
    "security": {"title": "Built to be trusted", "subtitle": "Security and transparency first.",
                 "points": [
                     {"title": "Encrypted tokens", "desc": "Your Overleaf Git token is encrypted at rest with authenticated encryption."},
                     {"title": "No document storage", "desc": "We never store your paper's contents, only account metadata and an encrypted token."},
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


# Hero illustration: your AI edits your real research paper. A LaTeX paper (title,
# a real equation, body text) with one line freshly edited + committed, driven by
# a chat instruction. Language-neutral (only universal math), theme-aware via vars.
_HERO_ART = """<svg class='hero-art' viewBox='0 0 640 300' role='img'
  aria-label='Your AI edits your real Overleaf research paper' xmlns='http://www.w3.org/2000/svg'>
  <!-- the paper -->
  <rect x='300' y='30' width='312' height='236' rx='16' fill='var(--card)' stroke='var(--line)'/>
  <rect x='366' y='54' width='180' height='12' rx='6' fill='var(--accent)'/>
  <rect x='396' y='78' width='120' height='7' rx='3.5' fill='var(--muted)' opacity='.4'/>
  <rect x='411' y='90' width='90' height='7' rx='3.5' fill='var(--muted)' opacity='.3'/>
  <rect x='346' y='112' width='220' height='46' rx='10' fill='var(--bg)' stroke='var(--line)'/>
  <text x='456' y='143' text-anchor='middle' fill='var(--fg)'
    font-family='Georgia, "Times New Roman", serif' font-size='24'>e<tspan
    font-size='15' dy='-9'>i&#960;</tspan><tspan dy='9'> + 1 = 0</tspan></text>
  <rect x='324' y='176' width='84' height='9' rx='4' fill='var(--accent)' opacity='.85'/>
  <rect x='324' y='194' width='264' height='8' rx='4' fill='var(--muted)' opacity='.32'/>
  <rect x='318' y='208' width='278' height='18' rx='5' fill='var(--accent)' opacity='.13'/>
  <rect x='324' y='213' width='236' height='8' rx='4' fill='var(--accent)'/>
  <rect x='565' y='210' width='2.5' height='14' rx='1' fill='var(--accent)'/>
  <rect x='324' y='234' width='210' height='8' rx='4' fill='var(--muted)' opacity='.32'/>
  <rect x='324' y='250' width='244' height='8' rx='4' fill='var(--muted)' opacity='.32'/>
  <!-- committed -->
  <circle cx='585' cy='188' r='11' fill='#22c55e'/>
  <path d='M579 188 l4 4 l8 -9' stroke='#fff' stroke-width='2.4' fill='none'
    stroke-linecap='round' stroke-linejoin='round'/>
  <!-- the instruction that caused the edit -->
  <path d='M232 236 Q286 226 314 217' fill='none' stroke='var(--accent)'
    stroke-width='2' stroke-dasharray='2 6' stroke-linecap='round' opacity='.5'/>
  <rect x='30' y='150' width='192' height='94' rx='18' fill='var(--accent)'/>
  <path d='M208 230 L232 248 L204 242 Z' fill='var(--accent)'/>
  <rect x='52' y='176' width='146' height='9' rx='4.5' fill='#fff' opacity='.95'/>
  <rect x='52' y='195' width='116' height='9' rx='4.5' fill='#fff' opacity='.78'/>
  <rect x='52' y='214' width='86' height='9' rx='4.5' fill='#fff' opacity='.6'/>
</svg>"""


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
  {_HERO_ART}
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

    setup = en.get("setup", {})
    n_cl = len(setup.get("claude_steps", []))
    n_cg = len(setup.get("chatgpt_steps", []))
    claude_docs = html.escape(str(setup.get("claude_docs_url", "#")), quote=True)
    chatgpt_docs = html.escape(str(setup.get("chatgpt_docs_url", "#")), quote=True)
    claude_steps = "".join(
        f"<li data-i18n='setup.claude_steps.{i}'>{_t(en, f'setup.claude_steps.{i}')}</li>"
        for i in range(n_cl)
    )
    chatgpt_steps = "".join(
        f"<li data-i18n='setup.chatgpt_steps.{i}'>{_t(en, f'setup.chatgpt_steps.{i}')}</li>"
        for i in range(n_cg)
    )
    get_started = f"""<section class='section' id='get-started'>
  {_node(en,'h2','setup.title','h2')}
  {_node(en,'p','setup.subtitle','sub')}
  <div class='connect-url'>
    <span class='cu-label' data-i18n='setup.url_label'>{_t(en,'setup.url_label')}</span>
    <code>https://milatexai.com/mcp</code>
    <button class='btn btn-sm' onclick="navigator.clipboard&amp;&amp;navigator.clipboard.writeText('https://milatexai.com/mcp')">Copy</button>
  </div>
  <div class='grid two setup-cols'>
    <div class='card setup-card'>
      {_node(en,'h3','setup.claude_title')}
      <ol class='setup-steps'>{claude_steps}</ol>
      <a class='doclink' href='{claude_docs}' target='_blank' rel='noopener' data-i18n='setup.docs_label'>{_t(en,'setup.docs_label')}</a>
    </div>
    <div class='card setup-card'>
      {_node(en,'h3','setup.chatgpt_title')}
      <ol class='setup-steps'>{chatgpt_steps}</ol>
      {_node(en,'p','setup.chatgpt_note','muted small')}
      <a class='doclink' href='{chatgpt_docs}' target='_blank' rel='noopener' data-i18n='setup.docs_label'>{_t(en,'setup.docs_label')}</a>
    </div>
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
      {_node(en,'p','pricing.free.availability','avail')}
      <div class='price'><span data-i18n='pricing.free.price'>{_t(en,'pricing.free.price')}</span><small data-i18n='pricing.free.period'>{_t(en,'pricing.free.period')}</small></div>
      <ul>{free_feats}</ul>
      <a class='btn' href='#get-started' data-i18n='pricing.free.cta'>{_t(en,'pricing.free.cta')}</a>
    </div>
    <div class='plan featured'>
      <span class='ribbon' data-i18n='pricing.pro.badge'>{_t(en,'pricing.pro.badge')}</span>
      {_node(en,'h3','pricing.pro.name')}
      {_node(en,'p','pricing.pro.availability','avail pro')}
      <div class='price'><span data-i18n='pricing.pro.price'>{_t(en,'pricing.pro.price')}</span><small data-i18n='pricing.pro.period'>{_t(en,'pricing.pro.period')}</small></div>
      <ul>{pro_feats}</ul>
      <a class='btn' href='/account' data-i18n='pricing.pro.cta'>{_t(en,'pricing.pro.cta')}</a>
    </div>
  </div>
  {_node(en,'p','pricing.capacity_note','sub center small')}
  {_node(en,'p','pricing.note','sub center small')}
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
  {_node(en,'p','footer.disclaimer','muted small disclaimer')}
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
    tagline = html.escape(str(en.get("brand_tagline", "")))
    # SEO <title> front-loads the high-intent search terms, then the brand
    # (people search "edit Overleaf with ChatGPT", not "MiLatexAI").
    title = "Edit Overleaf with ChatGPT or Claude · MiLatexAI"
    meta_desc = html.escape(
        "Edit your real Overleaf LaTeX papers by chatting with ChatGPT or Claude. "
        "Read, edit, and fix compile errors; every change is a real Git commit. "
        "Free tier plus Pro."
    )
    url = "https://milatexai.com/"
    og_image = "https://milatexai.com/og.svg"
    price = html.escape(str(en.get("pricing", {}).get("pro", {}).get("price", "$4.99")))
    jsonld = json.dumps({
        "@context": "https://schema.org",
        "@type": "SoftwareApplication",
        "name": "MiLatexAI",
        "applicationCategory": "BusinessApplication",
        "operatingSystem": "Web (Claude, ChatGPT)",
        "url": "https://milatexai.com",
        "description": str(en.get("hero", {}).get("subtitle", "")),
        "offers": [
            {"@type": "Offer", "price": "0", "priceCurrency": "USD", "name": "Free"},
            {"@type": "Offer", "price": "4.99", "priceCurrency": "USD", "name": "Pro"},
        ],
        "sameAs": ["https://github.com/yasaminfayyaz/milatexai"],
    }, ensure_ascii=False)
    # FAQ structured data, built from the English FAQ (helps search engines
    # understand the page and can surface Q&A in results).
    faq_jsonld = json.dumps({
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": [
            {"@type": "Question", "name": str(it.get("q", "")),
             "acceptedAnswer": {"@type": "Answer", "text": str(it.get("a", ""))}}
            for it in en.get("faq", {}).get("items", [])
        ],
    }, ensure_ascii=False)
    seo = f"""<title>{title}</title>
<meta name='description' content='{meta_desc}'>
<meta name='keywords' content='edit Overleaf with ChatGPT, edit Overleaf with Claude, Overleaf AI editor, ChatGPT Overleaf, Claude Overleaf, edit LaTeX with AI, Overleaf MCP connector, AI research paper editor, LaTeX AI assistant'>
<link rel='canonical' href='{url}'>
<meta name='robots' content='index,follow'>
<meta name='theme-color' content='#0891b2'>
<meta property='og:type' content='website'>
<meta property='og:site_name' content='MiLatexAI'>
<meta property='og:title' content='{title}'>
<meta property='og:description' content='{desc}'>
<meta property='og:url' content='{url}'>
<meta property='og:image' content='{og_image}'>
<meta name='twitter:card' content='summary_large_image'>
<meta name='twitter:title' content='{title}'>
<meta name='twitter:description' content='{desc}'>
<meta name='twitter:image' content='{og_image}'>
<script type='application/ld+json'>{jsonld}</script>
<script type='application/ld+json'>{faq_jsonld}</script>"""
    return f"""<!doctype html><html lang='en'><head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
{seo}
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


def render_account(
    status: str | None = None,
    *,
    signed_in: bool = False,
    account: dict | None = None,
    billing_enabled: bool = True,
    has_projects: bool = False,
) -> str:
    """The /account page.

    ``status`` reflects a Stripe Checkout return (``success`` / ``cancelled``).
    When ``signed_in`` is True, ``account`` is ``{email, plan, is_admin,
    has_customer}`` and the page shows the real upgrade / manage-or-cancel /
    sign-out controls; otherwise it invites the user to sign in with the same
    account they use in Claude or ChatGPT.
    """
    acct = account or {}
    email = acct.get("email") or ""
    plan = acct.get("plan") or "free"
    is_admin = bool(acct.get("is_admin"))
    has_customer = bool(acct.get("has_customer"))

    # A checkout return is shown as a small banner above the main state.
    banner = ""
    if status == "success":
        banner = ("<div class='acct-banner ok'>🎉 <b>You're on Pro!</b> Thanks for "
                  "upgrading. It may take a few seconds to reflect in your assistant.</div>")
    elif status == "cancelled":
        banner = ("<div class='acct-banner'>↩️ Checkout cancelled, no charge was made. "
                  "You can upgrade any time.</div>")

    def _form(action: str, label: str, cls: str = "btn") -> str:
        return (f"<form method='post' action='{action}' style='margin:0'>"
                f"<button class='{cls}' type='submit'>{html.escape(label)}</button></form>")

    signout = _form("/logout", "Sign out", "btn btn-ghost btn-sm")

    if not signed_in:
        icon, heading = "🔐", "Sign in"
        body = ("Sign in with the same account you use in Claude or ChatGPT to "
                "upgrade, view, or cancel your subscription here. You can also manage "
                "billing right inside your assistant, whichever you prefer.")
        actions = "<a class='btn btn-lg' href='/login'>Sign in</a>"
    elif is_admin:
        icon, heading = "🛠️", "You have admin access"
        body = "Your account has unlimited access, no subscription needed."
        actions = signout
    elif plan == "pro":
        icon, heading = "⭐", "You're on Pro"
        body = ("Unlimited projects and unlimited write-commits. Update your payment "
                "method, view invoices, or cancel any time.")
        manage = (_form("/account/manage", "Manage or cancel subscription")
                  if (billing_enabled and has_customer) else "")
        actions = f"{manage}{signout}"
    else:
        icon, heading = "💳", "You're on the Free plan"
        body = ("Upgrade to Pro for unlimited projects and unlimited write-commits, "
                "$4.99/mo. Reads stay free and unlimited either way.")
        upgrade = (_form("/account/upgrade", "Upgrade to Pro · $4.99/mo")
                   if billing_enabled else
                   "<p class='muted'>Upgrades are briefly unavailable, please try again soon.</p>")
        actions = f"{upgrade}{signout}"

    whoami = (f"<p class='muted' style='margin-top:2px'>Signed in as {html.escape(email)}</p>"
              if signed_in and email else "")

    # Web-first signups (signed in, no project connected yet) need to know the
    # actual next step: editing happens through the assistant connector.
    nudge = ""
    if signed_in and not has_projects:
        nudge = (
            "<div class='acct-nudge'><b>Next step: start editing</b><br>"
            "Editing happens inside your assistant. Add MiLatexAI as a connector in "
            "Claude or ChatGPT, then connect an Overleaf project (this needs an "
            "Overleaf plan with Git integration). It takes about two minutes. "
            "<a href='/#get-started'>See the setup guide</a>.</div>"
        )

    return f"""<!doctype html><html lang='en'><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>{html.escape(heading)} · MiLatexAI</title><style>{_CSS}</style></head>
<body><main class='section' style='max-width:560px;margin:0 auto;text-align:center;min-height:72vh;display:flex;flex-direction:column;justify-content:center;gap:8px'>
<a class='brand' href='/' style='margin-bottom:10px'>Mi<span>LaTeX</span>AI</a>
{banner}
<div style='font-size:44px'>{icon}</div>
<h2 class='h2'>{html.escape(heading)}</h2>
<p class='muted'>{html.escape(body)}</p>
{whoami}
{nudge}
<div class='acct-actions'>{actions}</div>
<p class='muted' style='margin-top:14px'>Questions? <a href='mailto:support@milatexai.com'>support@milatexai.com</a></p>
<p style='margin-top:2px'><a class='muted' href='/'>Back to home</a></p>
</main></body></html>"""


def render_og_image() -> str:
    """A 1200x630 social preview image (SVG) referenced by og:image."""
    return """<svg xmlns='http://www.w3.org/2000/svg' width='1200' height='630' viewBox='0 0 1200 630'>
<rect width='1200' height='630' fill='#0b0d12'/>
<rect x='0' y='0' width='1200' height='8' fill='#0891b2'/>
<text x='90' y='300' font-family='Segoe UI, Helvetica, Arial, sans-serif' font-size='96' font-weight='800' fill='#ffffff'>Mi<tspan fill='#0891b2'>LaTeX</tspan>AI</text>
<text x='94' y='378' font-family='Segoe UI, Helvetica, Arial, sans-serif' font-size='42' fill='#c7cdd8'>Edit your Overleaf papers by talking to your AI.</text>
<text x='94' y='452' font-family='Segoe UI, Helvetica, Arial, sans-serif' font-size='30' fill='#7b8496'>Works with Claude &amp; ChatGPT  ·  milatexai.com</text>
</svg>"""


def robots_txt() -> str:
    return "User-agent: *\nAllow: /\nSitemap: https://milatexai.com/sitemap.xml\n"


def sitemap_xml() -> str:
    return (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>\n"
        "  <url><loc>https://milatexai.com/</loc><changefreq>weekly</changefreq><priority>1.0</priority></url>\n"
        "  <url><loc>https://milatexai.com/account</loc><changefreq>monthly</changefreq><priority>0.5</priority></url>\n"
        "</urlset>\n"
    )


_CSS = """
:root{color-scheme:light dark;--bg:#ffffff;--fg:#0f1420;--muted:#5b6472;--line:#e7eaf0;--card:#f7f8fb;--accent:#0891b2;--accent2:#0d9488;--radius:14px}
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
.btn:hover{background:#0e7490}
.btn-sm{padding:8px 14px;font-size:14px}
.btn-lg{padding:15px 30px;font-size:17px}
.btn-ghost{background:transparent;color:var(--fg);border:1px solid var(--line)}
.btn-ghost:hover{background:var(--card)}
.acct-banner{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;font-size:14px;color:var(--muted);margin-bottom:6px}
.acct-banner.ok{border-color:color-mix(in srgb,var(--accent) 40%,var(--line));color:var(--fg)}
.acct-actions{display:flex;flex-direction:column;align-items:center;gap:10px;margin-top:10px}
.acct-nudge{background:color-mix(in srgb,var(--accent) 8%,var(--card));border:1px solid color-mix(in srgb,var(--accent) 30%,var(--line));border-radius:12px;padding:14px 16px;font-size:14px;text-align:start;margin:8px auto;max-width:460px}
.acct-nudge a{color:var(--accent);font-weight:600}
.hero{max-width:820px;margin:0 auto;padding:72px 24px 40px;text-align:center}
.badge{display:inline-block;background:color-mix(in srgb,var(--accent) 14%,transparent);color:var(--accent);font-weight:600;font-size:13px;padding:6px 12px;border-radius:999px;margin-bottom:20px}
.hero-title{font-size:clamp(32px,5vw,52px);line-height:1.16;letter-spacing:-.03em;margin:0 0 26px;padding-bottom:6px;background:linear-gradient(120deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}
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
.cu-label{font-size:11px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.08em}
.hero-art{width:100%;max-width:560px;height:auto;margin:26px auto 8px;display:block}
.setup-cols{margin-top:26px;text-align:start}
.setup-card{padding:24px}
.setup-card h3{margin:0 0 12px;font-size:17px}
.setup-steps{margin:0 0 14px;padding-inline-start:20px;display:grid;gap:8px}
.setup-steps li{color:var(--muted);padding-inline-start:4px}
.setup-steps li::marker{color:var(--accent);font-weight:700}
.doclink{color:var(--accent);font-weight:600;font-size:14px}
.doclink:hover{text-decoration:underline}
.plans{align-items:stretch}
.plan{position:relative;background:var(--bg);border:1px solid var(--line);border-radius:var(--radius);padding:26px;display:flex;flex-direction:column}
.section.alt .plan{background:var(--card)}
.plan.featured{border-color:var(--accent);box-shadow:0 8px 40px color-mix(in srgb,var(--accent) 18%,transparent)}
.ribbon{position:absolute;top:-11px;inset-inline-end:20px;background:var(--accent);color:#fff;font-size:12px;font-weight:600;padding:4px 10px;border-radius:999px}
.plan h3{font-size:18px;margin:0 0 6px}
.avail{font-size:12px;font-weight:600;color:var(--muted);margin:0 0 10px;letter-spacing:.01em}
.avail.pro{color:var(--accent)}
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
.disclaimer{max-width:640px;margin:10px auto 0;font-size:11.5px;opacity:.8}
@media(max-width:640px){.links{display:none}}
"""
