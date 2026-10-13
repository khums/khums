"""Programmatic plan.md artifact gate for the process dimension."""
from __future__ import annotations

import json
import os
from pathlib import Path

from rewardkit import criterion


def _verifier_dir() -> Path:
    out = Path(os.environ.get("VERIFIER_DIR", os.environ.get("RESULT_DIR", "/logs/verifier")))
    out.mkdir(parents=True, exist_ok=True)
    return out


@criterion(description="plan.md process artifact present and non-empty.")
def plan_md_present(workspace: Path) -> float:
    plan_path = Path(os.environ.get("OUTPUT_DIR", "/app/output")) / "plan.md"
    text = (
        plan_path.read_text(encoding="utf-8", errors="replace")
        if plan_path.exists() and plan_path.is_file()
        else ""
    )
    stripped = text.strip()
    result = {
        "plan_md": {
            "path": str(plan_path),
            "exists": plan_path.exists() and plan_path.is_file(),
            "non_empty": bool(stripped),
            "bytes": len(text.encode()),
            "lines": len(text.splitlines()),
            "score": 1.0 if stripped else 0.0,
        }
    }
    (_verifier_dir() / "process_artifacts.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result["plan_md"]["score"]
