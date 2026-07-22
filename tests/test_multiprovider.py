"""Tests for multi-provider connect: identity parsing (parse_repo_target), the
SSRF trust boundary (validate_remote_git_url), and the AccountService changes
that keep per-repo GitHub/GitLab tokens separate from the account-level Overleaf
token. Overleaf stays the default and must behave byte-for-byte as before."""

from __future__ import annotations

import pytest

from leafbridge.config import (
    ConfigError,
    parse_repo_target,
    validate_remote_git_url,
)
from leafbridge.service import (
    AccountService,
    AlreadyConnected,
    ProjectNotConnected,
    ServiceError,
)
from leafbridge.store import InMemoryStore, TokenCipher

HEX = "0123456789abcdef01234567"
OVERLEAF_URL = f"https://www.overleaf.com/project/{HEX}"


def _svc() -> AccountService:
    return AccountService(InMemoryStore(), TokenCipher(TokenCipher.generate_key()))


async def _admin(svc: AccountService, uid: str = "u", email: str = "e@x.com"):
    # Admin => unlimited projects, so a test can hold an Overleaf + a GitHub repo.
    return await svc.get_or_create_user(uid, email, admin_emails=(email,))


# --- parse_repo_target -----------------------------------------------------

def test_parse_overleaf_url_and_bare_id():
    for src in (OVERLEAF_URL, HEX, f"https://git.overleaf.com/{HEX}"):
        t = parse_repo_target(src)
        assert t.provider == "overleaf"
        assert t.clone_url is None  # Overleaf synthesises its own default URL
        assert t.project_key == HEX
        assert t.git_username == "git"
        assert t.default_name == HEX[:8]


def test_parse_github_variants():
    for src in (
        "https://github.com/Owner/Repo",
        "https://github.com/Owner/Repo.git",
        "https://github.com/Owner/Repo/tree/main",
        "github.com/Owner/Repo",
    ):
        t = parse_repo_target(src)
        assert t.provider == "github"
        assert t.clone_url == "https://github.com/Owner/Repo.git"
        assert t.git_username == "x-access-token"
        # slugged + lowercased, with a hash of the canonical identity folded in so
        # _slug-colliding repos (my-lib vs my_lib) stay distinct.
        assert t.project_key.startswith("github-owner-repo-")
        assert t.default_name == "Repo"


def test_parse_gitlab_subgroup_preserved():
    t = parse_repo_target("https://gitlab.com/group/subgroup/paper/-/tree/main")
    assert t.provider == "gitlab"
    assert t.clone_url == "https://gitlab.com/group/subgroup/paper.git"
    assert t.git_username == "oauth2"
    assert t.project_key.startswith("gitlab-group-subgroup-paper-")
    assert t.default_name == "paper"


def test_parse_bitbucket():
    t = parse_repo_target("https://bitbucket.org/team/thesis.git")
    assert t.provider == "bitbucket"
    assert t.clone_url == "https://bitbucket.org/team/thesis.git"
    assert t.git_username == "x-token-auth"
    assert t.project_key.startswith("bitbucket-team-thesis-")
    assert t.default_name == "thesis"


def test_project_key_distinguishes_slug_colliding_repos():
    # my-lib, my_lib and my.lib are three genuinely distinct GitHub repos that
    # _slug would collapse to the same 'github-acme-my-lib'. The folded hash must
    # keep their project_keys (store row id AND on-disk clone dir) distinct.
    keys = {
        parse_repo_target(f"https://github.com/acme/{repo}").project_key
        for repo in ("my-lib", "my_lib", "my.lib")
    }
    assert len(keys) == 3
    # Segment-boundary collisions must also stay distinct.
    assert (
        parse_repo_target("https://github.com/a/b-c").project_key
        != parse_repo_target("https://github.com/a-b/c").project_key
    )
    assert (
        parse_repo_target("https://gitlab.com/a/b/c").project_key
        != parse_repo_target("https://gitlab.com/a-b/c").project_key
    )
    # GitHub is case-insensitive, so case-only variants must still dedup to one.
    assert (
        parse_repo_target("https://github.com/Acme/My-Lib").project_key
        == parse_repo_target("https://github.com/acme/my-lib").project_key
    )


def test_parse_generic_selfhosted_requires_allowlist(monkeypatch):
    monkeypatch.delenv("LEAFBRIDGE_GIT_HOST_ALLOWLIST", raising=False)
    # Not allow-listed -> rejected during parse (the SSRF gate).
    with pytest.raises(ConfigError):
        parse_repo_target("https://git.example.com/lab/paper.git")
    # Opt the host in via env -> parses as a generic ("git") remote.
    monkeypatch.setenv("LEAFBRIDGE_GIT_HOST_ALLOWLIST", "git.example.com")
    t = parse_repo_target("https://git.example.com/lab/paper.git")
    assert t.provider == "git"
    assert t.clone_url == "https://git.example.com/lab/paper.git"
    assert t.git_username == "git"
    assert t.project_key.startswith("git-")
    assert t.default_name == "paper"


