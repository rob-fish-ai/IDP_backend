"""Where the taxonomy and the checklist rows fall short, from the job store.

The HUD 92006 was reported missing on every HUD scan for weeks because the
form never prints its number in its title and the label carried no title
alias: the classifier filed it as a generic signed HUD form and the
checklist row asking for it came back "not in the packet". This sweep
finds that class of gap before a reviewer does:

  1. taxonomy labels never assigned to a page;
  2. page titles the classifier could only nearest-match, how often, and
     the label each landed under (a title that recurs under a fallback
     bucket wants an alias or a label of its own; one landing under a
     specific label it is not wants a hint);
  3. checklist row labels Cartograph has sent that no label answers.

    .venv/bin/python scripts/taxonomy_sweep.py [--db PATH] [--top N]
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.services.doc_taxonomy import known_labels  # noqa: E402
from app.services.cartograph.checklist import document_type_for, is_attribute_row, normalise_rows  # noqa: E402
from app.services.cartograph.identity import concepts_of  # noqa: E402

_SUFFIX = re.compile(r"\s*\((Previous|Superseded)\)$")
_TITLED = re.compile(r"page titled '([^']+)'")


def sweep(db_path: str, top: int = 40) -> dict:
    con = sqlite3.connect(db_path)
    assigned: collections.Counter = collections.Counter()
    titles: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    row_labels: collections.Counter = collections.Counter()
    cases = 0
    for er, cr in con.execute("select extraction_result, checklist_rows from audit_jobs where extraction_result is not null"):
        try:
            d = json.loads(er)
        except (TypeError, ValueError):
            continue
        cases += 1
        for g in d.get("document_groups") or []:
            base = _SUFFIX.sub("", g.get("document_type") or "")
            assigned[base] += 1
            m = _TITLED.search(g.get("notes") or "")
            if m:
                titles[re.sub(r"\s+", " ", m.group(1)).strip().lower()[:60]][base] += 1
        if cr:
            try:
                rows = json.loads(cr)
            except (TypeError, ValueError):
                rows = None
            if rows is not None:
                rows = normalise_rows({"checklist_rows": rows} if isinstance(rows, list) else rows)
                for r in rows:
                    if r.get("label"):
                        row_labels[r["label"].strip()] += 1
    labels = known_labels()
    never = sorted(l for l in labels if assigned[l] == 0)
    nearest = sorted(titles.items(), key=lambda kv: -sum(kv[1].values()))[:top]
    unresolved = []
    for label, n in row_labels.most_common():
        row = {"label": label}
        if concepts_of(row) or is_attribute_row(row):
            continue
        if document_type_for(row)[0] is None:
            unresolved.append((n, label))
    return {"cases": cases, "never_assigned": never, "nearest_titles": nearest, "unresolved_rows": unresolved}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--db", default=os.environ.get("IDP_JOB_STORE", "/var/data/audit_jobs.db"))
    ap.add_argument("--top", type=int, default=40)
    args = ap.parse_args()
    if not Path(args.db).exists():
        print(f"no job store at {args.db}", file=sys.stderr)
        return 2
    out = sweep(args.db, args.top)
    print(f"cases with an extraction: {out['cases']}")
    print(f"\nTAXONOMY LABELS NEVER ASSIGNED ({len(out['never_assigned'])}):")
    for l in out["never_assigned"]:
        print(f"  {l}")
    print(f"\nTITLES THE CLASSIFIER COULD ONLY NEAREST-MATCH (top {args.top}):")
    for title, where in out["nearest_titles"]:
        landed = ", ".join(f"{lab} x{n}" for lab, n in where.most_common(3))
        print(f"  {sum(where.values()):4d}  {title:<60} -> {landed}")
    print(f"\nCHECKLIST ROW LABELS NO TYPE ANSWERS ({len(out['unresolved_rows'])}):")
    for n, label in out["unresolved_rows"]:
        print(f"  {n:4d}  {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
