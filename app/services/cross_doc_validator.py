"""Cross-document validation — income/asset/household consistency (Sections 7, 8)."""

import logging

from app.schemas.extraction import (
    AssetExtraction,
    CertificationInfo,
    DocumentGroup,
    Finding,
    HouseholdDemographics,
    IncomeCalculationResult,
    IncomeExtraction,
)
from app.services.findings import (
    ASSIGN_CLIENT,
    ASSIGN_INTERNAL,
    CATEGORY_ASSET,
    CATEGORY_INCOME,
    CATEGORY_MEMBER,
    CATEGORY_UNIT_RENT,
    RESOLVE_PRESENCE,
    RESOLVE_RECALC,
    make_finding,
)

logger = logging.getLogger(__name__)


def _member_key(name: str | None) -> str:
    parts = (name or "").strip().lower().split()
    return f"{parts[0]} {parts[-1]}" if parts else ""


def _record_key(member: str | None, source: str | None, income_type: str | None) -> tuple[str, str, str]:
    """Identity of an income record: who, from whom, which program.

    Keyed on the payer name alone, three household members paid by the
    Social Security Administration collapsed into one bucket, the first
    value won, and a correct extraction was reported as a 53% shortfall
    against the certification total.
    """
    return (
        _member_key(member),
        (source or "").strip().lower(),
        (income_type or "").strip().lower(),
    )


def _record_label(member: str | None, source: str | None) -> str:
    who = (member or "").strip() or "unknown member"
    what = (source or "").strip() or "unknown source"
    return f"{who} — {what}"

# Threshold for flagging income discrepancies
_DISCREPANCY_THRESHOLD = 0.10  # 10%

# Above what absolute difference a small percentage gap stops being
# explicable as rounding. Rounding artifacts are bounded in dollars, not in
# percent: cents lost per source, a figure entered to the nearest dollar. A
# percentage band alone scales with household income, so on a $50,000
# certification it lets a $5,000 methodology difference — which month of a
# benefit was annualized, a source counted once or twice — be described as
# rounding and skimmed past. The percentage decides whether to look; this
# decides what to call what is found.
_ROUNDING_TOLERANCE = 100.0


