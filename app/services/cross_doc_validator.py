"""Cross-document validation — income/asset/household consistency (Sections 7, 8)."""

import logging

from app.schemas.extraction import (
    AssetExtraction,
    CertificationInfo,
    DocumentGroup,
    HouseholdDemographics,
    IncomeCalculationResult,
    IncomeExtraction,
)

logger = logging.getLogger(__name__)

# Threshold for flagging income discrepancies
_DISCREPANCY_THRESHOLD = 0.10  # 10%


def validate_income_consistency(
    income: IncomeExtraction | None,
    income_calculations: list[IncomeCalculationResult],
) -> list[str]:
    """Compare income calculation methods for significant discrepancies.

    Flags when self-declared vs VOI vs YTD vs paystub annual amounts differ > 10%.
    """
    findings: list[str] = []
    if not income_calculations:
        return findings

    # Group calculations by source
    by_source: dict[str, dict[str, float]] = {}
    for calc in income_calculations:
        key = (calc.sourceName or "Unknown").lower()
        if calc.annualIncome:
            try:
                by_source.setdefault(key, {})[calc.method or "unknown"] = float(calc.annualIncome)
            except ValueError:
                continue

    for source, methods in by_source.items():
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
            # Emit one finding per outlier, listing all the others it disagrees with.
            for method, val, others in outliers:
                others_str = ", ".join(f"{m} = ${v:,.2f}" for m, v in others)
                max_pct = max(
                    abs(val - v) / max(val, v) if max(val, v) > 0 else 0
                    for _, v in others
                )
                findings.append(
                    f"Income discrepancy for '{source}': {method} = ${val:,.2f} "
                    f"disagrees with {others_str} (up to {max_pct:.0%} difference) — "
                    f"review income calculation methods (Section 9)"
                )
        else:
            # No single outlier — methods disagree among themselves.
            # Emit ONE summary finding instead of N² pairs.
            max_val = max(values)
            min_val = min(values)
            if max_val > 0 and (max_val - min_val) / max_val > _DISCREPANCY_THRESHOLD:
                methods_str = ", ".join(
                    f"{m} = ${v:,.2f}" for m, v in sorted_items
                )
                findings.append(
                    f"Income discrepancy for '{source}': methods disagree "
                    f"({methods_str}) — review income calculation methods (Section 9)"
                )

    return findings


def validate_duplicate_income(
    income: IncomeExtraction | None,
    certification_info: CertificationInfo | None = None,
) -> list[str]:
    """Detect duplicate income records with identical key fields.

    Two records with same source, member, rate, and frequency are likely duplicates
    (e.g., same business income extracted twice). Cross-references TIC total to
    determine if the duplicate is expected (e.g., 2 × $1,734 = $3,468 in TIC column A).
    """
    findings: list[str] = []
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

        findings.append(
            f"Potential duplicate income: {len(indices)} records for "
            f"'{member}' at '{source}' with rate {rate}/{vi.frequencyOfPay or '?'} "
            f"({vi.incomeType or '?'}){note}"
        )

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
                    findings.append(
                        f"Near-duplicate income: '{vi_a.memberName}' has two "
                        f"'{vi_a.incomeType}' records — "
                        f"${amt_a:,.2f} vs ${amt_b:,.2f} — "
                        f"possibly extracted from both TIC and verification document"
                    )

    return findings


def validate_asset_consistency(
    assets: AssetExtraction | None,
) -> list[str]:
    """Compare self-declared asset balances against verified amounts."""
    findings: list[str] = []
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
                    findings.append(
                        f"Asset discrepancy for '{asset.sourceName or 'Unknown'}' "
                        f"({asset.accountType or 'Unknown'}): self-declared = ${self_declared:,.2f} vs "
                        f"verified = ${verified:,.2f} ({diff_pct:.0%} difference) — "
                        f"review asset worksheet (Section 7)"
                    )

    return findings


def validate_household_consistency(
    household: HouseholdDemographics | None,
    income: IncomeExtraction | None,
    assets: AssetExtraction | None,
) -> list[str]:
    """Check that names on income/asset docs match the household roster."""
    findings: list[str] = []
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
                findings.append(
                    f"Income record for '{vi.memberName}' at '{vi.sourceName}' — "
                    f"person not found in household roster. Verify household composition (Section 8)"
                )

        for ps in income.sourceIncome.payStub:
            name = (ps.memberName or "").lower().strip()
            if name and not _name_in_household(name, hh_names):
                findings.append(
                    f"Pay stub for '{ps.memberName}' from '{ps.sourceName}' — "
                    f"person not found in household roster. Verify household composition (Section 8)"
                )

    # Check asset records
    if assets:
        for asset in assets.assetInformation:
            name = (asset.assetOwner or "").lower().strip()
            if name and not _name_in_household(name, hh_names):
                findings.append(
                    f"Asset record for '{asset.assetOwner}' at '{asset.sourceName}' — "
                    f"person not found in household roster. Verify household composition (Section 8)"
                )

    return findings


