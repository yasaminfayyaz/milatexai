"""LeafBridge MCP server (Phase 1: local, single user, no auth).

Exposes the read / structure / edit / write / history / search tools described in
the design plan, backed by the Overleaf Git bridge. Run it with::

    python -m leafbridge            # serves Streamable HTTP at http://127.0.0.1:8000/mcp/

then add ``http://127.0.0.1:8000/mcp/`` as a custom connector in Claude, or point
ChatGPT developer mode at the same URL.
"""

from __future__ import annotations

import base64
import difflib
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from . import __version__
from .config import ConfigError, ProjectConfig, Settings, load_settings
from .files import (
    PathError,
    list_source_files,
    number_lines,
    read_text,
    safe_join,
    search_files,
    write_bytes_exact,
    write_text_exact,
)
from .git_worker import GitError, GitWorker, PushConflict
from . import latex
from . import texcompile

INSTRUCTIONS = """\
LeafBridge edits the user's real Overleaf projects over Overleaf's Git bridge.

Key facts to work well:
- Every write tool (edit_file, write_file, delete_file) commits AND pushes
  immediately, so changes appear in Overleaf right away and in its version
  history. There is no separate "save". Treat writes as live edits to the
  user's paper.
- Before editing, read the file (read_file) or a section (read_section) so your
  edit_file old_string matches the current text exactly.
- edit_file replaces one exact, unique occurrence of old_string. If it is not
  unique, include more surrounding context to disambiguate.
- Reading, listing, structure, history, and search are always free/unmetered;
  prefer them liberally to ground your edits.
- If a project is not specified and the user has exactly one connected project,
  it is used automatically.
"""


class _State:
    """Lazily-loaded settings + git worker, so the server boots even before the
    user has created projects.json."""

    def __init__(self) -> None:
        self._settings: Settings | None = None
        self._worker: GitWorker | None = None

    def load(self) -> tuple[Settings, GitWorker]:
        if self._settings is None:
            try:
                self._settings = load_settings()
            except ConfigError as exc:
                raise ToolError(f"LeafBridge is not configured yet: {exc}") from exc
            self._worker = GitWorker(self._settings.data_dir)
        assert self._worker is not None
        return self._settings, self._worker

    def resolve(self, project: str | None) -> tuple[ProjectConfig, GitWorker]:
        settings, worker = self.load()
        try:
            return settings.get_project(project), worker
        except ConfigError as exc:
            raise ToolError(str(exc)) from exc


STATE = _State()


def _overleaf_url(project: ProjectConfig) -> str:
    return f"https://www.overleaf.com/project/{project.project_id}"


def _mini_diff(old: str, new: str, path: str, max_lines: int = 40) -> str:
    """A compact unified diff of a single edit, for the tool response."""
    diff = list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )
    if not diff:
        return ""
    if len(diff) > max_lines:
        diff = diff[:max_lines] + [f"… ({len(diff) - max_lines} more diff lines)"]
    return "Change applied:\n" + "\n".join(diff)


def _wrap_fs_errors(exc: Exception) -> ToolError:
    if isinstance(exc, (PathError, ConfigError)):
        return ToolError(str(exc))
    if isinstance(exc, PushConflict):
        return ToolError(str(exc))
    if isinstance(exc, GitError):
        return ToolError(f"Git operation failed: {exc}")
    return ToolError(f"Unexpected error: {exc}")


mcp = FastMCP(name="LeafBridge", instructions=INSTRUCTIONS, version=__version__)


# --------------------------------------------------------------------------- #
# Read-only tools (unmetered)
# --------------------------------------------------------------------------- #

@mcp.tool
async def list_projects() -> str:
    """List the Overleaf projects connected to this LeafBridge server."""
    settings, _ = STATE.load()
    lines = ["Connected Overleaf projects:"]
    for p in settings.projects:
        lines.append(f"- {p.name}  (id {p.project_id})  {_overleaf_url(p)}")
    return "\n".join(lines)