def validate_income_consistency(
    income: IncomeExtraction | None,
    income_calculations: list[IncomeCalculationResult],
) -> list[Finding]:
    """Compare income calculation methods for significant discrepancies.

    Flags when self-declared vs VOI vs YTD vs paystub annual amounts differ > 10%.
    """
    findings: list[Finding] = []
    if not income_calculations:
        return findings

    # Group calculations by source
    by_source: dict[tuple[str, str, str], dict[str, float]] = {}
    labels: dict[tuple[str, str, str], tuple[str | None, str | None]] = {}
    for calc in income_calculations:
        key = _record_key(calc.memberName, calc.sourceName, calc.incomeType)
        labels.setdefault(key, (calc.memberName, calc.sourceName))
        if calc.annualIncome:
            try:
                by_source.setdefault(key, {})[calc.method or "unknown"] = float(calc.annualIncome)
            except ValueError:
                continue

    for key, methods in by_source.items():
        member_name, source_name = labels[key]
        source = _record_label(member_name, source_name)
        if len(methods) < 2:
            continue

        values = list(methods.values())
        if max(values) == 0:
            continue

        # Consolidate by outlier instead of emitting one finding per pair.
        # For each method, check its % deviation from the median of the others.
        # A method whose deviation exceeds the threshold against ALL other
        # methods is the outlier and produces a single finding.
        sorted_items = sorted(methods.items(), key=lambda kv: kv[1])
        outliers: list[tuple[str, float, list[tuple[str, float]]]] = []
        for method, val in sorted_items:
            others = [(m, v) for m, v in sorted_items if m != method]
            # An outlier deviates from a CONSENSUS — it needs at least 2 other
            # methods to stand apart from. With only one other method (e.g.
            # ytd-based vs voi-based) both would qualify as each other's
            # outlier, producing two mirror-image findings for one disagreement.
            # That case falls through to the single summary finding below.
            if len(others) < 2:
                continue
            diffs = [
                abs(val - v) / max(val, v) if max(val, v) > 0 else 0
                for _, v in others
            ]
            if all(d > _DISCREPANCY_THRESHOLD for d in diffs):
                outliers.append((method, val, others))

        # If every method is an "outlier", there's no consensus to deviate
        # from — emit the single summary finding instead of one per method.
        if outliers and len(outliers) < len(methods):
            # One finding per source, not per outlier. Two outlier methods on
            # the same source describe a single disagreement that one
            # recalculation resolves, and separate findings would collide on
            # identity anyway — a finding is keyed by the record it concerns.
            # With a single outlier the wording is unchanged.
            clauses: list[str] = []
            worst_pct = 0.0
            for method, val, others in outliers:
                others_str = ", ".join(f"{m} = ${v:,.2f}" for m, v in others)
                worst_pct = max(worst_pct, max(
                    abs(val - v) / max(val, v) if max(val, v) > 0 else 0
                    for _, v in others
                ))
                clauses.append(f"{method} = ${val:,.2f} disagrees with {others_str}")
            findings.append(make_finding(
                "INCOME_METHOD_OUTLIER",
                f"Income discrepancy for '{source}': {'; '.join(clauses)} "
                f"(up to {worst_pct:.0%} difference) — "
                f"review income calculation methods (Section 9)",
                label="An income calculation method disagrees with the others",
                category=CATEGORY_INCOME,
                subject_type="income_record",
                subject_ref={"member_name": member_name, "source_name": source_name},
                assignment=ASSIGN_INTERNAL,
                correction_required=(
                    "Determine which calculation method is correct for this "
                    "source and recompute the annual income"
                ),
                resolution_type=RESOLVE_RECALC,
            ))
        else:
            # No single outlier — methods disagree among themselves.
            # Emit ONE summary finding instead of N² pairs.
            max_val = max(values)
            min_val = min(values)
            if max_val > 0 and (max_val - min_val) / max_val > _DISCREPANCY_THRESHOLD:
                methods_str = ", ".join(
                    f"{m} = ${v:,.2f}" for m, v in sorted_items
                )
                findings.append(make_finding(
                    "INCOME_METHODS_DISAGREE",
                    f"Income discrepancy for '{source}': methods disagree "
                    f"({methods_str}) — review income calculation methods (Section 9)",
                    label="No income calculation method agrees with any other",
                    category=CATEGORY_INCOME,
                    subject_type="income_record",
                    subject_ref={"member_name": member_name, "source_name": source_name},
                    assignment=ASSIGN_INTERNAL,
                    correction_required=(
                        "Establish which verification is authoritative for this "
                        "source and recompute the annual income"
                    ),
                    resolution_type=RESOLVE_RECALC,
                ))

    return findings