def validate_asset_worksheet_rules(
    assets: AssetExtraction | None,
    document_groups: list[DocumentGroup],
) -> list[str]:
    """Section 7 asset worksheet checks."""
    findings: list[str] = []
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
            findings.append(
                "No assets extracted and no 'No Asset Certification' found — "
                "zero-asset certification record required if household has no assets (Section 7)"
            )

    # Check for joint/shared accounts without percentage of ownership
    for asset in assets.assetInformation:
        if asset.percentageOfOwnership:
            try:
                pct = float(asset.percentageOfOwnership)
                if 0 < pct < 100:
                    findings.append(
                        f"Joint account at '{asset.sourceName or 'Unknown'}' with "
                        f"{pct}% ownership — verify asset values are adjusted by "
                        f"ownership percentage (Section 7)"
                    )
            except ValueError:
                pass

    return findings


def validate_rent_assistance(
    certification_info: CertificationInfo | None,
    document_groups: list[DocumentGroup],
) -> list[str]:
    """Check rent assistance documents against TIC fields.

    If a HomeBASE, Section 8, or other assistance document is present,
    the TIC should show a non-zero assistance amount.
    """
    findings: list[str] = []
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

    # Check TIC rent assistance fields
    non_fed = certification_info.__dict__.get("_nonFederalAssistance")
    fed = certification_info.__dict__.get("_federalAssistance")
    non_fed_val = float(non_fed) if non_fed and non_fed != "0" else 0
    fed_val = float(fed) if fed and fed != "0" else 0

    if non_fed_val == 0 and fed_val == 0:
        findings.append(
            f"Rent assistance document(s) present ({', '.join(assistance_docs)}) "
            f"but TIC shows $0 for both federal and non-federal rent assistance — "
            f"verify if assistance amount should be recorded on TIC Part VI"
        )

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
) -> list[str]:
    """Cross-reference TIC/HUD 50059 total income against sum of individual sources.

    The certification form declares a household income total. The sum of all
    extracted individual income sources should approximately match. Discrepancies
    indicate missing sources, duplicate extraction, or type misidentification.
    """
    findings: list[str] = []
    if not certification_info or not certification_info.householdIncome:
        # No total to compare against
        if income and income.sourceIncome.verificationIncome:
            findings.append(
                "Household income total not extracted from certification form — "
                "cannot cross-validate individual income sources against declared total"
            )
        return findings

    tic_total = _parse_money(certification_info.householdIncome)
    if tic_total is None or tic_total == 0:
        return findings

    # Strategy 1: Use income_calculations (best method per source)
    best_by_source: dict[str, float] = {}
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
        key = (calc.sourceName or calc.memberName or "unknown").lower()
        method = calc.method or ""
        priority = _METHOD_PRIORITY.get(method, 99)
        if key not in best_by_source or priority < _METHOD_PRIORITY.get(
            _best_method_for_key(key, income_calculations, best_by_source), 99
        ):
            best_by_source[key] = val

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
            key = (vi.sourceName or vi.incomeType or "unknown").lower()
            best_by_source.setdefault(key, 0)
            best_by_source[key] += annual

    # Strategy 3: Sum rateOfPay × frequency
    if not best_by_source and income:
        from app.services.income_calculator import get_frequency_multiplier, _classify_income_mode
        for vi in income.sourceIncome.verificationIncome:
            rate = _parse_money(vi.rateOfPay)
            if not rate:
                continue
            key = (vi.sourceName or vi.incomeType or "unknown").lower()
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
        findings.append(
            f"TIC declares household income ${tic_total:,.2f} but no individual income "
            f"calculations produced results — verify all income sources extracted"
        )
        return findings

    diff = abs(tic_total - calc_total)
    diff_pct = diff / tic_total if tic_total > 0 else 0

    if diff_pct > 0.15:
        direction = "higher" if calc_total > tic_total else "lower"
        source_detail = ", ".join(f"{k}: ${v:,.0f}" for k, v in best_by_source.items())
        findings.append(
            f"Income total mismatch: TIC declares ${tic_total:,.2f} but extracted sources "
            f"sum to ${calc_total:,.2f} ({diff_pct:.0%} {direction}). "
            f"Sources: [{source_detail}]. "
            f"Possible missing/duplicate income source — review Section 9"
        )
    elif diff_pct > 0.05:
        findings.append(
            f"Minor income discrepancy: TIC ${tic_total:,.2f} vs calculated ${calc_total:,.2f} "
            f"({diff_pct:.0%} difference) — may be rounding"
        )

    return findings