@mcp.tool
async def list_files(project: str | None = None, all_files: bool = False) -> str:
    """List files in a project.

    Args:
        project: Project name or id. Optional if only one project is connected.
        all_files: If true, list every file, not just LaTeX source files.
    """
    try:
        proj, worker = STATE.resolve(project)
        repo = await worker.ensure_repo(proj)
        entries = list_source_files(repo, all_files=all_files)
    except Exception as exc:
        raise _wrap_fs_errors(exc)
    if not entries:
        return f"No files found in project {proj.name!r}."
    header = f"{len(entries)} file(s) in {proj.name!r}:"
    rows = [f"- {e.path}  ({e.size} bytes)" for e in entries]
    return "\n".join([header, *rows])


@mcp.tool
async def read_file(
    path: str, project: str | None = None, with_line_numbers: bool = True
) -> str:
    """Read a file's full text content.

    Args:
        path: Project-relative path, e.g. "sections/intro.tex".
        project: Project name or id. Optional if only one project is connected.
        with_line_numbers: Prefix each line with its number (for reference only;
            edit_file matches the raw text without these prefixes).
    """
    try:
        proj, worker = STATE.resolve(project)
        repo = await worker.ensure_repo(proj)
        target = safe_join(repo, path)
        content = read_text(target)
    except Exception as exc:
        raise _wrap_fs_errors(exc)
    return number_lines(content) if with_line_numbers else content


@mcp.tool
async def get_sections(path: str, project: str | None = None) -> str:
    """Return the LaTeX sectioning outline of a .tex file (with line ranges).

    Args:
        path: Project-relative path to a .tex file.
        project: Project name or id. Optional if only one project is connected.
    """
    try:
        proj, worker = STATE.resolve(project)
        repo = await worker.ensure_repo(proj)
        content = read_text(safe_join(repo, path))
    except Exception as exc:
        raise _wrap_fs_errors(exc)
    sections = latex.find_sections(content)
    return f"Sections in {path}:\n{latex.outline(sections)}"


@mcp.tool
async def read_section(path: str, title: str, project: str | None = None) -> str:
    """Return the content of one section of a .tex file, found by its title.

    Args:
        path: Project-relative path to a .tex file.
        title: Section title to find (case-insensitive; partial matches allowed).
        project: Project name or id. Optional if only one project is connected.
    """
    try:
        proj, worker = STATE.resolve(project)
        repo = await worker.ensure_repo(proj)
        content = read_text(safe_join(repo, path))
    except Exception as exc:
        raise _wrap_fs_errors(exc)
    found = latex.find_section(content, title)
    if found is None:
        outline = latex.outline(latex.find_sections(content))
        raise ToolError(
            f"No section matching {title!r} in {path}. Available sections:\n{outline}"
        )
    section, body = found
    header = f"# {section.command}: {section.title}  (lines {section.line}-{section.end_line})"
    return f"{header}\n{number_lines(body, start=section.line)}"


@mcp.tool
async def get_history(project: str | None = None, limit: int = 10) -> str:
    """Show recent commits (from Overleaf's history) for a project.

    Args:
        project: Project name or id. Optional if only one project is connected.
        limit: How many recent commits to show (max 50).
    """
    try:
        proj, worker = STATE.resolve(project)
        return await worker.log(proj, limit=max(1, min(limit, 50)))
    except Exception as exc:
        raise _wrap_fs_errors(exc)


