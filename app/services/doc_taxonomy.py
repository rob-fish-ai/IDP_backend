"""The document taxonomy, owned by code.

The classifier assigns labels from a list; every consumer then asks
questions of those labels — "is this the certification form?", "does this
document feed the income extractor?", "is this a HUD compliance form?" —
and each had grown its own literal list to answer it. Seven modules carried
copies, and copies drift: the signature validator looked up "Tenant
Release and Consent" while the classifier emitted "…Form", so an unsigned
release raised nothing; the required-forms check demanded "Acknowledgement
of Receipt of HUD Forms" while the packet carried "Acknowledgement of
Receipt", so a present form was reported missing on every HUD packet.

This module is the one table. It holds, per label: the category the
classifier's output is derived from (the model no longer chooses it), the
evidence family (what kind of document it is), the extractors it routes to,
aliases the model may return for it, and the description line the prompt
shows. The prompt's type list is generated from it; every routing set is
derived from it; a literal set anywhere else is checked against it at
import with `assert_known`.

Recognition is by exact label. Substring matching failed in both
directions on real packets: "Tenant Income Certification Questionnaire"
contains the certification's whole name and is not the certification;
"Annual Self Certification" is one and contains none of its markers.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

INCLUDE = "include"
COMPLIANCE = "compliance"
IGNORE = "ignore"

# Evidence families. Consumers ask for a family rather than a label.
FAMILY_CERT = "cert"                # the certification being audited
FAMILY_LEASE = "lease"              # carries rent and effective date, not a cert
FAMILY_HOUSEHOLD = "household"      # identity and composition evidence
FAMILY_INCOME = "income"            # third-party income evidence
FAMILY_ASSET = "asset"              # third-party asset evidence
FAMILY_DECLARATION = "declaration"  # the household's own statements
FAMILY_COMPLIANCE = "compliance"    # required forms, not data-extracted
FAMILY_IGNORE = "ignore"

# Routes: which extractors a document feeds.
ROUTE_DEMO, ROUTE_CERT, ROUTE_INCOME, ROUTE_ASSET = "demo", "cert", "income", "asset"


def _t(category, family, routes=(), aliases=(), hint=None, confirms=None):
    """`confirms` names the third-party report a countersigned confirmation
    sheet restates: a manager's sheet carrying that report's figures which
    the household signs as agreeing. Such a sheet is read both as the
    household's declaration and, when agreed and signed, as verification;
    it also stands in for the report when a rule asks whether one exists."""
    return {"category": category, "family": family, "routes": tuple(routes),
            "aliases": tuple(aliases), "hint": hint, "confirms": confirms}


# Order matters: it is the order the prompt lists the types in.
TAXONOMY: dict[str, dict] = {
    # --- certification forms and the lease ---
    "HUD 50059": _t(INCLUDE, FAMILY_CERT, (ROUTE_DEMO, ROUTE_CERT, ROUTE_INCOME, ROUTE_ASSET),
                    hint="HUD Owner's Certification of Compliance"),
    "Tenant Income Certification (TIC)": _t(INCLUDE, FAMILY_CERT, (ROUTE_DEMO, ROUTE_CERT, ROUTE_INCOME, ROUTE_ASSET),
                                            aliases=("Tenant Income Certification", "TIC"),
                                            hint="LIHTC TIC, state HFA TIC forms, AR-SC self-certifications"),
    "HUD 3560 Form": _t(INCLUDE, FAMILY_CERT, (ROUTE_DEMO, ROUTE_CERT, ROUTE_INCOME, ROUTE_ASSET),
                        aliases=("RD 3560-8", "Form RD 3560-8"),
                        hint="USDA RD 3560-8 Tenant Certification"),
    "HUD Model Lease": _t(INCLUDE, FAMILY_LEASE, (ROUTE_DEMO, ROUTE_CERT),
                          hint="HUD Section 8/202/236 lease — contains rent/effective date"),
    "Lease Agreement": _t(INCLUDE, FAMILY_LEASE, (ROUTE_DEMO, ROUTE_CERT),
                          aliases=("Residential Lease", "Residential Lease Agreement", "Rental Agreement",
                                   "Apartment Lease", "Lease Agreement (Non-HUD)"),
                          hint="a property's own residential lease (\"THIS LEASE is made…\", New Lease / Renewal boxes) — carries rent, term and the household on the lease; not the HUD model lease"),
    # --- household and identity ---
    "Application / Housing Questionnaire": _t(INCLUDE, FAMILY_DECLARATION, (ROUTE_DEMO, ROUTE_INCOME, ROUTE_ASSET),
                                              aliases=("Housing Questionnaire", "Recertification Questionnaire",
                                                       "Application for Housing", "Tenant Income Certification Questionnaire")),
    "Identity Document": _t(INCLUDE, FAMILY_HOUSEHOLD, (ROUTE_DEMO,),
                            hint="driver license / state ID / SSN card pages"),
    "Student Status Certification": _t(INCLUDE, FAMILY_HOUSEHOLD, (ROUTE_DEMO,),
                                       aliases=("Student Status Affidavit / Certification", "Student Status Affidavit",
                                                "Student Certification", "Student Status Verification",
                                                "Student Status Self-Certification")),
    "Owner Summary Sheet": _t(INCLUDE, FAMILY_HOUSEHOLD, (ROUTE_DEMO,),
                              hint="management's roster/summary for the unit"),
    "Family Summary Sheet": _t(INCLUDE, FAMILY_HOUSEHOLD, (ROUTE_DEMO,)),
    # --- income evidence ---
    "Verification of Income (VOI)": _t(INCLUDE, FAMILY_INCOME, (ROUTE_INCOME,),
                                       aliases=("Verification of Employment (VOE)", "Employment Verification")),
    "Verification of Assets (VOA)": _t(INCLUDE, FAMILY_ASSET, (ROUTE_ASSET,),
                                       aliases=("Verification of Deposit", "Verification of Deposit (VOD)", "Bank Verification",
                                                "Verification of Deposit Request", "VOD Request"),
                                       hint="a deposit / asset verification form sent to a bank, whether or not the bank has completed it — a blank response is still this type, never an authorization"),
    "Work Number / Equifax Report": _t(INCLUDE, FAMILY_INCOME, (ROUTE_INCOME,),
                                       aliases=("Work Number Report", "Equifax Report")),
    "Paystub": _t(INCLUDE, FAMILY_INCOME, (ROUTE_INCOME,), aliases=("Pay Stub", "Paystubs")),
    "SSA Benefit Letter": _t(INCLUDE, FAMILY_INCOME, (ROUTE_INCOME,),
                             aliases=("SSI Benefit Letter", "SSDI Benefit Letter", "SSA Benefit Verification Letter",
                                      "Social Security Benefit Letter", "SSA COLA Notice"),
                             hint="any Social Security Administration letter: retirement, SSI, SSDI, survivor, COLA"),
    "VA Benefit Letter": _t(INCLUDE, FAMILY_INCOME, (ROUTE_INCOME,),
                            aliases=("VA Disability Compensation Letter", "Veterans Affairs Benefit Letter",
                                     "VA Award Letter", "VA Benefits Verification Letter"),
                            hint="any Department of Veterans Affairs letter stating a benefit: disability compensation, pension, DIC"),
    "Verification of Disability Benefits": _t(INCLUDE, FAMILY_INCOME, (ROUTE_INCOME,),
                                              hint="private LTD/STD insurer benefit letters"),
    "Pension Statement": _t(INCLUDE, FAMILY_INCOME, (ROUTE_INCOME,)),
    "TANF Verification": _t(INCLUDE, FAMILY_INCOME, (ROUTE_INCOME,)),
    "TANF / Public Assistance Verification": _t(INCLUDE, FAMILY_INCOME, (ROUTE_INCOME,),
                                                aliases=("Verification of Benefits",),
                                                hint="county benefit printouts: CalWORKs, GA/GR, cash aid; use THIS name for forms headed \"Verification of Benefits\". A certificate naming a household member as the payee or provider for someone outside the household is that member's earned income, not a benefit to the household"),
    "Child Support Statement": _t(INCLUDE, FAMILY_INCOME, (ROUTE_INCOME,),
                                  aliases=("Child Support Order", "Child Support Verification", "Record of Payments")),
    "Child Support / Alimony Affidavit": _t(INCLUDE, FAMILY_DECLARATION, (ROUTE_INCOME,),
                                            hint="resident affidavit, not a payer statement"),
    "Gift Income Verification": _t(INCLUDE, FAMILY_INCOME, (ROUTE_INCOME,),
                                   hint="third party attesting to ongoing cash contributions"),
    "Zero Income Certification": _t(INCLUDE, FAMILY_DECLARATION, (ROUTE_INCOME,)),
    "Self-Employment Affidavit": _t(INCLUDE, FAMILY_INCOME, (ROUTE_INCOME,)),
    "Unemployment Affidavit": _t(INCLUDE, FAMILY_DECLARATION, (ROUTE_INCOME,),
                                 aliases=("Non-Employment Affidavit",)),
    "HomeBASE Verification": _t(INCLUDE, FAMILY_INCOME, (ROUTE_INCOME,)),
    # --- asset evidence ---
    "Bank Statement": _t(INCLUDE, FAMILY_ASSET, (ROUTE_ASSET,), aliases=("Account Statement", "ATM Balance Receipt")),
    "Investment Account Statement": _t(INCLUDE, FAMILY_ASSET, (ROUTE_ASSET,),
                                       hint="brokerage, mutual fund, retirement account"),
    "Real Estate Verification": _t(INCLUDE, FAMILY_ASSET, (ROUTE_ASSET,),
                                   hint="county tax roll or assessor inquiry, deed, appraisal, mortgage statement"),
    "Life Insurance Policy": _t(INCLUDE, FAMILY_ASSET, (ROUTE_ASSET,)),
    "Asset Self-Certification": _t(INCLUDE, FAMILY_DECLARATION, (ROUTE_ASSET,)),
    "No Asset Certification": _t(INCLUDE, FAMILY_DECLARATION, (ROUTE_ASSET,),
                                 hint="household attests it holds NO assets — distinct from an asset self-certification, which lists some"),
    "Disposal of Assets Certification": _t(INCLUDE, FAMILY_DECLARATION, (ROUTE_ASSET,),
                                           hint="assets given away below fair market value"),
    "Direct Express Card Verification": _t(INCLUDE, FAMILY_ASSET, (ROUTE_ASSET,)),
    "Debit Card Asset Self-Certification": _t(INCLUDE, FAMILY_DECLARATION, (ROUTE_ASSET,)),
    "Notice of Rent Change": _t(COMPLIANCE, FAMILY_COMPLIANCE, (ROUTE_CERT,),
                                aliases=("Lease Amendment", "Rent Change Notice")),
    # --- compliance: required forms, not data-extracted ---
    # The printed titles are aliases so the title matcher, which takes the
    # longest alias found in a heading, resolves the consent forms to
    # themselves and not to a generic release whose alias is a substring.
    "HUD 9887": _t(COMPLIANCE, FAMILY_COMPLIANCE,
                   aliases=("HUD-9887", "Notice and Consent for the Release of Information")),
    "HUD 9887-A": _t(COMPLIANCE, FAMILY_COMPLIANCE,
                     aliases=("HUD-9887-A", "Applicant's/Tenant's Consent to the Release of Information",
                              "Applicant's / Tenant's Consent to the Release of Information")),
    "HUD 9887 Consent Package Cover": _t(COMPLIANCE, FAMILY_COMPLIANCE,
                                         hint="the \"Document Package for Applicant's/Tenant's Consent to the Release Of Information\" sheet that introduces the package — not a consent form itself"),
    "HUD 9887/A Fact Sheet": _t(COMPLIANCE, FAMILY_COMPLIANCE,
                                hint="explanatory \"Fact Sheet\" page filed with the package — informational, never signed"),
    "HUD 92006": _t(COMPLIANCE, FAMILY_COMPLIANCE, aliases=("HUD-92006",)),
    "HUD Race and Ethnic Data Form": _t(COMPLIANCE, FAMILY_COMPLIANCE,
                                        aliases=("Race and Ethnic Data Reporting Form", "Race and Ethnic Data Form")),
    "Citizenship Declaration": _t(COMPLIANCE, FAMILY_COMPLIANCE, aliases=("Citizenship Declaration (Section 214)",)),
    "Acknowledgement of Receipt": _t(COMPLIANCE, FAMILY_COMPLIANCE,
                                     aliases=("Acknowledgement of Receipt of HUD Forms", "HUD Acknowledgement of Receipt of Documents")),
    "Tenant Release and Consent Form": _t(COMPLIANCE, FAMILY_COMPLIANCE,
                                          aliases=("Tenant Release and Consent", "Authorization to Release Information")),
    "Third-Party Authorization / Release": _t(COMPLIANCE, FAMILY_COMPLIANCE,
                                              aliases=("General Authorization Letter", "Authorization Letter", "Release of Information"),
                                              hint="a signed letter authorizing employers, banks or agencies to release information — not the HUD consent forms, and not a verification request form that carries a response section"),
    "VAWA Lease Addendum": _t(COMPLIANCE, FAMILY_COMPLIANCE,
                              aliases=("Violence Against Women Act Lease Addendum", "VAWA Addendum",
                                       "Lease Addendum - Violence Against Women", "HUD-91067")),
    "LIHTC Lease Addendum": _t(COMPLIANCE, FAMILY_COMPLIANCE,
                               aliases=("Low Income Housing Tax Credit Lease Addendum", "Tax Credit Lease Addendum",
                                        "LIHTC Addendum", "Low-Income Housing Tax Credit Lease Addendum"),
                               hint="the tax-credit program's lease addendum (Section 42 obligations) — not the VAWA addendum"),
    "Lead-Based Paint Certification": _t(COMPLIANCE, FAMILY_COMPLIANCE),
    "Initial Notice of Recertification": _t(COMPLIANCE, FAMILY_COMPLIANCE,
                                            aliases=("Recertification Notice", "Annual Certification Reminder Notice")),
    "EIV Summary Report": _t(COMPLIANCE, FAMILY_COMPLIANCE, aliases=("EIV Household Summary",),
                             hint="the EIV Summary Report page: head-of-household identifiers, member list with identity verification status — no amounts"),
    "EIV Income Report": _t(INCLUDE, FAMILY_INCOME, (ROUTE_INCOME,),
                            aliases=("EIV Report", "EIV Wage and Benefit Report", "Wage and Benefit Report",
                                     "EIV Income Discrepancy Report", "Enterprise Income Verification Report"),
                            hint="HUD's EIV printout per household member: Employment Information (quarterly wages by employer), Unemployment Benefits, Social Security Benefits Verification Data (gross and net monthly benefit), Supplemental Security Income, Dual Entitlement, Medicare — the third-party verification of SSA benefits on a HUD file"),
    "EIV Income Report Confirmation": _t(INCLUDE, FAMILY_DECLARATION, (ROUTE_INCOME,),
                                         aliases=("EIV Confirmation", "Income Report Confirmation", "EIV Income Report Confirmation Sheet"),
                                         confirms="EIV Income Report",
                                         hint="the manager's coversheet filed with the EIV report: tenant agrees / disagrees with the income report, handwritten source, amount and frequency, resident and management signatures — a declaration, not the report"),
    "Expense / Allowance Declaration": _t(COMPLIANCE, FAMILY_COMPLIANCE,
                                          aliases=("Medical Expense Worksheet", "Medical Expense Declaration",
                                                   "Child Care Expense Declaration"),
                                          hint="the household's medical, child-care or disability expense listing — filed for deductions, not income"),
    "Court Order / Legal Document": _t(COMPLIANCE, FAMILY_COMPLIANCE,
                                       aliases=("Guardianship Order", "Custody Order", "Court Order"),
                                       hint="guardianship, custody, divorce or other court papers"),
    "Compliance — Other Signed HUD Form": _t(COMPLIANCE, FAMILY_COMPLIANCE,
                                             aliases=("Wage Match Agreement", "Other HUD Form"),
                                             hint="a signed HUD form no other type names (e.g. Wage Match Agreement) — goes to the inventory and signature checks"),
    # --- ignore: not processed ---
    "Income Calculation Worksheet": _t(IGNORE, FAMILY_IGNORE, hint="INTERNAL staff calc sheet ONLY"),
    "Certification Review": _t(IGNORE, FAMILY_IGNORE, hint="reviewer/auditor findings & correction reports"),
    "Receipt / Purchase Documentation": _t(IGNORE, FAMILY_IGNORE, hint="retail receipts, order summaries, billing receipts"),
    "File Order Form": _t(IGNORE, FAMILY_IGNORE, aliases=("File Checklist", "Cover Sheet")),
    "Blank Page": _t(IGNORE, FAMILY_IGNORE),
    "Blank Form": _t(IGNORE, FAMILY_IGNORE),
    "Correspondence": _t(IGNORE, FAMILY_IGNORE, aliases=("Email", "Letter")),
    "Fax Cover Sheet": _t(IGNORE, FAMILY_IGNORE),
    "Background Screening Report": _t(IGNORE, FAMILY_IGNORE,
                                      aliases=("Credit Screening Report", "Screening Affidavit", "Criminal Background Check",
                                               "Sex Offender Registry Search"),
                                      hint="credit, criminal or registry screening paperwork and its affidavits"),
    "Maintenance / Inspection Form": _t(IGNORE, FAMILY_IGNORE),
    "Unknown": _t(IGNORE, FAMILY_IGNORE),
    # emitted by the pipeline itself
    "OCR Failed": _t(IGNORE, FAMILY_IGNORE),
}

_PREVIOUS_MARKER = "(previous)"
_SUPERSEDED_MARKER = "(superseded)"
_SUFFIXES = (" (Previous)", " (Superseded)")

_ALIAS_INDEX: dict[str, str] = {}
for _label, _spec in TAXONOMY.items():
    _ALIAS_INDEX[_label.lower()] = _label
    for _a in _spec["aliases"]:
        _ALIAS_INDEX[_a.lower()] = _label


def _split_suffix(document_type: str) -> tuple[str, str]:
    for suf in _SUFFIXES:
        if document_type.endswith(suf):
            return document_type[: -len(suf)], suf
    return document_type, ""


def canonical_label(document_type: str | None) -> tuple[str, str]:
    """(canonical label, fit) for a label the classifier returned.

    fit is "exact" when the label is canonical, "alias" when it was one of
    the recorded aliases, "none" when it is not in the taxonomy at all — in
    which case the canonical label is "Unknown" and the caller keeps the
    observed label in the page notes.
    """
    if not document_type:
        return "Unknown", "none"
    base, suffix = _split_suffix(document_type.strip())
    key = base.strip().lower()
    if key in TAXONOMY or key in {k.lower() for k in TAXONOMY}:
        canon = _ALIAS_INDEX.get(key, base)
        return canon + suffix, "exact"
    if key in _ALIAS_INDEX:
        return _ALIAS_INDEX[key] + suffix, "alias"
    return "Unknown", "none"


def is_countersigned_confirmation(document_type: str | None) -> bool:
    """Whether the type is a manager's sheet restating a third-party report
    that the household countersigns (see `_t`)."""
    spec = spec_of(document_type)
    return bool(spec and spec.get("confirms"))


def confirmed_report_of(document_type: str | None) -> str | None:
    """The report type a countersigned confirmation sheet stands in for."""
    spec = spec_of(document_type)
    return spec.get("confirms") if spec else None


def spec_of(document_type: str | None) -> dict | None:
    if not document_type:
        return None
    base, _ = _split_suffix(document_type)
    return TAXONOMY.get(base)


def category_of(document_type: str | None) -> str:
    """The category a label carries — decided here, never by the model.
    A previous or superseded certification is ignored whatever its base."""
    if not document_type:
        return IGNORE
    base, suffix = _split_suffix(document_type)
    if suffix:
        return IGNORE
    spec = TAXONOMY.get(base)
    return spec["category"] if spec else IGNORE


def family_of(document_type: str | None) -> str:
    spec = spec_of(document_type)
    return spec["family"] if spec else FAMILY_IGNORE


def labels_for_route(route: str) -> frozenset[str]:
    return frozenset(label for label, spec in TAXONOMY.items() if route in spec["routes"])


def labels_in_family(*families: str) -> frozenset[str]:
    return frozenset(label for label, spec in TAXONOMY.items() if spec["family"] in families)


def known_labels() -> frozenset[str]:
    return frozenset(TAXONOMY)


def assert_known(labels, where: str) -> None:
    """Fail loudly at import when a module's literal label set names a type
    the classifier cannot emit. Silence here was the failure mode: a check
    keyed on a label that does not exist never runs, and nothing says so."""
    unknown = sorted(l for l in labels if _split_suffix(l)[0] not in TAXONOMY)
    if unknown:
        raise ValueError(f"{where}: labels not in the document taxonomy: {unknown}")


def prompt_type_list() -> str:
    """The CANONICAL DOCUMENT TYPES block of the classifier prompt, generated
    so the prompt and the code cannot drift."""
    sections = (
        (INCLUDE, "INCLUDE — data-extracted forms:"),
        (COMPLIANCE, "COMPLIANCE — required forms, not data-extracted:"),
        (IGNORE, "IGNORE — not processed:"),
    )
    lines = ["CANONICAL DOCUMENT TYPES (use these exact names):"]
    for cat, title in sections:
        lines.append("")
        lines.append(f"  {title}")
        for label, spec in TAXONOMY.items():
            if spec["category"] != cat or label == "OCR Failed":
                continue
            line = f"    - {label}"
            extras = []
            if spec["hint"]:
                extras.append(spec["hint"])
            if spec["aliases"]:
                extras.append("also known as: " + ", ".join(spec["aliases"][:4]))
            if extras:
                line = f"{line:<44}({'; '.join(extras)})"
            lines.append(line)
    return "\n".join(lines)


# --- certification predicates (unchanged contract) ---------------------------

_RESEMBLES_CERTIFICATION = ("tenant income certification", "50059", "3560")
_reported: set[str] = set()


def _base_label(document_type: str) -> str:
    return _split_suffix(document_type)[0].lower().strip()


def is_certification_form(document_type: str | None) -> bool:
    """The certification being audited (or a previous copy of one).

    An unrecognized label that reads like a certification is reported rather
    than guessed at: the classifier returning something outside its own
    taxonomy means the document reaches no extractor and contributes
    nothing, and it does so silently.
    """
    if not document_type:
        return False
    base, _ = _split_suffix(document_type)
    spec = TAXONOMY.get(base)
    if spec and spec["family"] == FAMILY_CERT:
        return True
    low = base.lower()
    if any(word in low for word in _RESEMBLES_CERTIFICATION):
        if low not in _reported:
            _reported.add(low)
            logger.warning(
                "Document type %r is not a known type but reads like a "
                "certification — it will be treated as an ordinary document. "
                "If it is a certification form, the classifier should be "
                "returning one of: %s",
                document_type, sorted(labels_in_family(FAMILY_CERT)),
            )
    return False


def is_previous_certification(document_type: str | None) -> bool:
    """A previous certification restates last year's figures. Nothing in it
    should match this year's records, so any check comparing an extraction
    against the certification must exclude it."""
    low = (document_type or "").lower()
    return _PREVIOUS_MARKER in low or _SUPERSEDED_MARKER in low


def is_current_certification_form(document_type: str | None) -> bool:
    """The certification being audited: a certification form, not a prior copy."""
    return is_certification_form(document_type) and not is_previous_certification(document_type)
