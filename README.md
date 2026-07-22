# MiLatexAI

**Edit your real Overleaf projects, plus GitHub, GitLab, Bitbucket, and self-hosted Git repositories, by talking to Claude or ChatGPT. No copy-paste, no downloading files, and every change lands on your default branch as a real Git commit.**

[milatexai.com](https://milatexai.com) · Works with Claude and ChatGPT · Open source (AGPL-3.0)

MiLatexAI is a hosted, remote [MCP](https://modelcontextprotocol.io) server that connects your AI assistant to your Overleaf projects and any GitHub, GitLab, Bitbucket, or allowlisted self-hosted Git repository. Ask *"tighten my introduction"* or *"add a related-work paragraph citing Smith 2021"* and the edit is committed and pushed to the repository's **default branch** instantly, visible in your Git history where you can review the diff or revert it. (There's no pull-request flow or branch picker yet.)

> The internal Python package is still named `leafbridge`.

---

## Use it (hosted, recommended)

Nothing to install. In Claude or ChatGPT, add MiLatexAI as a **custom connector**:

1. **Add the connector** using the URL **`https://milatexai.com/mcp`**.
   - **Claude:** Settings, Connectors, Add custom connector, paste the URL, sign in.
   - **ChatGPT:** Settings, Connectors, enable Developer mode, Add custom connector, paste the URL, authorize. Works on ChatGPT including its free plan.
2. **Connect a project.** Ask the assistant to connect a project (or run `start_connect`). You get a one-time link to a secure web form where you paste your repository URL and Git access token. It never appears in the chat and is encrypted at rest.
3. **Talk to your paper.** "List my files," "read the methods section," "why won't it compile," "rewrite this paragraph."

Full setup guide: **[milatexai.com/#get-started](https://milatexai.com/#get-started)**

---

## Requirement: a repository you can push to

You connect a repository by pasting its URL and an access token. What that takes depends on the host:

**Overleaf — Git integration (a paid Overleaf feature).** MiLatexAI works through Overleaf's **Git integration**, which is a **premium (paid) feature** on Overleaf. To use it on your own Overleaf projects you need one of:

- An Overleaf **paid plan** (Standard or Professional), or
- A project shared with you from a **premium or group/institutional** account that includes Git integration.

Quick test: if you can create a Git token under **Overleaf, Account Settings, Git Integration**, you are good to go. Free-only Overleaf accounts cannot create Git tokens.

**GitHub, GitLab, Bitbucket, or self-hosted Git — no paid plan needed.** Any repository you can push to works with a free access token: a **GitHub** fine-grained PAT with **Contents read/write**, a **GitLab** project or personal access token, or a **Bitbucket** app password. Self-hosted HTTPS Git repos are supported when allowlisted.

You will also need **Claude or ChatGPT**. Their free tiers work fine.

---

## Pricing

- **Free:** 1 connected project, 25 write-commits per month, unlimited reads. Runs on spare capacity (best-effort).
- **Pro, $8.99/mo:** unlimited projects, unlimited write-commits, guaranteed access.

Reads are always free and unlimited. If you connect Overleaf, its own subscription is separate and billed by Overleaf; GitHub, GitLab, and Bitbucket need only a free access token. Manage billing inside your assistant or at [milatexai.com/account](https://milatexai.com/account).

---

## What it does

| Tool | What it does | Write |
|---|---|---|
| `list_projects`, `list_files` | List your projects, or a project's files | No |
| `read_file`, `get_sections`, `read_section` | Read files and navigate LaTeX structure | No |
| `check_compile` | Build with a bundled LaTeX engine (Tectonic) and report the exact errors | No |
| `get_history` | Recent commits on the default branch | No |
| `edit_file`, `write_file`, `delete_file`, `upload_file` | Change files, each an immediate Git commit and push | Yes |

Only **writes** count toward the monthly limit; reads are always free. Every write commits *and* pushes immediately to the default branch and returns the commit hash, auditable in your repository's history. Paths are validated (no traversal), edits require an exact unique match, and the engine always pulls before pushing and never force-pushes.

---

## Security and privacy

- Your **Git access token** is entered on a secure web form, **encrypted at rest**, and **never written to the chat**.
- MiLatexAI touches **only the projects you explicitly connect**, nothing else in your account.
- We **do not store your document contents**. We keep only your account email, your encrypted token, and a monthly commit counter.
- The full connector is **open source** (AGPL-3.0), so you can audit exactly what it does.

MiLatexAI is not affiliated with, endorsed by, or sponsored by Overleaf or Digital Science, GitHub, GitLab, Bitbucket, Anthropic, or OpenAI. Overleaf, GitHub, GitLab, Bitbucket, Claude, and ChatGPT are trademarks of their respective owners, used only to describe compatibility.

---

## Self-host

MiLatexAI is AGPL-3.0, so you can run the server yourself.

```bash
python -m venv .venv
. .venv/Scripts/activate          # or: source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q                # test suite
```

The hosted service runs `leafbridge.asgi:app` (Streamable HTTP MCP at `/mcp`) on Azure Container Apps, with WorkOS AuthKit for authentication and Stripe for billing. Self-hosted Overleaf Server Pro is supported via a `git_url` override on a project. Running a modified version as a network service requires publishing your changes (AGPL).

---

## Status

- **Live** at [milatexai.com](https://milatexai.com): hosted multi-user server, WorkOS sign-in, per-user encrypted tokens, server-side compile checks (Tectonic), Stripe billing, and website sign-in for managing your subscription.
- Roadmap: connector-directory listings, multi-file smart edits, team plans.

---

## License

[AGPL-3.0-or-later](LICENSE). You may self-host freely; running a modified version as a network service requires publishing your changes.
