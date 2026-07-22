"""Self-contained HTML for the tiny web surface (the connect page + a landing
page). No templates engine, no external assets, one string per page, every
interpolated value HTML-escaped. Kept apart from the server wiring so the markup
is easy to eyeball and change.
"""

from __future__ import annotations

import html

BRAND = "MiLatexAI"
TAGLINE = "Edit your Overleaf, GitHub, or GitLab LaTeX projects from your AI assistant."

# Shown under the token field on every connect/manage form: how to create a
# token per provider (Overleaf first).
_TOKEN_HINT = (
    "<p class='hint'>Overleaf: Account Settings → "
    "<a href='https://www.overleaf.com/user/settings' target='_blank' rel='noopener'>Git Integration</a>. "
    "GitHub: a fine-grained personal access token with Contents read/write. "
    "GitLab: a project access token with the read/write repository scope.</p>"
)
# Placeholder that leads with an Overleaf URL, then the other providers.
_REPO_PLACEHOLDER = "https://www.overleaf.com/project/… · GitHub · GitLab · Bitbucket URL"

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
.brand span { color: #89ecda; }
h1 { font-size: 21px; margin: 18px 0 6px; letter-spacing: -.01em; }
.muted { color: #6b7280; font-size: 14px; margin: 0 0 20px; }
label { display: block; font-size: 13px; font-weight: 600; margin: 16px 0 6px; }
input {
  width: 100%; padding: 11px 12px; font-size: 15px; border: 1px solid #d7dbe0;
  border-radius: 9px; background: #fff; color: #1a1d21; font-family: inherit;
}
input:focus { outline: 2px solid #89ecda; outline-offset: 1px; border-color: #89ecda; }
button {
  width: 100%; margin-top: 22px; padding: 12px; font-size: 15px; font-weight: 600;
  color: #0b3b33; background: #89ecda; border: 0; border-radius: 9px; cursor: pointer;
}
button:hover { background: #5fd9bf; }
.btn-danger { width: auto; margin: 0; padding: 8px 14px; font-size: 13px; background: #e5484d; }
.btn-danger:hover { background: #c93b40; }
.proj-list { margin: 14px 0 4px; }
.proj-row { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 12px 0; border-bottom: 1px solid #e6e9ef; }
@media (prefers-color-scheme: dark) { .proj-row { border-color: #2a2e37; } }
.hint { font-size: 12px; color: #6b7280; margin: 5px 0 0; }
.hint a { color: #89ecda; }
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
    has_token: bool = False,
) -> str:
    err_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    # The token field is ALWAYS shown: non-Overleaf repos carry a per-repo token,
    # so a saved Overleaf token can't stand in for them. For a returning user the
    # field is optional — leaving it blank reuses the saved Overleaf token, but
    # only for an Overleaf project.
    if has_token:
        who = (
            f"<p class='muted'>Signed in as {html.escape(email)}. "
            "Leave the token blank to reuse your saved Overleaf token for an "
            "Overleaf project; for GitHub, GitLab, or Bitbucket paste that repo's "
            "access token.</p>"
            if email
            else "<p class='muted'>Leave the token blank to reuse your saved Overleaf "
            "token for an Overleaf project; for GitHub, GitLab, or Bitbucket paste "
            "that repo's access token.</p>"
        )
        token_field = (
            "<label for='token'>Git access token <span class='muted'>(optional for Overleaf)</span></label>"
            "<input id='token' name='token' type='password' placeholder='olp_… · github_pat_… · glpat-…'>"
            + _TOKEN_HINT
        )
        note = ("<div class='note'>🔒 This link is single-use and expires 15 minutes after "
                "you generated it. The AI only ever touches the projects you add here.</div>")
    else:
        who = (
            f"<p class='muted'>Signed in as {html.escape(email)}. "
            "Your token is stored encrypted and never shown again.</p>"
            if email
            else "<p class='muted'>Your token is stored encrypted and never shown again.</p>"
        )
        token_field = (
            "<label for='token'>Git access token</label>"
            "<input id='token' name='token' type='password' placeholder='olp_… · github_pat_… · glpat-…' required>"
            + _TOKEN_HINT
        )
        note = ("<div class='note'>🔒 This link is single-use and expires 15 minutes after you "
                "generated it. Your Git token is encrypted before it touches disk, it is never "
                "written to the chat.</div>")
    return _page(
        f"Connect a project · {BRAND}",
        f"""{_brand_header()}
<h1>Connect an Overleaf, GitHub, or GitLab project</h1>
{who}
{err_html}
<form method='post' action='/connect' autocomplete='off'>
  <input type='hidden' name='code' value='{html.escape(code, quote=True)}'>
  <label for='overleaf_url'>Repository link</label>
  <input id='overleaf_url' name='overleaf_url' inputmode='url'
         placeholder='{html.escape(_REPO_PLACEHOLDER, quote=True)}'
         value='{html.escape(overleaf_url, quote=True)}' required>
  <p class='hint'>An Overleaf, GitHub, GitLab, Bitbucket, or self-hosted HTTPS Git URL.</p>
  {token_field}
  <label for='name'>Label <span class='muted'>(optional)</span></label>
  <input id='name' name='name' placeholder='e.g. thesis'
         value='{html.escape(name, quote=True)}'>
  <button type='submit'>Connect securely</button>
</form>
{note}""",
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
  <p class='muted'>You can close this tab and go back to your AI assistant, try
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


def render_manage_projects(code: str, projects, *, email: str = "", error: str | None = None) -> str:
    """The 'update the list of projects' form: add by URL / remove. No token."""
    err_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    rows = ""
    for p in projects:
        rows += (
            "<div class='proj-row'><span><b>"
            f"{html.escape(p.name)}</b><br><small class='muted'>{html.escape(p.project_id)}</small></span>"
            "<form method='post' action='/projects' style='margin:0'>"
            f"<input type='hidden' name='code' value='{html.escape(code, quote=True)}'>"
            "<input type='hidden' name='action' value='remove'>"
            f"<input type='hidden' name='project_id' value='{html.escape(p.project_id, quote=True)}'>"
            "<button class='btn-danger' type='submit'>Remove</button></form></div>"
        )
    if not rows:
        rows = "<p class='muted'>No projects connected yet.</p>"
    codeq = html.escape(code, quote=True)
    return _page(
        f"Manage projects · {BRAND}",
        f"""{_brand_header()}
<h1>Your projects</h1>
<p class='muted'>The AI can only touch the projects listed here, nothing else in
  your Overleaf, GitHub, or GitLab account. Add or remove any time. Overleaf
  reuses your saved token; GitHub, GitLab, and Bitbucket need that repo's token.</p>
{err_html}
<div class='proj-list'>{rows}</div>
<form method='post' action='/projects' autocomplete='off'>
  <input type='hidden' name='code' value='{codeq}'>
  <input type='hidden' name='action' value='add'>
  <label for='overleaf_url'>Add a repository link</label>
  <input id='overleaf_url' name='overleaf_url' inputmode='url'
         placeholder='{html.escape(_REPO_PLACEHOLDER, quote=True)}' required>
  <label for='token'>Access token
    <span class='muted'>(required for GitHub / GitLab / Bitbucket)</span></label>
  <input id='token' name='token' type='password'
         placeholder='leave blank to reuse your Overleaf token'>
  <label for='name'>Label <span class='muted'>(optional)</span></label>
  <input id='name' name='name' placeholder='e.g. thesis'>
  <button type='submit'>Add project</button>
</form>
<div class='note'>Need to change or revoke your Overleaf token?
  <a href='/token?code={codeq}'>Manage your token →</a></div>""",
    )


def render_token_form(code: str, has_token: bool, *, email: str = "", error: str | None = None) -> str:
    """The 'change or revoke token' form. Token only."""
    err_html = f"<div class='error'>{html.escape(error)}</div>" if error else ""
    codeq = html.escape(code, quote=True)
    revoke = ""
    if has_token:
        revoke = f"""<form method='post' action='/token' style='margin-top:12px'>
  <input type='hidden' name='code' value='{codeq}'>
  <input type='hidden' name='action' value='revoke'>
  <button class='btn-danger' type='submit'>Revoke token</button></form>"""
    verb = "Update" if has_token else "Add"
    return _page(
        f"Your token · {BRAND}",
        f"""{_brand_header()}
<h1>Your Overleaf token</h1>
<p class='muted'>{verb} the Git token the AI uses to reach your projects. It's
  encrypted and never shown in the chat.</p>
{err_html}
<form method='post' action='/token' autocomplete='off'>
  <input type='hidden' name='code' value='{codeq}'>
  <input type='hidden' name='action' value='set'>
  <label for='token'>Overleaf Git token</label>
  <input id='token' name='token' type='password' placeholder='olp_…' required>
  <p class='hint'>Create one in Overleaf → Account Settings →
     <a href='https://www.overleaf.com/user/settings' target='_blank' rel='noopener'>Git Integration</a>.</p>
  <button type='submit'>Save token</button>
</form>
{revoke}
<div class='note'>🔒 Your token is encrypted before it touches disk and is never
  written to the chat. Revoking removes the AI's access until you add one again.</div>""",
    )


def render_landing() -> str:
    return _page(
        BRAND,
        f"""{_brand_header()}
<h1>{html.escape(TAGLINE)}</h1>
<p class='muted'>{BRAND} is a connector that lets Claude or ChatGPT read and edit your real
   Overleaf projects over Overleaf's Git integration — and, the same way, your
   GitHub, GitLab, Bitbucket, or self-hosted Git LaTeX repositories.</p>
<div class='note'>To get started, add {BRAND} as a connector in Claude or ChatGPT, then run
   <b>start_connect</b> to link a project. There's nothing to configure on this
   page directly.</div>""",
    )