@mcp.tool
async def check_compile(project: str | None = None) -> str:
    """Build the project with a local LaTeX engine and report whether it compiles.

    Read-only: this does NOT push anything. Use it to confirm a paper still
    builds (e.g. after edits) and to see any hard LaTeX errors. Requires a
    Tectonic engine on the server; if none is installed it says so.

    Args:
        project: Project name or id. Optional if only one project is connected.
    """
    try:
        proj, worker = STATE.resolve(project)
        repo = await worker.ensure_repo(proj)
    except Exception as exc:
        raise _wrap_fs_errors(exc)
    main = texcompile.find_main_tex(repo)
    if not main:
        raise ToolError(
            "Could not find a root .tex file (one with \\documentclass and "
            "\\begin{document}) to compile."
        )
    res = await texcompile.compile_project(repo, main)
    if not res.available:
        return (
            f"Compile check unavailable: {res.message} "
            "(Install Tectonic to enable build verification.)"
        )
    lines = [f"{main}: {res.message}"]
    if res.warning_count:
        lines.append(f"{res.warning_count} warning(s) (typically cosmetic).")
    if not res.ok and res.errors:
        lines.append("Errors:")
        lines.extend(f"  {e}" for e in res.errors)
    elif not res.ok:
        lines.append("(No explicit error lines captured; check the full log.)")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Write tools (these are the ones a paid plan would meter in Phase 3)
# --------------------------------------------------------------------------- #

async def _apply_and_push(
    proj: ProjectConfig, worker: GitWorker, mutate, commit_message: str
) -> str:
    """Serialize per project: sync to remote, mutate on disk, commit + push."""
    async with worker.lock_for(proj):
        try:
            repo = await worker.ensure_repo(proj, sync=False)
            await worker.sync(proj, force=True)
            mutate(repo)
            result = await worker.commit_and_push(proj, commit_message)
        except Exception as exc:
            raise _wrap_fs_errors(exc)
    if not result.committed:
        return f"No change made: {result.message}"
    return (
        f"Done. Committed {result.hash} and pushed to Overleaf — the change is now "
        f"live in {proj.name!r} and visible in its history.\n{_overleaf_url(proj)}"
    )


@mcp.tool
async def edit_file(
    path: str,
    old_string: str,
    new_string: str,
    project: str | None = None,
) -> str:
    """Replace one exact, unique occurrence of old_string with new_string, then
    commit and push to Overleaf.

    Args:
        path: Project-relative path to the file to edit.
        old_string: Exact text to find. Must appear exactly once; include
            surrounding context if needed to make it unique. Do not include the
            line-number prefixes shown by read_file.
        new_string: Replacement text.
        project: Project name or id. Optional if only one project is connected.
    """
    if old_string == new_string:
        raise ToolError("old_string and new_string are identical; nothing to change.")
    proj, worker = STATE.resolve(project)

    def mutate(repo: Path) -> None:
        target = safe_join(repo, path)
        content = read_text(target)
        count = content.count(old_string)
        if count == 0:
            raise PathError(
                f"old_string was not found in {path}. Re-read the file; the text "
                "may differ from what you expected."
            )
        if count > 1:
            raise PathError(
                f"old_string appears {count} times in {path}; it must be unique. "
                "Include more surrounding context."
            )
        write_text_exact(target, content.replace(old_string, new_string, 1))

    result = await _apply_and_push(proj, worker, mutate, f"Edit {path} (via LeafBridge)")
    if result.startswith("Done"):
        diff = _mini_diff(old_string, new_string, path)
        if diff:
            result = f"{result}\n\n{diff}"
    return result