def validate_duplicate_income(
    income: IncomeExtraction | None,
    certification_info: CertificationInfo | None = None,
) -> list[Finding]:
    """Detect duplicate income records with identical key fields.

    Two records with same source, member, rate, and frequency are likely duplicates
    (e.g., same business income extracted twice). Cross-references TIC total to
    determine if the duplicate is expected (e.g., 2 × $1,734 = $3,468 in TIC column A).
    """
    findings: list[Finding] = []
    if not income:
        return findings

    vi_entries = income.sourceIncome.verificationIncome
    if len(vi_entries) < 2:
        return findings

    # Build signature for each entry
    seen: dict[str, list[int]] = {}
    for i, vi in enumerate(vi_entries):
        sig = (
            (vi.sourceName or "").lower().strip(),
            (vi.memberName or "").lower().strip(),
            (vi.rateOfPay or "").strip(),
            (vi.frequencyOfPay or "").lower().strip(),
            (vi.incomeType or "").lower().strip(),
        )
        key = "|".join(sig)
        if key and any(sig):  # skip fully-empty records
            seen.setdefault(key, []).append(i)

    for key, indices in seen.items():
        if len(indices) < 2:
            continue

        vi = vi_entries[indices[0]]
        source = vi.sourceName or vi.incomeType or "Unknown"
        member = vi.memberName or "Unknown"
        rate = vi.rateOfPay or "?"

        note = ""
        if certification_info and certification_info.householdIncome:
            note = " — cross-check against TIC total income to verify"

        findings.append(make_finding(
            "DUPLICATE_INCOME_RECORD",
            f"Potential duplicate income: {len(indices)} records for "
            f"'{member}' at '{source}' with rate {rate}/{vi.frequencyOfPay or '?'} "
            f"({vi.incomeType or '?'}){note}",
            label="Income source recorded more than once with identical fields",
            category=CATEGORY_INCOME,
            subject_type="income_record",
            subject_ref={
                "member_name": vi.memberName,
                "source_name": vi.sourceName,
                "income_type": vi.incomeType,
            },
            assignment=ASSIGN_INTERNAL,
            correction_required=(
                "Confirm whether the household genuinely holds more than one "
                "record for this source; delete the duplicate and recompute"
            ),
            resolution_type=RESOLVE_RECALC,
        ))

    # Also detect near-duplicates: same incomeType + same member + similar amount
    # (catches parser + LLM extracting the same source with slightly different field values)
    type_member_groups: dict[str, list[int]] = {}
    for i, vi in enumerate(vi_entries):
        group_key = f"{(vi.incomeType or '').lower()}|{(vi.memberName or '').lower()}"
        if group_key.strip("|"):
            type_member_groups.setdefault(group_key, []).append(i)

    for group_key, indices in type_member_groups.items():
        if len(indices) < 2:
            continue
        # Check if any pair already caught by exact match
        sig_keys = set()
        for i in indices:
            vi = vi_entries[i]
            sig = "|".join((
                (vi.sourceName or "").lower().strip(),
                (vi.memberName or "").lower().strip(),
                (vi.rateOfPay or "").strip(),
                (vi.frequencyOfPay or "").lower().strip(),
                (vi.incomeType or "").lower().strip(),
            ))
            sig_keys.add(sig)
        if len(sig_keys) == 1:
            continue  # Already caught by exact match above

        # Check amounts — if selfDeclaredAmount or rateOfPay are similar
        amounts = []
        for i in indices:
            vi = vi_entries[i]
            amt = _parse_money(vi.selfDeclaredAmount) or _parse_money(vi.rateOfPay)
            if amt:
                amounts.append((i, amt))

        if len(amounts) >= 2:
            # Collect every qualifying pair, then emit once for the group. Three
            # near-duplicate records produce three pairs but describe a single
            # problem with a single subject, so one finding carries them all.
            pairs: list[tuple[float, float]] = []
            subject_vi = None
            for a_idx in range(len(amounts)):
                for b_idx in range(a_idx + 1, len(amounts)):
                    i_a, amt_a = amounts[a_idx]
                    i_b, amt_b = amounts[b_idx]
                    if max(amt_a, amt_b) <= 0:
                        continue
                    ratio = min(amt_a, amt_b) / max(amt_a, amt_b)
                    if ratio <= 0.80:  # amounts differ > 20%
                        continue
                    vi_a = vi_entries[i_a]
                    vi_b = vi_entries[i_b]
                    # Sequential employment (one terminated, one active) is a
                    # job change, not a duplicate. Common on IR recerts.
                    status_a = (vi_a.employmentStatus or "").lower()
                    status_b = (vi_b.employmentStatus or "").lower()
                    if ("terminated" in status_a) != ("terminated" in status_b):
                        continue
                    # Different employers (distinct sourceName) are not
                    # duplicates even if rates happen to be similar.
                    src_a = (vi_a.sourceName or "").lower().strip()
                    src_b = (vi_b.sourceName or "").lower().strip()
                    if src_a and src_b and src_a != src_b:
                        continue
                    subject_vi = subject_vi or vi_a
                    pairs.append((amt_a, amt_b))

            if pairs and subject_vi is not None:
                pairs_str = "; ".join(
                    f"${a:,.2f} vs ${b:,.2f}" for a, b in pairs
                )
                count = "two" if len(pairs) == 1 else "several"
                findings.append(make_finding(
                    "NEAR_DUPLICATE_INCOME",
                    f"Near-duplicate income: '{subject_vi.memberName}' has {count} "
                    f"'{subject_vi.incomeType}' records — "
                    f"{pairs_str} — "
                    f"possibly extracted from both TIC and verification document",
                    label="Similar income records of the same type for one member",
                    category=CATEGORY_INCOME,
                    subject_type="income_record",
                    subject_ref={
                        "member_name": subject_vi.memberName,
                        "income_type": subject_vi.incomeType,
                    },
                    assignment=ASSIGN_INTERNAL,
                    correction_required=(
                        "Determine whether these are one source extracted twice "
                        "or genuinely separate income; remove any duplicate and "
                        "recompute"
                    ),
                    resolution_type=RESOLVE_RECALC,
                ))

    return findings


