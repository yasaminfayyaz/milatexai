"""A user on a self-hosted Overleaf or Git server is told how to ask for it, and hosts we have agreed to are
allowed by the deploy script (ops/deploy.sh GIT_HOSTS)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from leafbridge import config

DEPLOY = Path(__file__).resolve().parent.parent / "ops" / "deploy.sh"


def test_an_unknown_host_is_told_how_to_ask_for_it(monkeypatch):
    monkeypatch.delenv("LEAFBRIDGE_GIT_HOST_ALLOWLIST", raising=False)
    with pytest.raises(config.ConfigError) as err:
        config.parse_repo_target("https://git.example-university.edu/git/abc123")
    text = str(err.value)
    assert "support@milatexai.com" in text and "git.example-university.edu" in text
    assert "github.com" in text and "not on the allowed" not in text


def _deploy_hosts() -> str:
    script = DEPLOY.read_text(encoding="utf-8")
    assert 'LEAFBRIDGE_GIT_HOST_ALLOWLIST="$GIT_HOSTS"' in script        # the deploy really passes it on
    return re.search(r'^GIT_HOSTS="([^"]*)"', script, re.M).group(1)


def test_the_hosts_the_deploy_allows_can_be_connected(monkeypatch):
    hosts = _deploy_hosts()
    assert "sharelatex.gwdg.de" in hosts.split(",")
    monkeypatch.setenv("LEAFBRIDGE_GIT_HOST_ALLOWLIST", hosts)
    target = config.parse_repo_target("https://sharelatex.gwdg.de/git/69af06b91376ebf673aeb9db")
    assert target.provider == "git" and target.clone_url.startswith("https://sharelatex.gwdg.de/")


def test_the_allowlist_does_not_open_internal_addresses(monkeypatch):
    monkeypatch.setenv("LEAFBRIDGE_GIT_HOST_ALLOWLIST", _deploy_hosts())
    for url in ("https://169.254.169.254/x", "https://localhost/x", "https://intranet.internal/x"):
        with pytest.raises(config.ConfigError):
            config.validate_remote_git_url(url)