@mcp.tool
async def write_file(path: str, content: str, project: str | None = None) -> str:
    """Create a new file or overwrite an existing one, then commit and push.

    Args:
        path: Project-relative path to write.
        content: Full file content.
        project: Project name or id. Optional if only one project is connected.
    """
    proj, worker = STATE.resolve(project)

    def mutate(repo: Path) -> None:
        target = safe_join(repo, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        write_text_exact(target, content)

    return await _apply_and_push(proj, worker, mutate, f"Write {path} (via LeafBridge)")


@mcp.tool
async def delete_file(path: str, project: str | None = None) -> str:
    """Delete a file from the project, then commit and push.

    Args:
        path: Project-relative path to delete.
        project: Project name or id. Optional if only one project is connected.
    """
    proj, worker = STATE.resolve(project)

    def mutate(repo: Path) -> None:
        target = safe_join(repo, path)
        if not target.exists():
            raise PathError(f"Cannot delete {path}: it does not exist.")
        if target.is_dir():
            raise PathError(f"{path} is a directory; only files can be deleted.")
        target.unlink()

    return await _apply_and_push(proj, worker, mutate, f"Delete {path} (via LeafBridge)")


@mcp.tool
async def upload_file(
    path: str,
    content_base64: str | None = None,
    source_path: str | None = None,
    project: str | None = None,
) -> str:
    """Add or replace a BINARY file (image, PDF, …) in the project, then commit
    and push. Use this for figures such as PNGs that the text-only read_file /
    write_file tools cannot handle safely.

    Provide the bytes exactly one of two ways:

    Args:
        path: Project-relative destination, e.g. "figures/diagram.png".
        content_base64: The file's bytes, base64-encoded.
        source_path: Absolute path to a local file to read the bytes from
            (local-mode convenience only; ignored in a hosted deployment).
        project: Project name or id. Optional if only one project is connected.
    """
    if bool(content_base64) == bool(source_path):
        raise ToolError("Provide exactly one of content_base64 or source_path.")
    try:
        if content_base64:
            data = base64.b64decode(content_base64, validate=True)
        else:
            src = Path(source_path)  # type: ignore[arg-type]
            if not src.is_file():
                raise ToolError(f"source_path not found: {source_path}")
            data = src.read_bytes()
    except ToolError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"Could not read the file data: {exc}")

    proj, worker = STATE.resolve(project)

    def mutate(repo: Path) -> None:
        target = safe_join(repo, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_exact(target, data)

    return await _apply_and_push(
        proj, worker, mutate, f"Upload {path} ({len(data)} bytes, via LeafBridge)"
    )


# --------------------------------------------------------------------------- #
# ChatGPT-compatibility tools: search + fetch
# --------------------------------------------------------------------------- #

def _make_doc_id(project: ProjectConfig, rel_path: str) -> str:
    return f"{project.project_id}::{rel_path}"


@mcp.tool
async def search(query: str) -> dict:
    """Search across all connected Overleaf projects for a keyword.

    Returns a list of matching files as {id, title, url}. Use `fetch` with an id
    to retrieve a file's full text. (Also satisfies ChatGPT's deep-research
    connector contract.)
    """
    settings, worker = STATE.load()
    results: list[dict] = []
    seen: set[str] = set()
    for proj in settings.projects:
        try:
            repo = await worker.ensure_repo(proj)
            hits = search_files(repo, query, max_hits=50)
        except Exception:
            continue
        for hit in hits:
            doc_id = _make_doc_id(proj, hit.path)
            if doc_id in seen:
                continue
            seen.add(doc_id)
            results.append(
                {
                    "id": doc_id,
                    "title": f"{proj.name}/{hit.path}",
                    "url": _overleaf_url(proj),
                }
            )
    return {"results": results}


@mcp.tool
async def fetch(id: str) -> dict:
    """Fetch the full text of a file previously returned by `search`.

    Args:
        id: A document id of the form "<projectId>::<path>" from a search result.
    """
    if "::" not in id:
        raise ToolError("Invalid id; expected '<projectId>::<path>'.")
    project_id, _, rel_path = id.partition("::")
    try:
        proj, worker = STATE.resolve(project_id)
        repo = await worker.ensure_repo(proj)
        content = read_text(safe_join(repo, rel_path))
    except Exception as exc:
        raise _wrap_fs_errors(exc)
    return {
        "id": id,
        "title": f"{proj.name}/{rel_path}",
        "text": content,
        "url": _overleaf_url(proj),
        "metadata": {"project": proj.name, "path": rel_path},
    }


def create_server() -> FastMCP:
    return mcp


# ASGI app for production (uvicorn leafbridge.server:app). MCP mounts at /mcp/.
app = mcp.http_app()
