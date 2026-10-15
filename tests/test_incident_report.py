"""The repair's final verdict: a blip that healed itself is "recovered", not "needs you"."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

INC = Path(__file__).resolve().parents[1] / "ops" / "incident"
sys.path.insert(0, str(INC))
_spec = importlib.util.spec_from_file_location("incident_report", INC / "report.py")
report = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(report)


def _fake(monkeypatch, *, view_level, origin_healthy, kind="ours"):
    asked = []
    monkeypatch.setattr(report.c, "site_view", lambda: {"known": True, "level": view_level, "age_seconds": 30})
    monkeypatch.setattr(report.c, "effective_kind", lambda: kind)
    monkeypatch.setattr(report.c, "target_app", lambda: "milatexai-app")
    monkeypatch.setattr(report.c, "app_base", lambda app=None: "https://origin.example")
    monkeypatch.setattr(report.c, "probe_origin", lambda base, patience=0: asked.append(patience) or {"healthy": origin_healthy})
    return asked


def test_an_explicit_answer_from_an_earlier_step_wins(monkeypatch):
    _fake(monkeypatch, view_level="down", origin_healthy=False)
    assert report.healthy_now("true", {}) is True
    assert report.healthy_now("false", {"recovered": True}) is False


def test_after_first_aid_found_the_app_answering_the_app_itself_is_asked(monkeypatch):
    asked = _fake(monkeypatch, view_level="down", origin_healthy=True)   # watchdog still mid-incident
    assert report.healthy_now("", {"recovered": True, "restarted": False}) is True
    assert asked and asked[0] >= 120                                       # time to ride out a deploy swap


def test_without_first_aid_success_the_watchdog_view_decides(monkeypatch):
    asked = _fake(monkeypatch, view_level="down", origin_healthy=True)
    assert report.healthy_now("", {"recovered": False}) is False and not asked
    _fake(monkeypatch, view_level="ok", origin_healthy=False)
    assert report.healthy_now("", {}) is True


def test_edge_problems_are_not_cleared_by_a_healthy_app(monkeypatch):
    _fake(monkeypatch, view_level="down", origin_healthy=True, kind="edge")
    assert report.healthy_now("", {"recovered": True}) is False
