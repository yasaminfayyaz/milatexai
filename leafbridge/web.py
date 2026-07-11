"""Self-contained HTML for the tiny web surface (the connect page + a landing
page). No templates engine, no external assets — one string per page, every
interpolated value HTML-escaped. Kept apart from the server wiring so the markup
is easy to eyeball and change.
"""

from __future__ import annotations

import html

BRAND = "MiLatexAI"
TAGLINE = "Edit your Overleaf projects from Claude."

_STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; display: flex; align-items: center;
  justify-content: center; padding: 24px;
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background: #f5f6f8; color: #1a1d21;
}
@media (prefers-color-scheme: dark) {
  body { background: #0f1115; color: #e8eaed; }
  .card { background: #191c22 !important; box-shadow: none !important; border: 1px solid #2a2e37; }
  input { background: #0f1115 !important; color: #e8eaed !important; border-color: #333844 !important; }
  .muted { color: #9aa0aa !important; }
  .note { background: #14181f !important; border-color: #2a2e37 !important; }
}
.card {
  width: 100%; max-width: 460px; background: #fff; border-radius: 14px;
  box-shadow: 0 8px 40px rgba(0,0,0,.08); padding: 32px;
}
.brand { font-size: 20px; font-weight: 700; letter-spacing: -.02em; }
.brand span { color: #2f6df6; }
h1 { font-size: 21px; margin: 18px 0 6px; letter-spacing: -.01em; }
.muted { color: #6b7280; font-size: 14px; margin: 0 0 20px; }
label { display: block; font-size: 13px; font-weight: 600; margin: 16px 0 6px; }
input {
  width: 100%; padding: 11px 12px; font-size: 15px; border: 1px solid #d7dbe0;
  border-radius: 9px; background: #fff; color: #1a1d21; font-family: inherit;
}
input:focus { outline: 2px solid #2f6df6; outline-offset: 1px; border-color: #2f6df6; }
button {
  width: 100%; margin-top: 22px; padding: 12px; font-size: 15px; font-weight: 600;
  color: #fff; background: #2f6df6; border: 0; border-radius: 9px; cursor: pointer;
}
button:hover { background: #245ce0; }
.hint { font-size: 12px; color: #6b7280; margin: 5px 0 0; }
.hint a { color: #2f6df6; }
.note {
  margin-top: 22px; padding: 12px 14px; background: #f5f7fb; border: 1px solid #e6e9ef;
  border-radius: 9px; font-size: 12.5px; color: #6b7280;
}
.error {
  margin: 4px 0 18px; padding: 11px 13px; background: #fdecec; border: 1px solid #f6c6c6;
  color: #a61b1b; border-radius: 9px; font-size: 13.5px;
}
.ok { text-align: center; }
.ok .big { font-size: 40px; line-height: 1; margin-bottom: 8px; }
"""


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><div class='card'>{body}</div></body></html>"
    )


def _brand_header() -> str:
    return f"<div class='brand'>Mi<span>LaTeX</span>AI</div>"


def render_connect_form(
    code: str,
    *,
    overleaf_url: str = "",
    name: str = "",
    email: str = "",
    error: str | None = None,
) -> str:
    err_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    who = (
        f"<p class='muted'>Signed in as {html.escape(email)}. "
        "Your token is stored encrypted and never shown again.</p>"
        if email
        else "<p class='muted'>Your token is stored encrypted and never shown again.</p>"
    )
    return _page(
        f"Connect a project · {BRAND}",
        f"""{_brand_header()}
<h1>Connect an Overleaf project</h1>
{who}
{err_html}
<form method='post' action='/connect' autocomplete='off'>
  <input type='hidden' name='code' value='{html.escape(code, quote=True)}'>
  <label for='overleaf_url'>Overleaf project link</label>
  <input id='overleaf_url' name='overleaf_url' inputmode='url'
         placeholder='https://www.overleaf.com/project/…'
         value='{html.escape(overleaf_url, quote=True)}' required>
  <label for='token'>Overleaf Git token</label>
  <input id='token' name='token' type='password' placeholder='olp_…' required>
  <p class='hint'>Create one in Overleaf → Account Settings →
     <a href='https://www.overleaf.com/user/settings' target='_blank' rel='noopener'>Git Integration</a>.</p>
  <label for='name'>Label <span class='muted'>(optional)</span></label>
  <input id='name' name='name' placeholder='e.g. thesis'
         value='{html.escape(name, quote=True)}'>
  <button type='submit'>Connect securely</button>
</form>
<div class='note'>🔒 This link is single-use and expires 15 minutes after you
  generated it. Your Git token is encrypted before it touches disk — it is never
  written to the chat.</div>""",
    )


def render_success(project_name: str, project_id: str) -> str:
    return _page(
        f"Connected · {BRAND}",
        f"""{_brand_header()}
<div class='ok'>
  <div class='big'>✅</div>
  <h1>Connected</h1>
  <p class='muted'>Project <b>{html.escape(project_name)}</b>
     (<code>{html.escape(project_id)}</code>) is now linked to your account.</p>
  <p class='muted'>You can close this tab and go back to Claude — try
     <b>“list my files”</b> or ask it to edit your paper.</p>
</div>""",
    )


def render_notice(title: str, message: str, *, icon: str = "⚠️") -> str:
    return _page(
        f"{title} · {BRAND}",
        f"""{_brand_header()}
<div class='ok'>
  <div class='big'>{html.escape(icon)}</div>
  <h1>{html.escape(title)}</h1>
  <p class='muted'>{html.escape(message)}</p>
</div>""",
    )


def render_landing() -> str:
    return _page(
        BRAND,
        f"""{_brand_header()}
<h1>{html.escape(TAGLINE)}</h1>
<p class='muted'>{BRAND} is a connector that lets Claude read and edit your real
   Overleaf projects over Overleaf's Git integration.</p>
<div class='note'>To get started, add {BRAND} as a connector in Claude, then run
   <b>start_connect</b> to link a project. There's nothing to configure on this
   page directly.</div>""",
    )
