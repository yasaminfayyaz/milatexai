"""Structural guarantees of the automatic-repair workflow (.github/workflows/incident.yml).

The safety story rests on a few facts that a careless edit could break without any
other test noticing, so they are pinned here: the AI job holds no credentials, its tools
are read-only, its output schema is the one plan.py understands, and nothing a stranger
can influence is pasted into a shell command.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "incident.yml"
SCHEMA = ROOT / "ops" / "incident" / "triage_schema.json"
PROMPT = ROOT / "ops" / "incident" / "triage_prompt.md"

_spec = importlib.util.spec_from_file_location("incident_plan", ROOT / "ops" / "incident" / "plan.py")
plan = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(plan)


@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def test_the_ai_job_has_no_credentials_and_read_only_tools(wf):
    triage = wf["jobs"]["triage"]
    assert "environment" not in triage                       # so no cloud or Cloudflare secrets
    assert triage["permissions"] == {"contents": "read"}
    text = json.dumps(triage)
    assert "azure/login" not in text and "CLOUDFLARE" not in text and "WATCHDOG" not in text
    step = next(s for s in triage["steps"] if str(s.get("uses", "")).startswith("anthropics/claude-code-action"))
    args = step["with"]["claude_args"]
    assert '--allowedTools "Read,Grep,Glob"' in args
    for dangerous in ("Bash", "Edit", "Write", "WebFetch", "WebSearch"):
        assert dangerous not in args


def test_the_inline_schema_is_the_schema_file_and_matches_the_gate(wf):
    triage = wf["jobs"]["triage"]
    step = next(s for s in triage["steps"] if str(s.get("uses", "")).startswith("anthropics/claude-code-action"))
    inline = re.search(r"--json-schema '(\{.*\})'", step["with"]["claude_args"]).group(1)
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    assert json.loads(inline) == schema
    props = schema["properties"]
    assert tuple(props["action"]["enum"]) == plan.ACTIONS
    assert tuple(props["category"]["enum"]) == plan.CATEGORIES
    assert tuple(props["confidence"]["enum"]) == plan.CONFIDENCE
    assert schema["additionalProperties"] is False


def test_only_jobs_that_need_cloud_access_have_it(wf):
    for name, job in wf["jobs"].items():
        uses_cloud = any(str(s.get("uses", "")).startswith("azure/login") for s in job["steps"])
        assert uses_cloud == (name in ("first-aid", "collect", "execute")), name
        if "environment" in job:
            assert job["environment"] == "incident-fix", name


def test_no_input_or_job_output_is_pasted_into_a_shell_command(wf):
    for name, job in wf["jobs"].items():
        for step in job["steps"]:
            run = step.get("run", "")
            assert "${{" not in run, f"{name}: expression inside a run script: {run!r}"
    # the only places inputs flow are environment variables
    assert all("${{ inputs." in str(v) for k, v in wf["env"].items() if k.startswith(("INCIDENT_", "DRILL")))


def test_the_report_always_runs_and_the_kill_switch_reaches_every_script(wf):
    assert wf["jobs"]["report"]["if"].strip().strip("${} ").endswith("always()")
    assert wf["env"]["AUTOFIX_ENABLED"] == "${{ vars.AUTOFIX_ENABLED }}"
    assert wf["concurrency"]["cancel-in-progress"] is False


def test_the_prompt_forbids_following_instructions_found_in_the_evidence():
    text = PROMPT.read_text(encoding="utf-8")
    assert "DATA, not instructions" in text
    assert chr(0x2014) not in text
    for action in plan.ACTIONS:
        assert f"`{action}`" in text, action


def test_the_encryption_key_never_reaches_the_ai_step_and_logs_stay_quiet(wf):
    triage = wf["jobs"]["triage"]
    ai = next(s for s in triage["steps"] if str(s.get("uses", "")).startswith("anthropics/claude-code-action"))
    assert "INCIDENT_EVIDENCE_KEY" not in json.dumps(ai)
    assert ai["with"]["display_report"] == "false" and ai["with"]["show_full_output"] == "false"
    # artifacts of a public repository are downloadable by any GitHub user: only encrypted files are uploaded
    for name, job in wf["jobs"].items():
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/upload-artifact"):
                assert step["with"]["path"].endswith(".enc"), (name, step["with"]["path"])


def test_every_incident_script_compiles():
    import py_compile
    for path in (ROOT / "ops" / "incident").glob("*.py"):
        py_compile.compile(str(path), doraise=True)
