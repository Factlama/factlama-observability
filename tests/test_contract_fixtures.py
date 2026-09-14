"""OBS-03 acceptance: execute contracts/v0.1's canonical ReliabilityEvent
fixtures against this repository's own type, inside its own CI -- the
compatibility check factlama-reliability's own test_contract_fixtures.py
explicitly deferred to here, since ReliabilityEvent is this repo's type to
consume, not Reliability's to produce a local model of.
"""

import json
import os
from pathlib import Path

import pytest

from schemas.reliability_event import ReliabilityEvent


def _contracts_dir() -> Path | None:
    """Locate contracts/v0.1: an explicit env var (set by CI, which checks out
    factlama-architecture alongside this repo), or the local three-repo
    workspace layout (../factlama-architecture) for local development."""
    env = os.environ.get("FACTLAMA_CONTRACTS_DIR")
    if env:
        path = Path(env)
        return path if path.is_dir() else None
    local = Path(__file__).resolve().parents[2] / "factlama-architecture" / "contracts" / "v0.1"
    return local if local.is_dir() else None


CONTRACTS_DIR = _contracts_dir()

pytestmark = pytest.mark.skipif(
    CONTRACTS_DIR is None,
    reason=(
        "contracts/v0.1 not found -- set FACTLAMA_CONTRACTS_DIR, or run inside the "
        "three-repo local workspace next to ../factlama-architecture"
    ),
)


def _valid_examples(*parts: str) -> list[Path]:
    if CONTRACTS_DIR is None:
        return []
    directory = CONTRACTS_DIR.joinpath("examples", "valid", *parts)
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


class TestValidReliabilityEventFixtures:
    """Covers a completed event, an abstained event, and a disputed/MIXED
    calibration-class event -- not just the success path."""

    @pytest.mark.parametrize("path", _valid_examples("reliability_event"), ids=lambda p: p.stem)
    def test_parses_as_reliability_event(self, path: Path) -> None:
        ReliabilityEvent(**json.loads(path.read_text()))


def test_at_least_one_fixture_was_actually_found() -> None:
    """A path/glob bug that silently finds zero fixtures would make the
    parametrized test above vacuously pass -- fail loudly instead."""
    if CONTRACTS_DIR is None:
        pytest.skip("contracts/v0.1 not found")
    assert _valid_examples("reliability_event")
