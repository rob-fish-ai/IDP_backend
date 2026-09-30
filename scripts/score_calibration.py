"""How the engine's scores line up with what reviewers said.

Joins the reviewer verdicts in finding_feedback with the score of the
case each verdict is about, and prints: precision per finding code, the
overall score of cases with an invalid verdict against cases without one,
and the flag distribution of the fields those cases delivered. Read-only;
the thresholds are set by hand from what this shows once enough verdicts
exist on the current rules.

    python scripts/score_calibration.py
"""
from __future__ import annotations

import json
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from app.core.dependencies import get_settings
    db = Path(get_settings().audit_job_db)
    if not db.exists():
        print(f"no job store at {db}"); return 2
    c = sqlite3.connect(str(db)); c.row_factory = sqlite3.Row
    verdicts = c.execute("SELECT case_id, code, verdict FROM finding_feedback WHERE source='runpod' AND verdict IN ('valid','invalid')").fetchall()
    if not verdicts:
        print("no reviewer verdicts yet"); return 0
    by_code: dict[str, Counter] = defaultdict(Counter)
    by_case: dict[str, Counter] = defaultdict(Counter)
    for v in verdicts:
        by_code[v["code"] or "?"][v["verdict"]] += 1
        by_case[v["case_id"]][v["verdict"]] += 1
    print(f"{len(verdicts)} verdicts on {len(by_case)} cases")
    print("\nprecision by finding code (valid / judged):")
    for code, cnt in sorted(by_code.items(), key=lambda kv: -sum(kv[1].values())):
        n = sum(cnt.values()); print(f"   {code:36s} {cnt['valid']:3d} / {n:<3d}  {cnt['valid'] / n:5.0%}")
    scores = {}
    flags = {}
    for r in c.execute("SELECT case_id, confidence, extraction_result FROM audit_jobs WHERE extraction_result IS NOT NULL"):
        fs = (json.loads(r["extraction_result"]).get("field_scores") or {})
        scores[r["case_id"]] = fs.get("overall_composite", r["confidence"] or 0.0)
        flags[r["case_id"]] = (fs.get("green_fields", 0), fs.get("yellow_fields", 0), fs.get("red_fields", 0))
    with_invalid = [scores[k] for k, cnt in by_case.items() if cnt["invalid"] and k in scores]
    all_valid = [scores[k] for k, cnt in by_case.items() if not cnt["invalid"] and k in scores]
    print("\noverall score of cases with an invalid verdict:", [round(x, 2) for x in with_invalid],
          f"(median {statistics.median(with_invalid):.2f})" if with_invalid else "")
    print("overall score of cases with only valid verdicts: ", [round(x, 2) for x in all_valid],
          f"(median {statistics.median(all_valid):.2f})" if all_valid else "")
    print("\nfield flags (green, yellow, red) on judged cases:")
    for k in sorted(by_case):
        if k in flags:
            print(f"   {k:16s} {flags[k]}  verdicts {dict(by_case[k])}")
    print("\nNote: scores are those stored at the time of the run the reviewer judged; a case re-run since carries its newer score.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