def validate_asset_consistency(
    assets: AssetExtraction | None,
) -> list[Finding]:
    """Compare self-declared asset balances against verified amounts."""
    findings: list[Finding] = []
    if not assets:
        return findings

    for asset in assets.assetInformation:
        self_declared = _parse_money(asset.selfDeclaredAmount)
        verified = _parse_money(asset.currentBalance)

        if self_declared is not None and verified is not None:
            if verified == 0 and self_declared == 0:
                continue
            max_val = max(abs(self_declared), abs(verified))
            if max_val > 0:
                diff_pct = abs(self_declared - verified) / max_val
                if diff_pct > _DISCREPANCY_THRESHOLD:
                    findings.append(make_finding(
                        "ASSET_SELF_DECLARED_VS_VERIFIED",
                        f"Asset discrepancy for '{asset.sourceName or 'Unknown'}' "
                        f"({asset.accountType or 'Unknown'}): self-declared = ${self_declared:,.2f} vs "
                        f"verified = ${verified:,.2f} ({diff_pct:.0%} difference) — "
                        f"review asset worksheet (Section 7)",
                        label="Self-declared asset balance differs from the verified balance",
                        category=CATEGORY_ASSET,
                        subject_type="asset_record",
                        subject_ref={
                            "member_name": asset.assetOwner,
                            "source_name": asset.sourceName,
                            "account_type": asset.accountType,
                        },
                        assignment=ASSIGN_INTERNAL,
                        correction_required=(
                            "Use the verified balance on the asset worksheet and "
                            "recompute income from assets"
                        ),
                        resolution_type=RESOLVE_RECALC,
                    ))

    return findings


def validate_household_consistency(
    household: HouseholdDemographics | None,
    income: IncomeExtraction | None,
    assets: AssetExtraction | None,
) -> list[Finding]:
    """Check that names on income/asset docs match the household roster."""
    findings: list[Finding] = []
    if not household or not household.houseHold:
        return findings

    # Build set of known household member names (lowercase)
    hh_names: set[str] = set()
    for m in household.houseHold:
        full = f"{(m.FirstName or '')} {(m.LastName or '')}".strip().lower()
        if full:
            hh_names.add(full)
        # Also add first name only for fuzzy matching
        if m.FirstName:
            hh_names.add(m.FirstName.lower())

    # Check income records
    if income:
        for vi in income.sourceIncome.verificationIncome:
            name = (vi.memberName or "").lower().strip()
            if name and not _name_in_household(name, hh_names):
                findings.append(make_finding(
                    "INCOME_MEMBER_NOT_IN_ROSTER",
                    f"Income record for '{vi.memberName}' at '{vi.sourceName}' — "
                    f"person not found in household roster. Verify household composition (Section 8)",
                    label="Income earner is not on the household roster",
                    category=CATEGORY_MEMBER,
                    subject_type="income_record",
                    subject_ref={
                        "member_name": vi.memberName,
                        "source_name": vi.sourceName,
                    },
                    assignment=ASSIGN_INTERNAL,
                    correction_required=(
                        "Add the person to the household roster, or reassign the "
                        "income record to the member it belongs to"
                    ),
                    resolution_type=RESOLVE_RECALC,
                ))

        for ps in income.sourceIncome.payStub:
            name = (ps.memberName or "").lower().strip()
            if name and not _name_in_household(name, hh_names):
                findings.append(make_finding(
                    "PAYSTUB_MEMBER_NOT_IN_ROSTER",
                    f"Pay stub for '{ps.memberName}' from '{ps.sourceName}' — "
                    f"person not found in household roster. Verify household composition (Section 8)",
                    label="Paystub earner is not on the household roster",
                    category=CATEGORY_MEMBER,
                    subject_type="income_record",
                    subject_ref={
                        "member_name": ps.memberName,
                        "source_name": ps.sourceName,
                    },
                    assignment=ASSIGN_INTERNAL,
                    correction_required=(
                        "Add the person to the household roster, or reassign the "
                        "paystub to the member it belongs to"
                    ),
                    resolution_type=RESOLVE_RECALC,
                ))

    # Check asset records
    if assets:
        for asset in assets.assetInformation:
            name = (asset.assetOwner or "").lower().strip()
            if name and not _name_in_household(name, hh_names):
                findings.append(make_finding(
                    "ASSET_OWNER_NOT_IN_ROSTER",
                    f"Asset record for '{asset.assetOwner}' at '{asset.sourceName}' — "
                    f"person not found in household roster. Verify household composition (Section 8)",
                    label="Asset owner is not on the household roster",
                    category=CATEGORY_MEMBER,
                    subject_type="asset_record",
                    subject_ref={
                        "member_name": asset.assetOwner,
                        "source_name": asset.sourceName,
                    },
                    assignment=ASSIGN_INTERNAL,
                    correction_required=(
                        "Add the person to the household roster, or reassign the "
                        "asset to the member it belongs to"
                    ),
                    resolution_type=RESOLVE_RECALC,
                ))

    return findings


