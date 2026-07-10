# Research notes — verified API facts (as of 2026-07-10)

These are the load‑bearing external facts LeafBridge is built on, verified against
current docs. Re‑check anything version‑sensitive before Phase 2/3.

## Overleaf Git bridge

- **Clone URL:** `https://git.overleaf.com/<projectId>` (Cloud). Self‑hosted
  Server Pro: `https://<site>/git/<projectId>`.
- **Auth:** username is the literal **`git`**, the **token is the password**.
  Embedded form `https://git:<token>@git.overleaf.com/<id>` works (standard Git
  credential‑in‑URL behavior; undocumented but reliable). Tokens look like `olp_…`.
- **Token scope:** account‑wide (one token → all your projects). Expire after
  **1 year**, **max 10** per account.
- **Tier:** Git integration is a **premium/paid** feature on Overleaf Cloud;
  **not available on free accounts** (institutional site licenses often include it).
- **Branch:** new repos default to **`main`**, older repos to **`master`** — detect,
  never hardcode. (LeafBridge detects via `symbolic-ref`.)
- **Concurrency:** the web editor **auto‑commits**, so the remote advances on its
  own. Always **pull then push**; non‑fast‑forward pushes are rejected;
  **force‑push is not supported** (use Overleaf history "labels" instead).
- **Limits/politeness:** no published numeric rate limit, but avoid automated
  polling; back off on HTTP 429. Project size caps (~7 MB editable, 2 MB/text file);
  raise `http.postBuffer` for large pushes. No symlinks, no Git‑LFS.
- Sources: docs.overleaf.com → *Integrations → Git integration* (+ *authentication‑tokens*,
  *advanced‑git‑operations*); github.com/overleaf/overleaf wiki.

## Claude custom connectors (remote MCP + OAuth) — for Phase 2

- **Transport:** Streamable HTTP, single endpoint (convention `/mcp`; POST+GET,
  DELETE ends a session). Legacy standalone HTTP+SSE is deprecated.
- **OAuth 2.1** required: **PKCE (S256)** always; **DCR (RFC 7591)** supported
  (alternatives: CIMD, Anthropic‑held creds, static client). AS metadata
  (RFC 8414 `/.well-known/oauth-authorization-server`) and **protected‑resource
  metadata (RFC 9728 `/.well-known/oauth-protected-resource`)** required; `resource`
  must match the server URL; if multiple auth servers are listed Claude uses the first.
- **Callback URL (hosted Claude):** `https://claude.ai/api/mcp/auth_callback`.
  Claude Code uses an RFC 8252 loopback redirect instead.
- **Lazy auth pattern:** `initialize`, `tools/list`, and public tool calls can be
  unauthenticated; a protected `tools/call` must fail the **HTTP request** with
  **`401` + `WWW-Authenticate: Bearer … resource_metadata=…`** to trigger the sign‑in
  card. Returning a 200 with an "please sign in" error body does **not** trigger OAuth.
- **Spec version:** `2025-11-25` is current; a `2026-07-28` release candidate (goes
  stateless, removes the `initialize` handshake) is imminent — **re‑verify after it
  ratifies.**
- **Directory listing** requires a Team/Enterprise org with directory access; submit
  via `claude.ai/admin-settings/directory/submissions/new`.
- Sources: modelcontextprotocol.io (transports, authorization, 2025-11-25 changelog);
  claude.com/docs/connectors (authentication, lazy-authentication, directory).

## ChatGPT connectors (search/fetch contract)

- Deep‑research / company‑knowledge surfaces require exactly two tools:
  - **`search(query: string)` → `{ "results": [ {"id","title","url"} ] }`**
  - **`fetch(id: string)` → `{ "id","title","text","url","metadata" }`**
  - `url` should be a public, user‑openable link to enable citations; else `""`.
- **Developer mode / regular chat allow arbitrary tools** (including writes), so
  LeafBridge's full tool set works there. Custom connectors need a paid plan
  (Plus/Pro read‑only; Business/Enterprise/Edu for writes).
- Transport: Streamable HTTP (SSE legacy). OAuth 2.1 for authenticated servers.
- Sources: developers.openai.com (MCP guide, Apps SDK); gofastmcp.com/integrations/chatgpt;
  help.openai.com (connectors).

## FastMCP (the framework we use)

- **`fastmcp==3.x`** (built against 3.4.4), Python **3.10–3.13**, Apache‑2.0.
- Server: `from fastmcp import FastMCP; mcp = FastMCP(name=…, instructions=…, version=…, auth=…)`;
  tools via **`@mcp.tool`** (typed args + docstring drive the schema).
- Run HTTP: `mcp.run(transport="http", host=…, port=…)`; production ASGI:
  **`app = mcp.http_app()`** (Starlette app; MCP mounts at **`/mcp/`**) served by uvicorn
  → containerizes cleanly for **Azure Container Apps**.
- Auth (resource server): `FastMCP(auth=…)` with `JWTVerifier` (JWKS/issuer/audience),
  `RemoteAuthProvider` (adds protected‑resource discovery + DCR when the IdP supports it),
  or `OAuthProxy` (for non‑DCR IdPs). Prebuilt providers incl. Auth0, Azure/Entra, WorkOS,
  Descope, Google, GitHub. FastMCP does **not** mint tokens — an external IdP is the auth server.
- Per‑user identity inside a tool: `from fastmcp.server.dependencies import get_access_token`
  → `AccessToken.claims` (e.g. `sub`, `email`) is the per‑user lookup key.
- Sources: pypi.org/project/fastmcp; gofastmcp.com (quickstart, running-server, auth/*).
