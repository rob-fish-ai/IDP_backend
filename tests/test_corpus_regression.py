"""Every rule change is judged on every packet seen so far.

Skips when the job store is not present (CI); on the engine host it
replays the post-model stages over the whole corpus and fails on any
case whose findings or delivered figures moved from the baseline. A
movement that is intended is accepted by re-running
`python scripts/replay_corpus.py update` and committing the baseline.
"""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))


def test_the_corpus_replays_as_the_baseline_says():
    import replay_corpus
    db = replay_corpus._db_path()
    if not db.exists() or not replay_corpus.BASELINE.exists():
        pytest.skip("no job store or baseline on this host")
    base = json.loads(replay_corpus.BASELINE.read_text())
    now = replay_corpus.replay_all(db)
    lines = replay_corpus.diff(base, now)
    assert not lines, "\n".join(lines)
