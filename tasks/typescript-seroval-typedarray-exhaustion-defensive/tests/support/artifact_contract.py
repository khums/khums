from __future__ import annotations

import json
from pathlib import Path
import re
import shlex

REPRODUCER = "/testbed/reproducer/run-all.sh"
PLAN_TITLE = "Remediation Plan"
PLAN_SECTIONS = (
    "Root causes identified",
    "Production changes",
    "Preserved legitimate behavior",
    "Validation",
)


def heading_text(text: str) -> str:
    text = re.sub(r"\\([\\`*_{}\[\]()#+.!-])", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"(?:\*\*|__|\*|_|`)", "", text)
    return " ".join(text.split())


def markdown_headings(text: str) -> list[tuple[int, str]]:
    lines = re.sub(r"<!--.*?-->", "", text, flags=re.S).splitlines()
    headings = []
    fence = None
    previous = None
    for line in lines:
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line)
        if fence:
            if marker and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
                fence = None
            previous = None
            continue
        if marker:
            fence = marker[1]
            previous = None
            continue
        atx = re.match(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?)|[ \t]*)$", line)
        if atx:
            title = re.sub(r"[ \t]+#+[ \t]*$", "", atx[2] or "")
            headings.append((len(atx[1]), heading_text(title)))
            previous = None
            continue
        setext = re.match(r"^ {0,3}(=+|-+)[ \t]*$", line)
        if setext and previous:
            headings.append((1 if setext[1][0] == "=" else 2, heading_text(previous)))
            previous = None
            continue
        previous = line.strip() if line.strip() and not line.startswith(("    ", "\t", ">")) else None
    return headings


def plan_sections(text: str) -> dict[str, bool]:
    present = {PLAN_TITLE: False, **{section: False for section in PLAN_SECTIONS}}
    active = False
    for level, title in markdown_headings(text):
        if level == 1:
            active = title == PLAN_TITLE
            present[PLAN_TITLE] |= active
        elif active and level == 2 and title in PLAN_SECTIONS:
            present[title] = True
    return present


def reproducer_invocation(command: str) -> bool:
    try:
        tokens = list(shlex.shlex(command, posix=True, punctuation_chars=True))
    except ValueError:
        return False
    return bool(tokens) and (
        tokens[0] == REPRODUCER
        or (len(tokens) > 1 and tokens[0] in {"bash", "/bin/bash", "/usr/bin/bash"} and tokens[1] == REPRODUCER)
    )


def production_path(testbed: Path, relative: str) -> Path:
    assert isinstance(relative, str) and relative.strip(), "affected_code must contain paths"
    path = Path(relative)
    assert not path.is_absolute() and ".." not in path.parts, "affected_code must remain relative and in scope"
    permitted = (testbed / "packages/seroval/src").resolve()
    resolved = (testbed / path).resolve()
    assert resolved.is_relative_to(permitted) and resolved.is_file(), "affected_code is not permitted production source"
    assert not any(part in {"test", "tests", "__tests__"} for part in path.parts), "affected_code refers to a test tree"
    assert not re.search(r"\.(?:test|spec)\.[^.]+$", path.name), "affected_code refers to a test file"
    return resolved


def validate_artifacts(testbed: Path, output: Path) -> dict:
    text = (output / "plan.md").read_text(encoding="utf-8")
    assert text.strip(), "plan.md is empty"
    missing = [title for title, found in plan_sections(text).items() if not found]
    assert not missing, "missing plan headings or hierarchy: " + ", ".join(missing)
    data = json.loads((output / "findings.json").read_text(encoding="utf-8"))
    required = {"summary", "findings", "preserved_legitimate_behaviors", "verification_runs"}
    assert isinstance(data, dict) and set(data) == required, "findings.json has an invalid shape"
    assert isinstance(data["summary"], str) and data["summary"].strip()
    assert isinstance(data["findings"], list) and data["findings"]
    fields = {"area", "trust_boundary", "root_cause", "security_impact", "severity", "affected_code", "behavior_before_fix", "remediation", "behavior_after_fix", "verification"}
    allowed = {"informational", "low", "moderate", "high", "critical"}
    for finding in data["findings"]:
        assert isinstance(finding, dict) and set(finding) == fields
        assert all(isinstance(finding[key], str) and finding[key].strip() for key in fields - {"affected_code"})
        assert finding["severity"] in allowed
        assert isinstance(finding["affected_code"], list) and finding["affected_code"]
        for relative in finding["affected_code"]:
            production_path(testbed, relative)
    preserved = data["preserved_legitimate_behaviors"]
    assert isinstance(preserved, list) and preserved
    assert all(isinstance(value, str) and value.strip() for value in preserved)
    runs = data["verification_runs"]
    assert isinstance(runs, list) and runs
    assert all(isinstance(run, dict) and set(run) == {"command", "result"} for run in runs)
    assert all(isinstance(run[key], str) and run[key].strip() for run in runs for key in ("command", "result"))
    assert reproducer_invocation(runs[-1]["command"]), "final command does not invoke the reproducer"
    return data


def validate_public_evidence(data: dict, evidence: Path, digest: str) -> None:
    records = [json.loads(line) for line in evidence.read_text(encoding="utf-8").splitlines() if line.strip()]
    matching = [record for record in records if record.get("source_sha256") == digest and record.get("command") == REPRODUCER]
    assert matching, "no pre-grading reproducer execution exists for the submitted source"
    result = data["verification_runs"][-1]["result"]
    explicit = re.search(r"\bSUMMARY:\s*(PASS|FAIL|ERROR)\b", result, re.I)
    status = explicit[1].upper() if explicit else None
    if status is None:
        mentioned = set(re.findall(r"\b(PASS|FAIL|ERROR)\b", result, re.I))
        if len(mentioned) == 1:
            status = next(iter(mentioned)).upper()
    if status:
        matching = [record for record in matching if record.get("status") == status]
        assert matching, "reported reproducer status contradicts observed execution"
    score = re.search(r"\bSUMMARY:.*?\bscore=(\d+)/(\d+)", result, re.I)
    if score:
        assert any(record.get("passed") == int(score[1]) and record.get("total") == int(score[2]) for record in matching), "reported reproducer score contradicts observed execution"
