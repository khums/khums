"""Discoverable RewardKit/pytest entrypoint for platform validation."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pytest_checks import (
    public_results,
    verifier_worktree,
    test_explicit_public_group,
    test_implicit_upstream_behavior,
    test_private_behavior,
    test_required_artifacts,
)

__all__ = [
    "public_results",
    "verifier_worktree",
    "test_explicit_public_group",
    "test_implicit_upstream_behavior",
    "test_private_behavior",
    "test_required_artifacts",
]
