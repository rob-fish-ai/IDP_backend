#!/usr/bin/env python
"""Extraction-quality harness: replay real packets through the pipeline and
score every run against verified gold values.

The stored rows in the job store carry `page_ocr` (per-page text and flags),
so a packet can be replayed through everything after OCR without its PDF.
Vision-dependent steps (signature check, required-field image recovery) are
skipped in that mode because the page images are gone; the text path, the
classifier, the extractors, post-processing, findings and scoring all run for
real and cost real LLM calls.

Modes:
  --stored          score the extraction already in the job store (no LLM calls)
  --runs N          replay the stored OCR through the pipeline N times (LLM calls)
  --pdf PATH        run the full pipeline (OCR + extraction) on a PDF, --runs times
  --json PATH ...   score saved extraction JSON files

Examples:
  .venv/bin/python eval/run_eval.py --case J-PORT-05318 --stored
  .venv/bin/python eval/run_eval.py --case J-PORT-05318 --runs 3
  .venv/bin/python eval/run_eval.py --all --stored
  .venv/bin/python eval/run_eval.py --case CAS570103 --pdf /root/testpackets/packet.pdf --runs 2

Every replayed run is saved under eval/runs/<case>/ so it can be re-scored
after a scoring-only change without re-running the models.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GOLD_DIR = ROOT / "eval" / "gold"
RUNS_DIR = ROOT / "eval" / "runs"
DB_PATH = "/var/data/audit_jobs.db"

# Calculation method preference, same order the TIC-total check uses.
_METHOD_PRIORITY = {"voi-based": 0, "self-declared": 1, "ytd-based": 2, "paystub-based": 3}

_CERT_MONEY = {"householdIncome", "tenantRent", "utilityAllowance", "grossRent", "federalRentAssistance"}


# ---------------------------------------------------------------------------
# Normalisers
# ---------------------------------------------------------------------------

def _money(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("$", "").replace(",", "").strip()
    if not s or s.lower() in ("n/a", "null", "none", "-"):
        return None
    try:
        return round(float(s), 2)
    except ValueError:
        return None


def _near(a: float | None, b: float | None, tol_abs: float = 1.0, tol_rel: float = 0.005) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(tol_abs, abs(b) * tol_rel)


def _norm_str(v) -> str:
    return re.sub(r"\s+", " ", str(v or "")).strip().lower()


def _last4(v) -> str | None:
    d = re.sub(r"\D", "", str(v or ""))
    return d[-4:] if len(d) >= 4 else None


def _first_token(v) -> str:
    return _norm_str(v).split(" ")[0] if _norm_str(v) else ""


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

class Check:
    __slots__ = ("section", "name", "gold", "got", "ok", "note")

    def __init__(self, section, name, gold, got, ok, note=""):
        self.section, self.name, self.gold, self.got, self.ok, self.note = section, name, gold, got, ok, note

    def row(self) -> str:
        mark = "ok " if self.ok else "XX "
        return f"  {mark} {self.section:6} {self.name:34} gold={str(self.gold):>14}  got={str(self.got):>14}  {self.note}"


def _member_matches(gm: dict, m: dict) -> bool:
    return (_norm_str(m.get("LastName")) == _norm_str(gm["last"])
            and _first_token(m.get("FirstName")) == _first_token(gm["first"]))


def _source_matches(aliases: list[str], source: str) -> bool:
    s = _norm_str(source)
    return any(_norm_str(a) in s for a in aliases)


def _annual_for(ex: dict, gi: dict) -> tuple[float | None, str]:
    """Best annual figure the engine holds for a gold income record."""
    lasts = {_norm_str(gi["member_last"])} | {_norm_str(x) for x in gi.get("accept_member_last", [])}
    calcs = [
        c for c in ex.get("income_calculations", [])
        if any(_norm_str(c.get("memberName", "")).endswith(l) for l in lasts)
        and _source_matches(gi["source_aliases"], c.get("sourceName", ""))
        and not str(c.get("details") or "").startswith(("[audit]", "[historical]"))
    ]
    if calcs:
        calcs.sort(key=lambda c: _METHOD_PRIORITY.get(c.get("method") or "", 99))
        return _money(calcs[0].get("annualIncome")), f"calc:{calcs[0].get('method')}"
    # No calculation: fall back to the record's own rate × frequency.
    from app.services.income_calculator import get_frequency_multiplier
    for vi in ex.get("income", {}).get("sourceIncome", {}).get("verificationIncome", []):
        if any(_norm_str(vi.get("memberName", "")).endswith(l) for l in lasts) \
                and _source_matches(gi["source_aliases"], vi.get("sourceName", "")):
            rate = _money(vi.get("rateOfPay")) or _money(vi.get("selfDeclaredAmount"))
            mult = get_frequency_multiplier(vi.get("frequencyOfPay") or "") if vi.get("frequencyOfPay") else None
            if rate is not None and mult:
                return round(rate * mult, 2), "record:rate×freq"
            return rate, "record:no-annual"
    return None, "absent"


def _income_records_for(ex: dict, gi: dict) -> list[dict]:
    lasts = {_norm_str(gi["member_last"])} | {_norm_str(x) for x in gi.get("accept_member_last", [])}
    return [
        vi for vi in ex.get("income", {}).get("sourceIncome", {}).get("verificationIncome", [])
        if any(_norm_str(vi.get("memberName", "")).endswith(l) for l in lasts)
        and _source_matches(gi["source_aliases"], vi.get("sourceName", ""))
    ]


def score(gold: dict, ex: dict) -> tuple[list[Check], dict]:
    checks: list[Check] = []
    ci = ex.get("certification_info") or {}

    # --- certification scalars ---
    for field, gv in gold.get("certification", {}).items():
        if gv is None:
            continue
        got = ci.get(field)
        if field in _CERT_MONEY:
            ok = _near(_money(got), _money(gv))
            checks.append(Check("cert", field, gv, _money(got), ok))
        elif field == "householdSize":
            ok = _money(got) == float(gv)
            checks.append(Check("cert", field, gv, got, ok))
        else:
            ok = _norm_str(got) == _norm_str(gv)
            checks.append(Check("cert", field, gv, got, ok))
    # signatureDate None in gold means "must be absent"
    if "signatureDate" in gold.get("certification", {}) and gold["certification"]["signatureDate"] is None:
        got = ci.get("signatureDate")
        checks.append(Check("cert", "signatureDate", None, got, got in (None, "", "null"),
                            "" if got in (None, "", "null") else "a date was attached to an undated form"))

    # --- members ---
    members = (ex.get("household_demographics") or {}).get("houseHold") or []
    checks.append(Check("member", "count", len(gold["members"]), len(members), len(members) == len(gold["members"])))
    for gm in gold["members"]:
        m = next((x for x in members if _member_matches(gm, x)), None)
        label = f"{gm['first']} {gm['last']}"
        if m is None:
            checks.append(Check("member", f"{label} present", True, False, False))
            continue
        checks.append(Check("member", f"{label} DOB", gm["dob"], m.get("DOB"), _norm_str(m.get("DOB")) == _norm_str(gm["dob"])))
        checks.append(Check("member", f"{label} SSN last4", gm["ssn_last4"], _last4(m.get("socialSecurityNumber")),
                            _last4(m.get("socialSecurityNumber")) == gm["ssn_last4"]))
        checks.append(Check("member", f"{label} relationship", gm["relationship"], m.get("relationship"),
                            _norm_str(m.get("relationship")) == _norm_str(gm["relationship"])))

    # --- income ---
    vi_all = ex.get("income", {}).get("sourceIncome", {}).get("verificationIncome", [])
    matched_ids: set[int] = set()
    for gi in gold["income"]:
        label = f"{gi['member_first']} {gi['member_last']} / {gi['source_aliases'][0]}"
        recs = _income_records_for(ex, gi)
        matched_ids |= {id(r) for r in recs}
        annual, how = _annual_for(ex, gi)
        if annual is None:
            if gi.get("optional"):
                checks.append(Check("income", f"{label} annual", gi["annual"], None, True, "optional; absent"))
            else:
                checks.append(Check("income", f"{label} annual", gi["annual"], None, False, "absent"))
        else:
            checks.append(Check("income", f"{label} annual", gi["annual"], annual, _near(annual, gi["annual"]), how))
        if len(recs) > 1:
            checks.append(Check("income", f"{label} records", 1, len(recs), False, "duplicate records for one source"))
    extra = [r for r in vi_all if id(r) not in matched_ids]
    checks.append(Check("income", "extra records", 0, len(extra), len(extra) == 0,
                        "; ".join(f"{r.get('memberName')}/{r.get('sourceName')}" for r in extra)[:90]))

    # --- assets ---
    assets = (ex.get("assets") or {}).get("assetInformation") or []
    used: set[int] = set()
    for ga in gold["assets"]:
        label = f"{ga['type']}" + (f" #{ga['last4']}" if ga.get("last4") else f" {ga['balance']:.2f}")
        cand = None
        if ga.get("last4"):
            cand = next((a for a in assets if id(a) not in used and _last4(a.get("accountNumber")) == ga["last4"]), None)
        if cand is None:
            cand = next((a for a in assets if id(a) not in used
                         and _norm_str(ga["type"]) in _norm_str(a.get("accountType"))
                         and _near(_money(a.get("currentBalance")) if _money(a.get("currentBalance")) is not None
                                   else _money(a.get("selfDeclaredAmount")), ga["balance"])), None)
        if cand is None:
            checks.append(Check("asset", f"{label} present", True, False, False))
            continue
        used.add(id(cand))
        bal = _money(cand.get("currentBalance"))
        if bal is None:
            bal = _money(cand.get("selfDeclaredAmount"))
        checks.append(Check("asset", f"{label} balance", ga["balance"], bal, _near(bal, ga["balance"])))
        inc = _money(cand.get("incomeAmount"))
        checks.append(Check("asset", f"{label} income", ga["income"], inc,
                            _near(inc, ga["income"], tol_abs=0.02) if inc is not None else ga["income"] == 0,
                            "" if inc is not None else "income absent (treated as 0)"))
    extra_assets = [a for a in assets if id(a) not in used]
    checks.append(Check("asset", "extra records", 0, len(extra_assets), len(extra_assets) == 0,
                        "; ".join(f"{a.get('accountType')} {a.get('currentBalance') or a.get('selfDeclaredAmount')}" for a in extra_assets)[:90]))
    total = 0.0
    for a in assets:
        v = _money(a.get("currentBalance"))
        if v is None:
            v = _money(a.get("selfDeclaredAmount"))
        total += v or 0.0
    checks.append(Check("asset", "total of records", gold["asset_total"], round(total, 2), _near(total, gold["asset_total"])))

    # --- findings ---
    codes = [f.get("code") for f in ex.get("finding_records", [])]
    for c in gold.get("expect_findings", []):
        checks.append(Check("find", f"expect {c}", True, c in codes, c in codes))
    for c in gold.get("forbid_findings", []):
        checks.append(Check("find", f"forbid {c}", False, c in codes, c not in codes))

    fs = ex.get("field_scores") or {}
    summary = {
        "checks": len(checks),
        "passed": sum(1 for c in checks if c.ok),
        "confidence": round(fs.get("overall_composite") or 0, 3),
        "flag": fs.get("overall_flag"),
        "findings": len(ex.get("findings") or []),
        "structured_findings": len(codes),
        "members": len(members),
        "income_records": len(vi_all),
        "asset_records": len(assets),
        "asset_total": round(total, 2),
    }
    return checks, summary


# ---------------------------------------------------------------------------
# Data access and running
# ---------------------------------------------------------------------------

def load_stored(case_id: str) -> dict:
    con = sqlite3.connect(DB_PATH)
    row = con.execute("select extraction_result from audit_jobs where case_id=?", (case_id,)).fetchone()
    if not row or not row[0]:
        raise SystemExit(f"no stored extraction for {case_id}")
    return json.loads(row[0])


def page_texts_from_stored(ex: dict) -> list[dict]:
    return [{
        "page": p["page"],
        "text": p.get("text") or "",
        "ocr_flag": p.get("flag"),
        "ocr_score": p.get("score"),
        "ocr_flag_details": list(p.get("flags") or []),
        "image_path": None,
    } for p in ex["page_ocr"]]


def run_pipeline(page_texts: list[dict], cert_type: str | None) -> dict:
    from app.core.dependencies import get_settings
    from app.services.pipeline import run_extraction_pipeline
    ex = run_extraction_pipeline(page_texts, get_settings(), certification_type=cert_type)
    return ex.model_dump()


def run_pdf(pdf: Path, cert_type: str | None) -> dict:
    from app.core.dependencies import get_settings
    from app.services.pdf_service import process_pdf_full
    out = process_pdf_full(pdf.read_bytes(), get_settings(), certification_type=cert_type)
    ex = out.get("extraction") if isinstance(out, dict) else out
    return ex.model_dump() if hasattr(ex, "model_dump") else ex


def save_run(case_id: str, tag: str, ex: dict) -> Path:
    d = RUNS_DIR / case_id
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}_{tag}.json"
    p.write_text(json.dumps(ex, default=str))
    return p


def classification_map(ex: dict) -> dict[int, str]:
    return {p["page"]: p.get("document_type") for p in (ex.get("classification") or {}).get("pages", [])}


def report(case_id: str, gold: dict, runs: list[tuple[str, dict]], verbose: bool) -> None:
    print(f"\n{'=' * 100}\n{case_id} — {gold.get('packet', '')}\n{'=' * 100}")
    per_run: list[list[Check]] = []
    for tag, ex in runs:
        checks, summary = score(gold, ex)
        per_run.append(checks)
        print(f"\n[{tag}] passed {summary['passed']}/{summary['checks']} | confidence {summary['confidence']} {summary['flag']} | "
              f"members {summary['members']} income {summary['income_records']} assets {summary['asset_records']} "
              f"(total {summary['asset_total']}) | findings {summary['findings']} ({summary['structured_findings']} structured)")
        for c in checks:
            if verbose or not c.ok:
                print(c.row())
    if len(runs) > 1:
        print(f"\n[spread across {len(runs)} runs]")
        by_name: dict[str, list] = defaultdict(list)
        for checks in per_run:
            for c in checks:
                by_name[f"{c.section} {c.name}"].append(json.dumps(c.got, default=str))
        unstable = {k: Counter(v) for k, v in by_name.items() if len(set(v)) > 1}
        print(f"  unstable checks: {len(unstable)}/{len(by_name)}")
        for k, cnt in unstable.items():
            print(f"    {k:44} {dict(cnt)}")
        maps = [classification_map(ex) for _, ex in runs]
        pages = sorted(set().union(*[m.keys() for m in maps]))
        flips = [(p, [m.get(p) for m in maps]) for p in pages if len({m.get(p) for m in maps}) > 1]
        print(f"  classification flips: {len(flips)}/{len(pages)} pages")
        for p, labels in flips:
            print(f"    p{p}: {labels}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--case", help="case id (gold file name without .json)")
    ap.add_argument("--all", action="store_true", help="every case with a gold file")
    ap.add_argument("--stored", action="store_true", help="score the stored extraction (no LLM calls)")
    ap.add_argument("--runs", type=int, default=0, help="replay N times through the pipeline (LLM calls)")
    ap.add_argument("--pdf", help="run the full pipeline on this PDF instead of stored OCR")
    ap.add_argument("--json", nargs="*", help="score these saved extraction JSON files")
    ap.add_argument("--verbose", "-v", action="store_true", help="print passing checks too")
    ap.add_argument("--quiet-logs", action="store_true", help="silence pipeline logging")
    args = ap.parse_args()

    logging.basicConfig(level=logging.WARNING if args.quiet_logs else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    for n in ("httpx", "anthropic", "httpcore"):
        logging.getLogger(n).setLevel(logging.WARNING)

    cases = sorted(p.stem for p in GOLD_DIR.glob("*.json")) if args.all else [args.case]
    if not cases or cases == [None]:
        ap.error("--case or --all required")

    for case_id in cases:
        gold = json.loads((GOLD_DIR / f"{case_id}.json").read_text())
        cert_type = gold.get("caller_cert_type")
        runs: list[tuple[str, dict]] = []
        if args.json:
            for p in args.json:
                runs.append((Path(p).name, json.loads(Path(p).read_text())))
        if args.stored:
            runs.append(("stored", load_stored(case_id)))
        if args.runs:
            base = None if args.pdf else page_texts_from_stored(load_stored(case_id))
            for i in range(1, args.runs + 1):
                t0 = time.perf_counter()
                ex = run_pdf(Path(args.pdf), cert_type) if args.pdf else run_pipeline(
                    [dict(pt) for pt in base], cert_type)
                path = save_run(case_id, f"run{i}", ex)
                print(f"  run {i}/{args.runs} done in {time.perf_counter() - t0:.0f}s -> {path.relative_to(ROOT)}")
                runs.append((f"run{i}", ex))
        if not runs:
            ap.error("nothing to score: pass --stored, --runs N, or --json")
        report(case_id, gold, runs, args.verbose)


if __name__ == "__main__":
    main()
