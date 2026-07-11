"""Configuration loading for LeafBridge Phase 1 (local, single-user).

In Phase 1 there is no database and no auth. Configuration comes from two places:

* Environment variables (optionally via a ``.env`` file) for global settings.
* A local ``projects.json`` file mapping friendly project names to an Overleaf
  project URL and a Git authentication token.

``projects.json`` is git-ignored and never leaves this machine except as HTTPS
requests to ``git.overleaf.com`` using your own token.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

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