def validate_asset_worksheet_rules(
    assets: AssetExtraction | None,
    document_groups: list[DocumentGroup],
) -> list[Finding]:
    """Section 7 asset worksheet checks."""
    findings: list[Finding] = []
    if not assets:
        return findings

    doc_types = {g.document_type for g in document_groups if g.category != "ignore"}

    # Check for zero-asset scenario: no assets extracted but no "No Asset Certification"
    if not assets.assetInformation:
        has_no_asset_cert = any("No Asset" in dt for dt in doc_types)
        has_asset_doc = any(
            dt for dt in doc_types
            if any(kw in dt.lower() for kw in ("bank statement", "voa", "verification of asset"))
        )
        if not has_no_asset_cert and not has_asset_doc:
            findings.append(make_finding(
                "NO_ASSET_CERT_MISSING",
                "No assets extracted and no 'No Asset Certification' found — "
                "zero-asset certification record required if household has no assets (Section 7)",
                label="Zero-asset household with no asset certification on file",
                category=CATEGORY_ASSET,
                assignment=ASSIGN_CLIENT,
                correction_required=(
                    "Obtain a signed No Asset Certification, or supply the asset "
                    "documentation that is missing from the packet"
                ),
                resolution_type=RESOLVE_PRESENCE,
            ))

    # Check for joint/shared accounts without percentage of ownership
    for asset in assets.assetInformation:
        if asset.percentageOfOwnership:
            try:
                pct = float(asset.percentageOfOwnership)
                if 0 < pct < 100:
                    findings.append(make_finding(
                        "JOINT_ACCOUNT_OWNERSHIP_PCT",
                        f"Joint account at '{asset.sourceName or 'Unknown'}' with "
                        f"{pct}% ownership — verify asset values are adjusted by "
                        f"ownership percentage (Section 7)",
                        label="Jointly owned asset may not be prorated to the household share",
                        category=CATEGORY_ASSET,
                        subject_type="asset_record",
                        subject_ref={
                            "member_name": asset.assetOwner,
                            "source_name": asset.sourceName,
                            "account_type": asset.accountType,
                        },
                        assignment=ASSIGN_INTERNAL,
                        correction_required=(
                            "Apply the ownership percentage to the asset value and "
                            "recompute income from assets"
                        ),
                        resolution_type=RESOLVE_RECALC,
                    ))
            except ValueError:
                pass

    return findings


def validate_rent_assistance(
    certification_info: CertificationInfo | None,
    document_groups: list[DocumentGroup],
) -> list[Finding]:
    """Check rent assistance documents against TIC fields.

    If a HomeBASE, Section 8, or other assistance document is present,
    the TIC should show a non-zero assistance amount.
    """
    findings: list[Finding] = []
    if not certification_info:
        return findings

    # Check for assistance-related documents
    assistance_docs = []
    for g in document_groups:
        if g.category == "ignore":
            continue
        dt_lower = g.document_type.lower()
        # Only match on classified document type — not raw text content
        # to avoid false positives from generic "assistance" mentions
        if any(kw in dt_lower for kw in (
            "homebase", "rental assistance verification", "housing voucher",
            "rent subsidy verification",
        )):
            assistance_docs.append(g.document_type)

    if not assistance_docs:
        return findings

    # Both fields were read off __dict__ under underscore-prefixed names that
    # nothing in the codebase ever wrote, so they were always None and the
    # finding below fired on every packet carrying an assistance document —
    # telling a reviewer the certification shows $0 without having read the
    # certification at all.
    non_fed = certification_info.nonFederalRentAssistance
    fed = certification_info.federalRentAssistance

    if non_fed is None and fed is None:
        # Not extracted rather than recorded as zero. The check cannot run,
        # and saying nothing is right: an assertion about a value nobody read
        # is worse than a gap, because it looks like evidence.
        logger.info(
            "Rent assistance documents present (%s) but the certification "
            "carries no assistance figure — nothing to compare against",
            ", ".join(assistance_docs),
        )
        return findings

    # Reuse the module's own parser rather than float() on a raw string:
    # "1,250.00" and "$0.00" are both shapes a certification prints.
    non_fed_val = _parse_money(non_fed) or 0.0
    fed_val = _parse_money(fed) or 0.0

    if non_fed_val == 0 and fed_val == 0:
        findings.append(make_finding(
            "RENT_ASSISTANCE_NOT_ON_TIC",
            f"Rent assistance document(s) present ({', '.join(assistance_docs)}) "
            f"but TIC shows $0 for both federal and non-federal rent assistance — "
            f"verify if assistance amount should be recorded on TIC Part VI",
            label="Rent assistance documented but not recorded on the certification",
            category=CATEGORY_UNIT_RENT,
            assignment=ASSIGN_INTERNAL,
            correction_required=(
                "Record the assistance amount on the certification, or confirm "
                "the assistance did not apply for this period"
            ),
            resolution_type=RESOLVE_RECALC,
        ))

    return findings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _name_in_household(name: str, hh_names: set[str]) -> bool:
    """Check if a name matches any household member (fuzzy)."""
    if name in hh_names:
        return True
    # Check if any household name contains this name or vice versa
    for hh in hh_names:
        if name in hh or hh in name:
            return True
        # Token overlap
        name_tokens = set(name.split())
        hh_tokens = set(hh.split())
        if name_tokens and hh_tokens:
            overlap = len(name_tokens & hh_tokens)
            if overlap >= 1 and overlap / min(len(name_tokens), len(hh_tokens)) >= 0.5:
                return True
    return False


