"""Replay the post-model stages of the pipeline over every stored case.

The job store keeps the full extraction of every case that has run. The
half of the pipeline after the model — reconciliation merges, identity,
name reconciliation, income calculations, findings, scoring, and the
Cartograph payload — is deterministic and runs from that stored result in
seconds. This replays it for the whole corpus and summarises what each
case would deliver, so a rule change is judged on every packet seen so
far and not only on the one it was written for.

    python scripts/replay_corpus.py update     # write tests/corpus_baseline.json
    python scripts/replay_corpus.py check      # diff the current code against it
    python scripts/replay_corpus.py check -v   # and print each case's summary

What it cannot replay: OCR, classification, and the model reads (the
prompts). Those need a live run.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASELINE = ROOT / "tests" / "corpus_baseline.json"
logging.getLogger("app").setLevel(logging.ERROR)


def _db_path() -> Path:
    from app.core.dependencies import get_settings
    return Path(get_settings().audit_job_db)


def load_cases(db_path: Path) -> list[dict]:
    c = sqlite3.connect(str(db_path)); c.row_factory = sqlite3.Row
    rows = c.execute(
        "SELECT case_id, funding_program, cert_type, extraction_result FROM audit_jobs "
        "WHERE extraction_result IS NOT NULL ORDER BY case_id"
    ).fetchall()
    return [dict(r) for r in rows]


def replay_case(row: dict) -> dict:
    """Run the post-model stages on one stored extraction and summarise."""
    from app.core.config import Settings
    from app.schemas.context import PipelineContext
    from app.schemas.extraction import AssetEntry, ExtractionResult, VerificationIncomeEntry
    from app.services import extractor, pipeline
    from app.services.cartograph.adapter import build_payload
    from app.services.doc_taxonomy import is_current_certification_form
    from app.services.findings import dedupe as dedupe_findings, records as finding_records, render as render_findings
    from app.services.identity import resolve_identities
    from app.services.name_reconciler import reconcile_names

    ex = ExtractionResult.model_validate(json.loads(row["extraction_result"]))
    groups = ex.document_groups
    household, cert, income, assets = ex.household_demographics, ex.certification_info, ex.income, ex.assets
    page_text = {p.page: p.text or "" for p in ex.page_ocr}
    ocr_quality = {p.page: {"flag": p.flag or "green", "score": p.score, "text": p.text or ""} for p in ex.page_ocr}
    ctx = PipelineContext(
        funding_program=(row.get("funding_program") or "").strip() or None,
        certification_type=(cert.certificationType if cert else None),
    )

    # Post-model merges in the extractor (idempotent on reconciled records).
    vis = [v.model_dump() for v in income.sourceIncome.verificationIncome]
    for name in ("_merge_same_source_records",):
        fn = getattr(extractor, name, None)
        if fn:
            vis = fn(vis)
    income.sourceIncome.verificationIncome = [VerificationIncomeEntry.model_validate(v) for v in vis]
    arecs = [a.model_dump() for a in assets.assetInformation]
    for name in ("_dedupe_asset_records", "_merge_empty_verifications"):
        fn = getattr(extractor, name, None)
        if fn:
            arecs = fn(arecs)
    assets.assetInformation = [AssetEntry.model_validate(a) for a in arecs]

    findings: list = []
    if ex.questionnaire_disclosures and income:
        findings.extend(pipeline._link_questionnaire_to_income(ex.questionnaire_disclosures, income, groups))
    name_findings = reconcile_names(household, income, assets, groups)
    if household and household.houseHold:
        name_findings.extend(pipeline._deduplicate_household_members(household))
    income.sourceIncome.verificationIncome = pipeline._resolve_duplicate_self_declarations(income.sourceIncome.verificationIncome)
    income.sourceIncome.verificationIncome, merge_findings = pipeline._merge_household_level_sources(income.sourceIncome.verificationIncome)
    name_findings.extend(merge_findings)
    income.sourceIncome.verificationIncome, declared_findings = pipeline._collapse_declared_duplicates(income.sourceIncome.verificationIncome)
    name_findings.extend(declared_findings)
    identity_findings = resolve_identities(household, groups, page_text) if household and household.houseHold else []
    income_calculations = pipeline._compute_income_calculations(income, cert, ctx) if income else []

    findings.extend(pipeline._generate_findings(
        ex.classification, groups, household=household, certification_info=cert, income=income, assets=assets,
        inventory_financial=ex.document_inventory_financial, inventory_hud=ex.document_inventory_hud,
        income_calculations=income_calculations, questionnaire_disclosures=ex.questionnaire_disclosures,
        ctx=ctx, previous_certification=ex.previous_certification,
    ))
    findings.extend(name_findings)
    findings.extend(pipeline._rent_identity_findings(cert, groups))
    findings.extend(pipeline._reconciliation_findings(income, ctx))
    findings.extend(identity_findings)
    pipeline._calculation_and_compliance_findings(findings, income_calculations, cert, groups)
    cert_groups = [g for g in groups if is_current_certification_form(g.document_type)]
    score_summary = pipeline._score_and_note(
        findings, household=household, certification_info=cert, income=income, assets=assets,
        document_groups=groups, ocr_quality=ocr_quality, cert_groups=cert_groups, ctx=ctx,
    )
    deduped = dedupe_findings(findings)
    replayed = ex.model_copy(update={
        "income_calculations": income_calculations,
        "findings": render_findings(deduped),
        "finding_records": finding_records(deduped),
        "field_scores": score_summary,
    })
    adapted = build_payload(replayed, Settings(), case_ref=row["case_id"])
    p = adapted.payload
    cr = p.get("cert_review") or {}
    return {
        "codes": dict(sorted(Counter(f.code for f in replayed.finding_records).items())),
        "notes": sorted(t[:70] for t in replayed.findings if not t.startswith("[") and not any(t == f.text for f in replayed.finding_records)),
        "cert_review": {k: cr.get(k) for k in ("cert_type", "effective_date", "annual_income", "annual_assets", "tenant_rent", "gross_rent", "utility_allowance", "hh_size")},
        "members": [(m.get("first_name"), m.get("last_name"), m.get("relationship")) for m in p.get("household_members", [])],
        "income": _sorted_rows((r.get("income_type"), r.get("verification_status"), r.get("annual_amount"), (r.get("source_name") or "")[:30]) for r in p.get("income_records", [])),
        "assets": _sorted_rows((r.get("asset_type"), (r.get("institution_name") or "")[:25], r.get("current_value"), r.get("manual_balance"), r.get("bank_stmt_avg_balance"), r.get("verification_status")) for r in p.get("asset_records", [])),
        "warnings": sorted(w[:70] for w in adapted.warnings),
        "score": round(score_summary.overall_composite, 2),
    }


def _sorted_rows(rows) -> list:
    """Rows sorted with nulls treated as empty, so a missing value never
    makes a case unreplayable."""
    return sorted(rows, key=lambda t: tuple("" if x is None else str(x) for x in t))


def replay_all(db_path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for row in load_cases(db_path):
        try:
            out[row["case_id"]] = replay_case(row)
        except Exception as exc:  # a case that cannot replay is itself a signal
            out[row["case_id"]] = {"error": f"{type(exc).__name__}: {exc}"[:300]}
    return out


def diff(base: dict, now: dict) -> list[str]:
    lines: list[str] = []
    for case in sorted(set(base) | set(now)):
        b, n = base.get(case), now.get(case)
        if b is None:
            lines.append(f"{case}: new case (not in baseline)"); continue
        if n is None:
            lines.append(f"{case}: missing from this run"); continue
        for key in sorted(set(b) | set(n)):
            bv, nv = b.get(key), n.get(key)
            if json.dumps(bv, sort_keys=True, default=str) != json.dumps(nv, sort_keys=True, default=str):
                lines.append(f"{case} {key}:\n    before: {json.dumps(bv, default=str)[:400]}\n    after:  {json.dumps(nv, default=str)[:400]}")
    return lines


def main(argv: list[str]) -> int:
    mode = argv[0] if argv else "check"
    verbose = "-v" in argv
    db = _db_path()
    if not db.exists():
        print(f"no job store at {db}"); return 2
    now = replay_all(db)
    if mode == "update":
        BASELINE.write_text(json.dumps(now, indent=1, sort_keys=True, default=str) + "\n")
        print(f"baseline written: {len(now)} cases -> {BASELINE}"); return 0
    if not BASELINE.exists():
        print("no baseline; run `update` first"); return 2
    base = json.loads(BASELINE.read_text())
    lines = diff(base, now)
    if verbose:
        for case, summary in now.items():
            print(case, json.dumps(summary, default=str)[:600])
    print(f"{len(now)} cases replayed, {len(lines)} difference(s) against baseline")
    for l in lines:
        print(" -", l)
    return 1 if lines else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