def test_parse_overleaf_lookalike_host_not_treated_as_overleaf(monkeypatch):
    monkeypatch.delenv("LEAFBRIDGE_GIT_HOST_ALLOWLIST", raising=False)
    # A host that merely CONTAINS "overleaf.com" as a substring must NOT be
    # classified as Overleaf (that would ignore the pasted host and route to
    # git.overleaf.com). It falls through to the generic path and is rejected
    # by the SSRF allowlist.
    for bad in (
        f"https://overleaf.com.evil.com/project/{HEX}",
        f"https://notoverleaf.com/project/{HEX}",
    ):
        with pytest.raises(ConfigError):
            parse_repo_target(bad)
    # The genuine hosts are still classified as Overleaf.
    assert parse_repo_target(f"https://www.overleaf.com/project/{HEX}").provider == "overleaf"
    assert parse_repo_target(f"https://git.overleaf.com/{HEX}").provider == "overleaf"


# --- validate_remote_git_url (SSRF boundary) -------------------------------

@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/x/y",            # not https
        "file:///etc/passwd",               # file scheme
        "ssh://git@github.com/x/y",         # ssh scheme
        "git://github.com/x/y",             # git scheme
        "github.com/x/y",                   # scheme-less
        "",                                 # empty url
        "https:///path",                    # no host
        "https://localhost/x",              # localhost
        "https://api.localhost/x",          # .localhost suffix
        "https://foo.internal/x",           # internal TLD
        "https://db.local/x",               # .local (mDNS) suffix
        "https://127.0.0.1/x",              # IPv4 loopback
        "https://10.0.0.5/x",               # private IPv4
        "https://169.254.169.254/x",        # cloud metadata IP
        "https://[::1]/x",                  # IPv6 loopback
        "https://[::ffff:169.254.169.254]/x",  # IPv4-mapped IPv6 metadata bypass
        "https://[fd00::1]/x",              # IPv6 ULA (private)
        "https://[fe80::1]/x",              # IPv6 link-local
        "https://user:pw@github.com/x",     # embedded credentials
        "https://evil.example.com/x/y",     # not on the allowlist
    ],
)
def test_validate_rejects(url, monkeypatch):
    monkeypatch.delenv("LEAFBRIDGE_GIT_HOST_ALLOWLIST", raising=False)
    with pytest.raises(ConfigError):
        validate_remote_git_url(url)


def test_validate_accepts_known_and_env_allowlisted(monkeypatch):
    monkeypatch.delenv("LEAFBRIDGE_GIT_HOST_ALLOWLIST", raising=False)
    for url in (
        "https://github.com/o/r.git",
        "https://gitlab.com/g/r.git",
        "https://bitbucket.org/w/r.git",
        f"https://git.overleaf.com/{HEX}",
        "https://my.overleaf.com/git/abc",  # *.overleaf.com is trusted
    ):
        validate_remote_git_url(url)  # must not raise
    monkeypatch.setenv("LEAFBRIDGE_GIT_HOST_ALLOWLIST", "git.example.com, git2.example.com")
    validate_remote_git_url("https://git.example.com/x.git")
    validate_remote_git_url("https://git2.example.com/x.git")


# --- service: per-repo tokens vs. account Overleaf token -------------------

async def test_connect_github_stores_per_project_token():
    svc = _svc()
    await _admin(svc)
    p = await svc.connect_project("u", "https://github.com/owner/repo", "ghp_tok")
    assert p.provider == "github"
    assert p.git_username == "x-access-token"
    assert p.git_url == "https://github.com/owner/repo.git"
    # Token lives on the PROJECT (encrypted), never on the user's account token.
    assert p.token_encrypted and svc.cipher.decrypt(p.token_encrypted) == "ghp_tok"
    user = await svc.store.get_user("u")
    assert user.overleaf_token_encrypted == ""


async def test_resolve_github_authed_url():
    svc = _svc()
    await _admin(svc)
    await svc.connect_project("u", "https://github.com/owner/repo", "ghp_tok", "gh")
    cfg = await svc.resolve_project("u", "gh")
    assert cfg.git_username == "x-access-token"
    assert cfg.token == "ghp_tok"
    assert cfg.authed_url() == "https://x-access-token:ghp_tok@github.com/owner/repo.git"


async def test_overleaf_connect_unchanged():
    svc = _svc()
    await _admin(svc)
    p = await svc.connect_project("u", OVERLEAF_URL, "olp_tok", "thesis")
    assert p.provider == "overleaf"
    assert p.token_encrypted == ""  # Overleaf uses the account token
    user = await svc.store.get_user("u")
    assert svc.cipher.decrypt(user.overleaf_token_encrypted) == "olp_tok"
    cfg = await svc.resolve_project("u", "thesis")
    assert cfg.git_username == "git"
    assert cfg.authed_url() == f"https://git:olp_tok@git.overleaf.com/{HEX}"