def validate_tic_totals(
    certification_info: CertificationInfo | None,
    income: IncomeExtraction | None,
    income_calculations: list[IncomeCalculationResult],
) -> list[Finding]:
    """Cross-reference TIC/HUD 50059 total income against sum of individual sources.

    The certification form declares a household income total. The sum of all
    extracted individual income sources should approximately match. Discrepancies
    indicate missing sources, duplicate extraction, or type misidentification.
    """
    findings: list[Finding] = []
    if not certification_info or not certification_info.householdIncome:
        # No total to compare against
        if income and income.sourceIncome.verificationIncome:
            findings.append(make_finding(
                "TIC_TOTAL_NOT_EXTRACTED",
                "Household income total not extracted from certification form — "
                "cannot cross-validate individual income sources against declared total",
                label="Certification income total unavailable for cross-validation",
                category=CATEGORY_INCOME,
                # An extraction gap on our side, not a compliance failure by the
                # file. It suppresses a check rather than failing one.
                result="na",
                assignment=ASSIGN_INTERNAL,
                correction_required=(
                    "Confirm the certification total by hand; the comparison "
                    "against individual sources did not run"
                ),
                resolution_type=RESOLVE_PRESENCE,
            ))
        return findings

    tic_total = _parse_money(certification_info.householdIncome)
    if tic_total is None or tic_total == 0:
        return findings

    # Strategy 1: Use income_calculations (best method per record)
    best_by_source: dict[tuple[str, str, str], float] = {}
    best_method: dict[tuple[str, str, str], int] = {}
    key_labels: dict[tuple[str, str, str], str] = {}
    _METHOD_PRIORITY = {"voi-based": 0, "self-declared": 1, "ytd-based": 2, "paystub-based": 3}
    for calc in income_calculations:
        if not calc.annualIncome:
            continue
        # [audit] rows duplicate a primary method; [historical] rows are
        # stale EIV/Work Number wage history (job likely ended) — neither
        # belongs in the current-income total held against the TIC.
        if (calc.details or "").startswith(("[audit]", "[historical]")):
            continue
        try:
            val = float(calc.annualIncome)
        except ValueError:
            continue
        key = _record_key(calc.memberName, calc.sourceName, calc.incomeType)
        key_labels.setdefault(key, _record_label(calc.memberName, calc.sourceName))
        priority = _METHOD_PRIORITY.get(calc.method or "", 99)
        if key not in best_by_source or priority < best_method[key]:
            best_by_source[key] = val
            best_method[key] = priority

    # Strategy 2: If no calculations, sum selfDeclaredAmount from VI entries
    # Annualize based on frequencyOfPay — selfDeclaredAmount is often a
    # monthly SSA/pension amount, not an annual figure.
    if not best_by_source and income:
        from app.services.income_calculator import get_frequency_multiplier
        for vi in income.sourceIncome.verificationIncome:
            sd = _parse_money(vi.selfDeclaredAmount)
            if not sd or sd <= 0:
                continue
            mult = get_frequency_multiplier(vi.frequencyOfPay) if vi.frequencyOfPay else None
            annual = sd * mult if mult else sd
            key = _record_key(vi.memberName, vi.sourceName, vi.incomeType)
            key_labels.setdefault(key, _record_label(vi.memberName, vi.sourceName))
            best_by_source.setdefault(key, 0)
            best_by_source[key] += annual

    # Strategy 3: Sum rateOfPay × frequency
    if not best_by_source and income:
        from app.services.income_calculator import get_frequency_multiplier, _classify_income_mode
        for vi in income.sourceIncome.verificationIncome:
            rate = _parse_money(vi.rateOfPay)
            if not rate:
                continue
            key = _record_key(vi.memberName, vi.sourceName, vi.incomeType)
            key_labels.setdefault(key, _record_label(vi.memberName, vi.sourceName))
            mode = _classify_income_mode((vi.incomeType or "").lower())
            if mode == "fixed_monthly":
                best_by_source[key] = rate * 12
            elif mode == "annual_net":
                best_by_source[key] = rate
            elif vi.frequencyOfPay:
                mult = get_frequency_multiplier(vi.frequencyOfPay)
                if mult:
                    best_by_source[key] = rate * mult

    calc_total = sum(best_by_source.values())

    if calc_total == 0:
        findings.append(make_finding(
            "TIC_TOTAL_NO_CALCULATIONS",
            f"TIC declares household income ${tic_total:,.2f} but no individual income "
            f"calculations produced results — verify all income sources extracted",
            label="Certification declares income but no source calculation succeeded",
            category=CATEGORY_INCOME,
            # Again an extraction gap: the declared total is present, our side
            # produced nothing to hold against it.
            result="na",
            assignment=ASSIGN_INTERNAL,
            correction_required=(
                "Verify the income sources by hand; none were calculable from "
                "the packet"
            ),
            resolution_type=RESOLVE_PRESENCE,
        ))
        return findings

    diff = abs(tic_total - calc_total)
    diff_pct = diff / tic_total if tic_total > 0 else 0

    # Per-record disagreements between what the certification declares for
    # a source and what its verification computes are raised with a precise
    # subject by validate_cert_summary_vs_income. Whatever part of the total
    # gap those already explain is not reported again at case level, where
    # it would repaint every income record.
    explained = 0.0
    if income:
        for vi in income.sourceIncome.verificationIncome:
            declared = _parse_money(vi.declaredAnnualAmount)
            if declared is None:
                continue
            key = _record_key(vi.memberName, vi.sourceName, vi.incomeType)
            calc_val = best_by_source.get(key)
            if calc_val is not None and abs(declared - calc_val) / max(declared, 1.0) > 0.10:
                explained += abs(declared - calc_val)
    residual = max(0.0, diff - explained)
    residual_pct = residual / tic_total if tic_total > 0 else 0

    if diff_pct > 0.15 and residual_pct <= 0.15:
        logger.info(
            "TIC total: %.0f%% gap of $%.2f is explained by per-record declared-vs-"
            "calculated differences ($%.2f) — no case-level finding",
            diff_pct * 100, diff, explained,
        )
    elif diff_pct > 0.15:
        direction = "higher" if calc_total > tic_total else "lower"
        source_detail = ", ".join(
            f"{key_labels.get(k, '?')}: ${v:,.0f}" for k, v in best_by_source.items()
        )
        findings.append(make_finding(
            "TIC_TOTAL_MISMATCH",
            f"Income total mismatch: TIC declares ${tic_total:,.2f} but extracted sources "
            f"sum to ${calc_total:,.2f} ({diff_pct:.0%} {direction}). "
            f"Sources: [{source_detail}]. "
            f"Possible missing/duplicate income source — review Section 9",
            label="Household income total disagrees with the sum of its sources",
            category=CATEGORY_INCOME,
            assignment=ASSIGN_INTERNAL,
            correction_required=(
                "Locate the missing or duplicated income source and recompute "
                "the household total"
            ),
            resolution_type=RESOLVE_RECALC,
        ))
    elif diff_pct > 0.05:
        if diff <= _ROUNDING_TOLERANCE:
            findings.append(make_finding(
                "TIC_TOTAL_MINOR_DIFF",
                f"Minor income discrepancy: TIC ${tic_total:,.2f} vs calculated "
                f"${calc_total:,.2f} (${diff:,.2f}, {diff_pct:.0%}) — "
                f"consistent with rounding",
                label="Declared and calculated household income differ by a rounding amount",
                category=CATEGORY_INCOME,
                result="na",
                assignment=ASSIGN_INTERNAL,
                resolution_type=RESOLVE_PRESENCE,
            ))
        else:
            findings.append(make_finding(
                "TIC_TOTAL_MINOR_DIFF",
                f"Income discrepancy: TIC ${tic_total:,.2f} vs calculated "
                f"${calc_total:,.2f} — a difference of ${diff:,.2f} ({diff_pct:.0%}). "
                f"Too large to be rounding; identify which source accounts for it",
                label="Declared and calculated household income disagree",
                category=CATEGORY_INCOME,
                assignment=ASSIGN_INTERNAL,
                correction_required=(
                    "Identify the source responsible for the difference and "
                    "confirm which figure is correct"
                ),
                resolution_type=RESOLVE_RECALC,
            ))

    return findings


