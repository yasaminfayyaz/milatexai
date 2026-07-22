"""Configuration loading for LeafBridge Phase 1 (local, single-user).

In Phase 1 there is no database and no auth. Configuration comes from two places:

* Environment variables (optionally via a ``.env`` file) for global settings.
* A local ``projects.json`` file mapping friendly project names to an Overleaf
  project URL and a Git authentication token.

``projects.json`` is git-ignored and never leaves this machine except as HTTPS
requests to ``git.overleaf.com`` using your own token.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

try:  # optional convenience: load a local .env if python-dotenv is installed
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is optional
    pass


# A Mongo ObjectId — Overleaf project ids are 24 lowercase hex characters.
_OBJECT_ID = re.compile(r"[0-9a-f]{24}", re.IGNORECASE)

# Overleaf's default Git bridge host.
GIT_HOST = "git.overleaf.com"


class ConfigError(Exception):
    """Raised when configuration is missing or malformed."""


def extract_project_id(url_or_id: str) -> str:
    """Pull the 24-hex Overleaf project id out of whatever the user pasted.

    Accepts a raw id, a normal project URL
    (``https://www.overleaf.com/project/<id>``), or a Git URL
    (``https://git.overleaf.com/<id>``). Raises ``ConfigError`` if no id is
    found — notably, read/write *share* links such as
    ``https://www.overleaf.com/1234567890abcdef`` do **not** contain the project
    id and cannot be used with Git; the user must use the ``/project/<id>`` URL.
    """
    if not url_or_id:
        raise ConfigError("Empty project url/id.")

    candidate = url_or_id.strip()

    # If a bare id was given, accept it directly.
    if _OBJECT_ID.fullmatch(candidate):
        return candidate.lower()

    # Prefer the id that follows an explicit marker ("/project/", the git host,
    # or a self-hosted "/git/" path). Overleaf Cloud ids are 24-hex ObjectIds,
    # but we accept a broader charset after an explicit marker to tolerate
    # self-hosted Server Pro. The bare fallback stays strict (24-hex) so we don't
    # mistake a shorter read/write *share* token for a project id.
    m = re.search(r"/project/([0-9a-zA-Z]{6,40})", candidate)
    if not m:
        m = re.search(r"git\.overleaf\.com/([0-9a-zA-Z]{6,40})", candidate)
    if not m:
        m = re.search(r"/git/([0-9a-zA-Z]{6,40})", candidate)
    if not m:
        # Fall back to the first ObjectId-looking token anywhere in the string.
        m = _OBJECT_ID.search(candidate)
    if not m:
        raise ConfigError(
            "Could not find a 24-character Overleaf project id in "
            f"{url_or_id!r}. Use your project URL of the form "
            "https://www.overleaf.com/project/<id> (a read/write share link "
            "will not work with Git)."
        )
    # group(1) when a labelled pattern matched, else group(0) for the fallback.
    return m.group(m.lastindex or 0).lower()


# -- multi-provider identity parsing + SSRF trust boundary -------------------
#
# Overleaf stays the DEFAULT. These additions let a hosted user connect a
# GitHub / GitLab / Bitbucket / self-hosted HTTPS Git repo too — "at the end of
# the day it's just Git", so the git worker is unchanged; only the identity
# parsing and the SSRF guard live here. ``extract_project_id`` above is left
# untouched so every Overleaf path behaves byte-for-byte as before.

# Hosts allowed as remote Git origins for the HOSTED service by default.
# Self-hosters opt additional hosts in via LEAFBRIDGE_GIT_HOST_ALLOWLIST.
_DEFAULT_GIT_HOST_ALLOWLIST = frozenset(
    {
        "github.com",
        "www.github.com",
        "gitlab.com",
        "bitbucket.org",
        "git.overleaf.com",
    }
)


@dataclass(frozen=True)
class RepoTarget:
    """The parsed identity of a repository the user pasted.

    ``clone_url`` is ``None`` for Overleaf (the git worker synthesises the
    default ``https://git.overleaf.com/<id>`` URL); for every other provider it
    is the tokenless HTTPS clone URL. ``project_key`` is a filesystem-safe,
    stable id used as the stored ``Project.project_id`` (and the clone dir name).
    """

    provider: str  # "overleaf" | "github" | "gitlab" | "bitbucket" | "git"
    clone_url: str | None
    project_key: str
    git_username: str
    default_name: str


def _git_host_allowlist() -> set[str]:
    """The effective host allowlist (built-ins + env), read fresh each call so a
    self-hoster's ``LEAFBRIDGE_GIT_HOST_ALLOWLIST`` change takes effect at once."""
    hosts = {h.lower() for h in _DEFAULT_GIT_HOST_ALLOWLIST}
    for extra in os.environ.get("LEAFBRIDGE_GIT_HOST_ALLOWLIST", "").split(","):
        extra = extra.strip().lower()
        if extra:
            hosts.add(extra)
    return hosts


def _host_allowed(host: str) -> bool:
    allow = _git_host_allowlist()
    if host in allow:
        return True
    # Overleaf Cloud is one account across every subdomain, so *.overleaf.com is
    # always trusted.
    return host == "overleaf.com" or host.endswith(".overleaf.com")


def validate_remote_git_url(url: str) -> None:
    """Reject anything unsafe to hand to git as a remote (raises ``ConfigError``).

    This is the SSRF trust boundary for the HOSTED service: only ``https://``
    URLs, without embedded credentials, to a non-IP host on the allowlist are
    accepted. The local single-user ``load_settings`` path deliberately does NOT
    route through here (it still allows ``file://`` remotes for tests).
    """
    if not url or not isinstance(url, str):
        raise ConfigError("Empty git url.")
    parts = urlsplit(url.strip())
    if parts.scheme.lower() != "https":
        raise ConfigError(
            "Only https:// Git URLs are allowed "
            f"(got {parts.scheme or 'no'}:// scheme)."
        )
    # Embedded credentials (user:pass@host) are never allowed — the token is
    # injected per-request by ProjectConfig.authed_url, never persisted in a URL.
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ConfigError("Git URL must not embed credentials (user:pass@host).")
    host = (parts.hostname or "").lower()
    if not host:
        raise ConfigError("Git URL has no host.")
    if (
        host == "localhost"
        or host.endswith(".localhost")
        or host.endswith(".internal")
        or host.endswith(".local")
    ):
        raise ConfigError(f"Refusing internal host {host!r}.")
    # Reject ALL bare-IP hosts (the simplest safe rule): this covers IPv4/IPv6
    # loopback, private, link-local (incl. the 169.254.169.254 metadata IP),
    # reserved, multicast, unspecified, and IPv4-mapped IPv6 in one stroke.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass  # not an IP literal — good, it's a domain name
    else:
        raise ConfigError(f"Refusing bare IP host {host!r}; use a domain name.")
    if not _host_allowed(host):
        raise ConfigError(
            f"Host {host!r} is not on the allowed Git host list. Allowed: "
            "github.com, gitlab.com, bitbucket.org, and *.overleaf.com — add "
            "more via the LEAFBRIDGE_GIT_HOST_ALLOWLIST env var."
        )


def _slug(text: str) -> str:
    """Lowercase, collapse every run of non-alphanumerics to a single '-'."""
    s = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return s or "repo"


def _shorthash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _repo_path(path: str) -> str:
    """Normalise a URL path to ``owner/.../repo``: drop leading/trailing slashes,
    any ``/tree/…`` / ``/blob/…`` / GitLab ``/-/…`` web suffix, and a ``.git``
    suffix."""
    p = path.strip("/")
    for marker in ("/-/", "/tree/", "/blob/"):
        idx = p.find(marker)
        if idx != -1:
            p = p[:idx]
    if p[-4:].lower() == ".git":
        p = p[:-4]
    return p.strip("/")


def _two_segments(path: str, host: str) -> tuple[str, str]:
    p = _repo_path(path)
    segs = [s for s in p.split("/") if s]
    if len(segs) < 2:
        raise ConfigError(
            f"Could not parse an owner/repo from the {host} URL. Expected "
            f"https://{host}/<owner>/<repo>."
        )
    return segs[0], segs[1]


def _overleaf_target(pid: str) -> RepoTarget:
    return RepoTarget(
        provider="overleaf",
        clone_url=None,
        project_key=pid,
        git_username="git",
        default_name=pid[:8],
    )


def parse_repo_target(url_or_id: str) -> RepoTarget:
    """Parse whatever the user pasted into a :class:`RepoTarget`.

    Overleaf is the default: a bare 24-hex id or any ``*.overleaf.com`` URL maps
    to ``provider="overleaf"`` with ``clone_url=None``. GitHub / GitLab /
    Bitbucket get their canonical HTTPS clone URL and per-provider git username.
    Any other host is treated as a generic self-hosted Git remote and is only
    accepted if it passes :func:`validate_remote_git_url`.
    """
    raw = (url_or_id or "").strip()
    if not raw:
        raise ConfigError("Empty repository url/id.")

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    # A bare token (no scheme, no slash) can only be an Overleaf project id.
    if not scheme and "/" not in raw:
        return _overleaf_target(extract_project_id(raw))
    # Give scheme-less "host/owner/repo" input a host by assuming https; the
    # clone URLs we build below are always https regardless.
    if not scheme:
        parts = urlsplit("https://" + raw)
    host = (parts.hostname or "").lower()
    path = parts.path or ""

    # -- Overleaf (the default) --------------------------------------------
    # Exact host match (not a substring) so a look-alike like
    # ``overleaf.com.evil.com`` is NOT classified as Overleaf.
    if host == "overleaf.com" or host.endswith(".overleaf.com"):
        return _overleaf_target(extract_project_id(raw))

    # -- GitHub ------------------------------------------------------------
    if host in ("github.com", "www.github.com"):
        owner, repo = _two_segments(path, "github.com")
        return RepoTarget(
            provider="github",
            clone_url=f"https://github.com/{owner}/{repo}.git",
            # Fold a hash of the canonical (case-normalised) identity into the key
            # so distinct repos that _slug collapses together (my-lib / my_lib /
            # my.lib) stay distinct, while GitHub's case-insensitivity dedups.
            project_key=_slug(f"github-{owner}-{repo}")
            + "-"
            + _shorthash(f"github.com/{owner.lower()}/{repo.lower()}"),
            git_username="x-access-token",
            default_name=repo,
        )

    # -- GitLab (subgroup paths preserved) ---------------------------------
    if host == "gitlab.com":
        sub = _repo_path(path)
        segs = [s for s in sub.split("/") if s]
        if len(segs) < 2:
            raise ConfigError(
                "Could not parse a group/repo from the gitlab.com URL. Expected "
                "https://gitlab.com/<group>[/<subgroup>…]/<repo>."
            )
        sub = "/".join(segs)
        return RepoTarget(
            provider="gitlab",
            clone_url=f"https://gitlab.com/{sub}.git",
            project_key="gitlab-"
            + _slug(sub)
            + "-"
            + _shorthash("gitlab.com/" + sub.lower()),
            git_username="oauth2",
            default_name=segs[-1],
        )

    # -- Bitbucket ---------------------------------------------------------
    if host == "bitbucket.org":
        workspace, repo = _two_segments(path, "bitbucket.org")
        return RepoTarget(
            provider="bitbucket",
            clone_url=f"https://bitbucket.org/{workspace}/{repo}.git",
            project_key=_slug(f"bitbucket-{workspace}-{repo}")
            + "-"
            + _shorthash(f"bitbucket.org/{workspace.lower()}/{repo.lower()}"),
            git_username="x-token-auth",
            default_name=repo,
        )

    # -- generic self-hosted HTTPS Git remote ------------------------------
    clean = _repo_path(path)
    clone_url = f"https://{host}/{clean}.git" if clean else f"https://{host}"
    validate_remote_git_url(clone_url)  # allowlist is the gate for generic hosts
    return RepoTarget(
        provider="git",
        clone_url=clone_url,
        project_key="git-" + _shorthash(host + "/" + clean),
        git_username="git",
        default_name=(clean.rsplit("/", 1)[-1] if clean else host),
    )


@dataclass(frozen=True)
class ProjectConfig:
    """A single Overleaf project the user has connected locally."""

    name: str
    project_id: str
    token: str
    git_username: str = "git"
    # Optional override of the clone URL: for self-hosted Overleaf Server Pro
    # (https://<site>/git/<id>) or for local testing (a file path / file:// URL).
    git_url: str | None = None

    @property
    def clone_url(self) -> str:
        """The tokenless clone URL (safe to log / store as remote)."""
        return self.git_url or f"https://{GIT_HOST}/{self.project_id}"

    def authed_url(self) -> str:
        """The clone URL with embedded credentials, used per fetch/push.

        We pass this explicitly on each network call rather than persisting it
        into ``.git/config`` so the token never lingers on disk in the clone.
        Credentials are only injected for HTTPS URLs; local/file remotes (used in
        tests) are returned untouched.
        """
        base = self.clone_url
        if not base.startswith("https://"):
            return base
        from urllib.parse import quote

        user = quote(self.git_username, safe="")
        secret = quote(self.token, safe="")
        rest = base[len("https://") :]
        return f"https://{user}:{secret}@{rest}"

    def redacted(self) -> dict:
        """A log-safe view with the token removed."""
        return {
            "name": self.name,
            "project_id": self.project_id,
            "git_username": self.git_username,
        }


@dataclass
class Settings:
    """Global LeafBridge settings plus the loaded projects."""

    projects: list[ProjectConfig] = field(default_factory=list)
    data_dir: Path = field(default_factory=lambda: default_data_dir())
    host: str = "127.0.0.1"
    port: int = 8000

    def get_project(self, ref: str | None) -> ProjectConfig:
        """Resolve a project by name or id.

        If ``ref`` is ``None`` and exactly one project is configured, return it
        (so single-project users never have to name it). Otherwise require an
        explicit reference.
        """
        if ref is None:
            if len(self.projects) == 1:
                return self.projects[0]
            names = ", ".join(p.name for p in self.projects) or "(none)"
            raise ConfigError(
                "Multiple projects are configured; specify which one by name "
                f"or id. Available: {names}."
            )
        ref_norm = ref.strip().lower()
        for p in self.projects:
            if p.name.lower() == ref_norm or p.project_id == ref_norm:
                return p
        # Maybe they passed a full URL.
        try:
            pid = extract_project_id(ref)
        except ConfigError:
            pid = None
        if pid:
            for p in self.projects:
                if p.project_id == pid:
                    return p
        names = ", ".join(p.name for p in self.projects) or "(none)"
        raise ConfigError(f"No connected project matches {ref!r}. Available: {names}.")


def default_data_dir() -> Path:
    """Where to keep git clone caches.

    Defaults outside the project (and outside OneDrive) so a synced folder isn't
    churned by clone data. Override with ``LEAFBRIDGE_DATA_DIR``.
    """
    override = os.environ.get("LEAFBRIDGE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "LeafBridge" / "cache"
    return Path.home() / ".cache" / "leafbridge"


def _config_path() -> Path:
    override = os.environ.get("LEAFBRIDGE_CONFIG")
    if override:
        return Path(override).expanduser()
    # Default: projects.json next to the repo root (two levels up from this file
    # is the repo root: leafbridge/config.py -> repo/).
    return Path(__file__).resolve().parent.parent / "projects.json"


def load_settings() -> Settings:
    """Load global settings and the connected projects from disk."""
    cfg_path = _config_path()
    if not cfg_path.exists():
        raise ConfigError(
            f"No projects file found at {cfg_path}. Copy projects.example.json "
            "to projects.json and fill in your Overleaf project URL and Git token."
        )

    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"projects.json is not valid JSON: {exc}") from exc

    entries = raw.get("projects") if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not entries:
        raise ConfigError(
            "projects.json must contain a non-empty 'projects' array. "
            "See projects.example.json."
        )

    projects: list[ProjectConfig] = []
    seen_names: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"projects[{i}] must be an object.")
        git_url = entry.get("git_url") or None
        url = entry.get("url") or entry.get("project_id") or git_url or ""
        token = entry.get("token") or ""
        if not token or "PASTE" in token:
            raise ConfigError(
                f"projects[{i}] ({entry.get('name', '?')}) is missing a real Git "
                "token. Create one in Overleaf: Account Settings > Git Integration."
            )
        project_id = extract_project_id(url)
        name = entry.get("name") or project_id[:8]
        if name.lower() in seen_names:
            raise ConfigError(f"Duplicate project name {name!r} in projects.json.")
        seen_names.add(name.lower())
        projects.append(
            ProjectConfig(
                name=name,
                project_id=project_id,
                token=token,
                git_username=entry.get("git_username") or "git",
                git_url=git_url,
            )
        )

    port_raw = os.environ.get("LEAFBRIDGE_PORT", "8000")
    try:
        port = int(port_raw)
    except ValueError as exc:
        raise ConfigError(f"LEAFBRIDGE_PORT must be a number, got {port_raw!r}.") from exc

    return Settings(
        projects=projects,
        data_dir=default_data_dir(),
        host=os.environ.get("LEAFBRIDGE_HOST", "127.0.0.1"),
        port=port,
    )