def _best_method_for_key(
    key: str,
    calculations: list[IncomeCalculationResult],
    current_best: dict[str, float],
) -> str:
    """Find the method name of the current best calculation for a source key."""
    if key not in current_best:
        return ""
    target_val = current_best[key]
    for calc in calculations:
        if (calc.sourceName or calc.memberName or "unknown").lower() == key:
            try:
                if calc.annualIncome and float(calc.annualIncome) == target_val:
                    return calc.method or ""
            except ValueError:
                continue
    return ""


def validate_cert_summary_vs_income(
    income: IncomeExtraction | None,
    income_calculations: list[IncomeCalculationResult],
    document_groups: list[DocumentGroup],
) -> list[str]:
    """Cross-validate individual income records against cert summary tables.

    Certification forms (USDA RD 3560-8, LIHTC TIC page 3, HUD 50059 Section D)
    contain per-person income/employer summary tables that serve as ground truth.
    If VOI-extracted rate × hours × frequency differs >10% from the summary's
    annual salary, flag it and report the summary value.

    This works across ALL cert form types — not doc-specific regex.
    """
    import re

    findings: list[str] = []
    if not income:
        return findings

    # Step 1: Extract per-person annual salary from cert summary tables
    # Pattern: table rows with (Resident/Name, Employer, Annual salary, ...)
    # This pattern exists in USDA RD, LIHTC TIC page 3 worksheets, HUD 50059 Section D
    summary_entries: list[dict] = []

    for g in document_groups:
        if g.category == "ignore" and "(Previous)" in g.document_type:
            continue
        if not any(kw in g.document_type for kw in (
            "TIC", "HUD 50059", "Certification",
        )):
            continue

        text = g.combined_text
        # Look for HTML table rows: Resident | Employer | Annual salary
        # Match: <td>Name</td><td>Employer</td><td>amount</td>
        for m in re.finditer(
            r"<tr><td>([A-Z][a-z][\w\s-]+?)</td><td>([\w\s&]+?)</td><td>([\d,]+\.\d{2})</td>",
            text,
        ):
            name = m.group(1).strip()
            employer = m.group(2).strip()
            amount = m.group(3).replace(",", "")
            # Skip "Total" rows
            if name.lower() in ("total", "totals"):
                continue
            try:
                annual = float(amount)
                if annual > 0:
                    summary_entries.append({
                        "name": name.lower(),
                        "employer": employer.lower(),
                        "annual": annual,
                        "raw_name": name,
                        "raw_employer": employer,
                    })
            except ValueError:
                continue

    if not summary_entries:
        return findings

    # Step 2: Match summary entries to income records and compare
    vi_entries = income.sourceIncome.verificationIncome
    for se in summary_entries:
        # Find matching VI entry by member name + employer name
        best_vi = None
        best_calc_annual = None
        for vi in vi_entries:
            vi_name = (vi.memberName or "").lower()
            vi_source = (vi.sourceName or "").lower()
            # Match by name overlap
            if not vi_name or not (
                se["name"] in vi_name or vi_name in se["name"]
                or set(vi_name.split()) & set(se["name"].split())
            ):
                continue
            # Match by employer overlap
            if not vi_source or not (
                se["employer"] in vi_source or vi_source in se["employer"]
                or set(vi_source.split()) & set(se["employer"].split())
            ):
                continue
            best_vi = vi
            break

        if not best_vi:
            continue

        # Find the best calculation for this source
        source_key = (best_vi.sourceName or best_vi.memberName or "").lower()
        for calc in income_calculations:
            calc_key = (calc.sourceName or calc.memberName or "").lower()
            if calc_key == source_key or (
                set(calc_key.split()) & set(source_key.split())
            ):
                try:
                    best_calc_annual = float(calc.annualIncome)
                except (ValueError, TypeError):
                    continue
                break

        if best_calc_annual is None:
            continue

        # Compare
        summary_annual = se["annual"]
        diff = abs(best_calc_annual - summary_annual)
        if summary_annual > 0:
            diff_pct = diff / summary_annual
        else:
            continue

        if diff_pct > 0.10:
            direction = "higher" if best_calc_annual > summary_annual else "lower"
            findings.append(
                f"Income mismatch for {se['raw_name']} at {se['raw_employer']}: "
                f"cert summary shows ${summary_annual:,.2f}/year but VOI-based calculation "
                f"is ${best_calc_annual:,.2f} ({diff_pct:.0%} {direction}). "
                f"Cert summary is typically more reliable — verify VOI rate/hours."
            )

    return findings


def _parse_money(value: str | None) -> float | None:
    """Parse a monetary string."""
    if not value:
        return None
    try:
        return float(value.replace("$", "").replace(",", "").strip())
    except ValueError:
        return None