def validate_cert_summary_vs_income(
    income: IncomeExtraction | None,
    income_calculations: list[IncomeCalculationResult],
    document_groups: list[DocumentGroup] | None = None,
) -> list[Finding]:
    """Compare what the certification declares for each income source with
    what its verification computes.

    The declared figure comes from the reconciled declared bucket
    (VerificationIncomeEntry.declaredAnnualAmount, set when a row of the
    certification's income table matched the record). This replaced a regex
    over the OCR'd table that required a name in the first cell, which no
    real form prints — the forms key rows by member number — so the check
    had never fired on a real layout. The finding names the record, so the
    scorer lowers that record rather than the whole case.
    """
    findings: list[Finding] = []
    if not income:
        return findings
    calc_by_key: dict[tuple[str, str, str], float] = {}
    _METHOD_PRIORITY = {"voi-based": 0, "self-declared": 1, "ytd-based": 2, "paystub-based": 3}
    best_method: dict[tuple[str, str, str], int] = {}
    for calc in income_calculations:
        if not calc.annualIncome or (calc.details or "").startswith(("[audit]", "[historical]")):
            continue
        try:
            val = float(calc.annualIncome)
        except ValueError:
            continue
        key = _record_key(calc.memberName, calc.sourceName, calc.incomeType)
        priority = _METHOD_PRIORITY.get(calc.method or "", 99)
        if key not in calc_by_key or priority < best_method[key]:
            calc_by_key[key] = val
            best_method[key] = priority

    for vi in income.sourceIncome.verificationIncome:
        declared = _parse_money(vi.declaredAnnualAmount)
        if declared is None or declared <= 0:
            continue
        if vi.verificationStatus == "declared_only":
            continue   # nothing verified to compare against; reported separately
        key = _record_key(vi.memberName, vi.sourceName, vi.incomeType)
        calc_val = calc_by_key.get(key)
        if calc_val is None:
            continue
        diff = abs(calc_val - declared)
        diff_pct = diff / declared
        if diff_pct <= 0.10:
            continue
        direction = "higher" if calc_val > declared else "lower"
        where = vi.declaredSource or "certification"
        findings.append(make_finding(
            "CERT_SUMMARY_INCOME_MISMATCH",
            f"Income mismatch for {vi.memberName or 'a member'} from {vi.sourceName or 'a source'}: "
            f"the {where} declares ${declared:,.2f}/year but the verification computes "
            f"${calc_val:,.2f} ({diff_pct:.0%} {direction}) — verify the rate, frequency and "
            f"hours on the source document against the certification (Section 9)",
            label="Calculated income disagrees with what the certification declares for this source",
            category=CATEGORY_INCOME,
            subject_type="income_record",
            subject_ref={
                "member_name": vi.memberName,
                "source_name": vi.sourceName,
                "field": "declaredAnnualAmount",
            },
            assignment=ASSIGN_INTERNAL,
            correction_required=(
                "Re-verify the rate, frequency and hours on the verification "
                "against the certification, then recompute"
            ),
            resolution_type=RESOLVE_RECALC,
            pages=list(vi.sourcePages or []),
        ))
    return findings


def _parse_money(value: str | None) -> float | None:
    """Parse a monetary string."""
    if not value:
        return None
    try:
        return float(value.replace("$", "").replace(",", "").strip())
    except ValueError:
        return None
