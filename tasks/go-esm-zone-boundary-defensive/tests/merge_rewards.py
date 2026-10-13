"""Merge the deterministic and (optional) semantic RewardKit outputs into the
root verifier files consumed by DataOS: reward.json, reward-details.json,
final-reward-details.json, semantic-execution.json and reward.txt.

Invoked by tests/test.sh as:
  merge_rewards.py VERIFIER_DIR DET_REWARD_JSON JUDGE_REWARD_JSON ORACLE_RUN DETERMINISTIC_OK

Semantic judge output is merged only when this invocation's
semantic-execution.json records a completed judge pass with every required
dimension, so a partial or stale judge directory can never leak dimensions or
criteria into the root reward-details.json.
"""
import json
import sys
from pathlib import Path

def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def main(argv: list[str]) -> None:
    out = Path(argv[1])
    det_path = Path(argv[2])
    judge_path = Path(argv[3])
    oracle_run = argv[4] == "1"
    deterministic_complete = argv[5] == "1"




    det = load(det_path)
    judge = load(judge_path)
    prepass = load(out / "deterministic_reward.json")
    process_artifacts = load(out / "process_artifacts.json")
    semantic_execution = load(out / "semantic-execution.json")
    det_details = load(det_path.parent / "reward-details.json")
    judge_details = load(judge_path.parent / "reward-details.json")

    det_score = float(prepass.get("deterministic_score", det.get("deterministic", 0.0)) or 0.0)
    plan_score = float(
        ((process_artifacts.get("plan_md") or {}).get("score", det.get("process", 0.0))) or 0.0
    )
    semantic_names = ("process", "output_quality", "validity", "task_quality")
    semantic_scores = {
        name: min(1.0, max(0.0, float(judge.get(name, 0.0) or 0.0)))
        for name in semantic_names
    }
    semantic_complete = (
        semantic_execution.get("status") == "completed"
        and bool(judge)
        and all(name in judge for name in semantic_names)
    )
    if not semantic_complete:
        # Never merge a partial or stale judge pass into this invocation's output.
        judge = {}
        judge_details = {}
        semantic_scores = {name: 0.0 for name in semantic_names}
    semantic_strict = semantic_complete and all(
        score == 1.0 for score in semantic_scores.values()
    )
    non_deterministic = (
        sum(semantic_scores.values()) / len(semantic_names) if semantic_names else 0.0
    )

    # binary_reward is deterministic-only by design: this task's deterministic
    # reproducer (real HTTP exploitation against a real running server) fully and
    # precisely captures correctness, with no ambiguity that needs subjective
    # judging. Semantic scores (process/output_quality/task_quality/validity) are
    # still computed when available and reported for continuous_reward and
    # diagnostics, but they never gate the binary pass/fail decision -- matching
    # the deterministic-only strict formula used in production
    # (java-apinizer-expression-boundary-defensive/tests/test.sh), not the
    # semantic-gated variant.
    binary = 1.0 if (deterministic_complete and bool(prepass.get("strict_pass")) and plan_score == 1.0) else 0.0

    process_judge = judge.get("process")
    process_score = (
        plan_score
        if process_judge is None
        else min(plan_score, float(process_judge or 0.0))
    )
    weighted = {"deterministic": (det_score, 55.0), "process": (process_score, 20.0)}
    if "output_quality" in judge:
        weighted["output_quality"] = (semantic_scores["output_quality"], 25.0)
    denominator = sum(weight for _, weight in weighted.values())
    continuous = (
        sum(score * weight for score, weight in weighted.values()) / denominator
        if denominator
        else 0.0
    )

    reward = {
        "reward": binary,
        "binary_reward": binary,
        "continuous_reward": round(continuous, 4),
        "deterministic": round(det_score, 4),
        "process": round(process_score, 4),
    }
    for name in ("output_quality", "task_quality", "validity"):
        if name in judge:
            reward[name] = round(semantic_scores[name], 4)

    diagnostics = {
        "reward": reward,
        "deterministic_prepass": prepass,
        "deterministic_rewardkit": det,
        "scoring_policy": {
            "authority": "deterministic_only",
            "binary_reward_gate": "strict_pass_and_plan_score",
            "semantic_review_affects_reward": False,
            "semantic_review_affects_continuous_reward": semantic_complete,
            "required_semantic_dimensions": list(semantic_names),
            "semantic_strict_pass": semantic_strict,
            "oracle_exemption": oracle_run,
        },
        "semantic_review": {
            "complete": semantic_complete,
            "skipped": not semantic_complete,
            "non_deterministic": round(non_deterministic, 4),
            "scores": {name: round(score, 4) for name, score in semantic_scores.items()},
            "raw_rewardkit": judge,
        },
        "rewardkit_details": {
            "deterministic": det_details,
            "semantic": judge_details,
        },
        "process_artifacts": process_artifacts,
        "weights": {"deterministic": 55, "output_quality": 25, "process": 20},
    }

    details = dict(det_details)
    for dimension, block in judge_details.items():
        if dimension == "process" and isinstance(details.get(dimension), dict) and isinstance(block, dict):
            merged = dict(details[dimension])
            merged["criteria"] = list(details[dimension].get("criteria") or []) + list(block.get("criteria") or [])
            merged.update({key: value for key, value in block.items() if key not in {"criteria", "score"}})
            merged["score"] = round(process_score, 4)
            details[dimension] = merged
        else:
            details[dimension] = block
    if isinstance(details.get("process"), dict):
        details["process"]["score"] = round(process_score, 4)
    if not details:
        details = {
            "deterministic": {"score": round(det_score, 4), "criteria": [], "kind": "programmatic"},
            "process": {"score": round(process_score, 4), "criteria": [], "kind": "programmatic"},
        }

    (out / "semantic-execution.json").write_text(
        json.dumps(semantic_execution, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out / "reward.json").write_text(
        json.dumps(reward, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out / "reward-details.json").write_text(
        json.dumps(details, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out / "final-reward-details.json").write_text(
        json.dumps(diagnostics, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out / "reward.txt").write_text(f"{binary:.4f}\n", encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv)
