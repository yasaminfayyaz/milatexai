# LeafBridge

**Edit your Overleaf projects straight from Claude and ChatGPT — no installs, no cloning, no copy‑paste.**

LeafBridge is a remote [MCP](https://modelcontextprotocol.io) server that connects an AI assistant to your Overleaf projects through Overleaf's own Git bridge. Ask *"fix the grammar in my introduction"* or *"add a related‑work paragraph citing Smith 2021"* and the change is committed and pushed to Overleaf instantly, visible in your project's history.

> **Status: Phase 1 (local engine) — working and tested.** You run the server on your own machine and add its URL to Claude/ChatGPT. The hosted, multi‑user, billed service (Phases 2–3) is on the roadmap below.

---

## ⚠️ Requirement: Overleaf Git integration

LeafBridge works through Overleaf's **Git integration**, which is a **paid/premium feature** on Overleaf Cloud. You need one of:

- An Overleaf **paid plan** (Standard/Professional), **or**
- Access through a **university/institutional site license** that includes Git integration (many do — check whether your Overleaf shows *Account Settings → Git Integration*).

Free Overleaf accounts cannot create Git tokens, so LeafBridge can't connect to them.

---

## Quick start (local)

**Prerequisites:** Python 3.10+ and Git.

```bash
# 1. Install dependencies (from the repo root)
python -m venv .venv
.venv\Scripts\activate           # Windows PowerShell:  .venv\Scripts\Activate.ps1
#  source .venv/bin/activate      # macOS/Linux
pip install -r requirements.txt

# 2. Configure your project(s)
copy projects.example.json projects.json     #  cp on macOS/Linux
#   then edit projects.json (see below)

# 3. Run it
python -m leafbridge
#   -> MCP endpoint: http://127.0.0.1:8000/mcp/
```

### Getting your Overleaf Git token

1. In Overleaf, open **Account Settings → Git Integration** (also called *Project Synchronization*).
2. Click **Create Token** / **Add another token** and copy the `olp_…` token (you only see it once).
3. Grab your project link: open the project and copy the URL — it looks like
   `https://www.overleaf.com/project/aaaaaaaaaaaaaaaaaaaaaaaa`.

### `projects.json`

```json
{
  "projects": [
    {
      "name": "thesis",
      "url": "https://www.overleaf.com/project/aaaaaaaaaaaaaaaaaaaaaaaa",
      "token": "olp_your_token_here"
    }
  ]
}
```

`projects.json` is **git‑ignored** and never leaves your machine except as HTTPS requests to `git.overleaf.com`. You can list several projects; tools default to the only project when you have just one.

### Connect it to your AI

- **Claude** (web/desktop): *Settings → Connectors → Add custom connector* → paste `http://127.0.0.1:8000/mcp/`.
- **ChatGPT** (Plus/Pro/Business/Edu): enable *Developer mode* under *Settings → Apps/Connectors*, then add the same URL. (Write actions require a Business/Enterprise/Edu workspace; read‑only works on Plus/Pro.)

Then just talk to the assistant about your paper.

---

## Tools

| Tool | What it does | Metered\* |
|---|---|---|
| `list_projects` | Show your connected projects | No |
| `list_files` | List `.tex`, `.bib`, `.cls`, `.sty`, … files | No |
| `read_file` | Read a file (with line numbers) | No |
| `get_sections` | Parse a `.tex` file's section outline | No |
| `read_section` | Return one section by title | No |
| `edit_file` | Exact‑string replacement → commit + push | **Yes** |
| `write_file` | Create/overwrite a file → commit + push | **Yes** |
| `delete_file` | Delete a file → commit + push | **Yes** |
| `upload_file` | Add/replace a **binary** file (image, PDF) → commit + push | **Yes** |
| `check_compile` | Build the project with a local LaTeX engine and report errors (optional; needs Tectonic) | No |
| `get_history` | Recent commits from Overleaf's history | No |
| `search` | Keyword search across project files | No |
| `fetch` | Return a file's full text by id (ChatGPT contract) | No |

\* Only *writes* (pushes to Overleaf) are metered in the future hosted plan; reading is always free. In Phase 1 there is no metering at all.

**Every write commits *and* pushes immediately** and returns the commit hash — there's no separate "save," and the change is auditable in Overleaf's history. All file paths are validated against the project directory (no traversal), edits require an exact, unique match, and the engine always pulls before pushing and never force‑pushes.

---

## Architecture (Phase 1)

```
Claude / ChatGPT
      │  MCP over Streamable HTTP  (http://127.0.0.1:8000/mcp/)
      ▼
LeafBridge (FastMCP, Python)
      ├─ tools: list / read / sections / edit / write / history / search / fetch
      ├─ git worker: shallow clone cache, per‑project lock, pull‑then‑push
      └─ projects.json (your links + tokens, local only)
      │  HTTPS + your Overleaf Git token
      ▼
Overleaf Git bridge  (git.overleaf.com/<projectId>)
```

The clone cache is disposable (default: `%LOCALAPPDATA%\LeafBridge\cache`) and re‑created on demand. See [`docs/RESEARCH-NOTES.md`](docs/RESEARCH-NOTES.md) for the verified Overleaf / MCP / FastMCP API details this is built on.

---

## Development

```bash
pip install -e ".[dev]"

python -m pytest tests/test_units.py -q     # fast, no network
python tests/it_gitflow.py                  # full clone→edit→push flow vs a local fake remote
python tests/http_smoke.py                  # against a running `python -m leafbridge`
```

Settings via environment (optional, or in a `.env`): `LEAFBRIDGE_HOST`, `LEAFBRIDGE_PORT`, `LEAFBRIDGE_DATA_DIR`, `LEAFBRIDGE_CONFIG`.

Self‑hosted Overleaf Server Pro (or local testing) is supported by adding a `git_url` override to a project entry.

---

## Roadmap

- **Phase 1 — local engine** ✅ *(this repo)* — read/edit/write tools over the Git bridge, tested end‑to‑end.
- **Phase 2 — hosted + auth** — deploy to Azure Container Apps; OAuth 2.1 (PKCE + DCR) so users connect via `claude.ai`'s custom‑connector flow; per‑user encrypted token storage.
- **Phase 3 — billing** — Stripe Checkout, usage metering (writes only), free tier + Pro.
- **Phase 4 — publish** — docs site, privacy policy, Connectors Directory submission.
- **Later** — compile checks (chktex), multi‑file smart edits, team plans.

---

## License

[AGPL‑3.0‑or‑later](LICENSE). You may self‑host freely; running a modified version as a network service requires publishing your changes.