async def test_revoke_and_set_token_leave_github_token_intact():
    svc = _svc()
    await _admin(svc)
    await svc.connect_project("u", OVERLEAF_URL, "olp_tok", "thesis")
    await svc.connect_project("u", "https://github.com/owner/repo", "ghp_tok", "gh")

    # Revoking the Overleaf account token blocks the Overleaf project but must NOT
    # wipe the GitHub project's own (per-repo) token.
    await svc.revoke_token("u")
    assert (await svc.resolve_project("u", "gh")).token == "ghp_tok"
    with pytest.raises(ProjectNotConnected):
        await svc.resolve_project("u", "thesis")

    # Setting a new Overleaf token likewise leaves the GitHub token untouched.
    await svc.set_token("u", "olp_new")
    assert (await svc.resolve_project("u", "gh")).token == "ghp_tok"
    assert (await svc.resolve_project("u", "thesis")).token == "olp_new"


async def test_account_token_not_adopted_from_github():
    svc = _svc()
    await _admin(svc)
    # Only a GitHub repo connected: its token must NOT become the account token.
    await svc.connect_project("u", "https://github.com/owner/repo", "ghp_tok", "gh")
    assert await svc.has_token("u") is False
    # So adding an Overleaf project still requires the first Overleaf token.
    with pytest.raises(ServiceError):
        await svc.add_project("u", OVERLEAF_URL, "thesis")


async def test_add_github_requires_token():
    svc = _svc()
    await _admin(svc)
    await svc.connect_project("u", OVERLEAF_URL, "olp_tok", "thesis")
    # add_project reuses the Overleaf token for Overleaf, but a GitHub repo needs
    # its own token — omitting it is an error, not a silent Overleaf-token reuse.
    with pytest.raises(ServiceError):
        await svc.add_project("u", "https://github.com/owner/repo", "gh")
    p = await svc.add_project("u", "https://github.com/owner/repo", "gh", token="ghp_tok")
    assert p.provider == "github"
    assert (await svc.resolve_project("u", "gh")).token == "ghp_tok"


async def test_connect_rejects_non_allowlisted_selfhosted(monkeypatch):
    # The service boundary must wrap parse_repo_target's SSRF ConfigError into a
    # user-facing ServiceError, both on the first connect and on a later add.
    monkeypatch.delenv("LEAFBRIDGE_GIT_HOST_ALLOWLIST", raising=False)
    svc = _svc()
    await _admin(svc)
    with pytest.raises(ServiceError):
        await svc.connect_project("u", "https://git.evil.com/a/b.git", "tok")
    # After an Overleaf project exists, add_project must reject it too.
    await svc.connect_project("u", OVERLEAF_URL, "olp_tok", "thesis")
    with pytest.raises(ServiceError):
        await svc.add_project("u", "https://git.evil.com/a/b.git", token="tok")


async def test_connect_generic_selfhosted_roundtrip(monkeypatch):
    monkeypatch.setenv("LEAFBRIDGE_GIT_HOST_ALLOWLIST", "git.example.com")
    svc = _svc()
    await _admin(svc)
    p = await svc.connect_project(
        "u", "https://git.example.com/lab/paper.git", "tok", "paper"
    )
    assert p.provider == "git"
    # The token lives on the project (per-connection), never on the account.
    assert p.token_encrypted and svc.cipher.decrypt(p.token_encrypted) == "tok"
    user = await svc.store.get_user("u")
    assert user.overleaf_token_encrypted == ""
    cfg = await svc.resolve_project("u", "paper")
    assert cfg.git_username == "git"
    assert cfg.authed_url() == "https://git:tok@git.example.com/lab/paper.git"


async def test_add_github_duplicate_rejected():
    svc = _svc()
    await _admin(svc)
    await svc.connect_project("u", OVERLEAF_URL, "olp_tok", "thesis")
    await svc.add_project("u", "https://github.com/owner/repo", "gh", token="ghp_tok")
    with pytest.raises(AlreadyConnected):
        await svc.add_project(
            "u", "https://github.com/owner/repo", token="ghp_tok2"
        )


async def test_resolve_gitlab_and_bitbucket_authed_url():
    svc = _svc()
    await _admin(svc)
    await svc.connect_project("u", "https://gitlab.com/grp/paper", "gl_tok", "gl")
    await svc.connect_project("u", "https://bitbucket.org/team/thesis", "bb_tok", "bb")
    gl = await svc.resolve_project("u", "gl")
    assert gl.git_username == "oauth2"
    assert gl.authed_url() == "https://oauth2:gl_tok@gitlab.com/grp/paper.git"
    bb = await svc.resolve_project("u", "bb")
    assert bb.git_username == "x-token-auth"
    assert bb.authed_url() == "https://x-token-auth:bb_tok@bitbucket.org/team/thesis.git"
