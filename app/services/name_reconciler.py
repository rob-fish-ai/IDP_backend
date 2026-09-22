"""Name reconciler — resolves OCR name variants across documents.

After all extraction, collects every name mention from every source,
clusters variants by fuzzy matching, picks the highest-confidence
canonical form, and applies it to all output records.

Source trust hierarchy (highest first):
  1. TIC typed text (structured table cell)
  2. HUD 50059 typed text (structured table cell)
  3. Application / typed form
  4. Work Number / Equifax (third-party data)
  5. Handwritten forms / LLM extraction
"""

import logging

from app.services.findings import ASSIGN_INTERNAL, CATEGORY_MEMBER, RESOLVE_PRESENCE, make_finding

logger = logging.getLogger(__name__)

# Minimum similarity threshold for clustering (0-1)
_CLUSTER_THRESHOLD = 0.55

# Source trust ranking (lower = more trusted)
_SOURCE_TRUST = {
    "TIC": 1,
    "HUD 50059": 2,
    "Application": 3,
    "Work Number": 4,
    "Equifax": 4,
    "VOI": 5,
    "Paystub": 6,
    "LLM": 7,
    "Unknown": 8,
}


def reconcile_names(
    household,
    income=None,
    assets=None,
    document_groups=None,
) -> list[str]:
    """Reconcile name variants across all extraction records.

    Modifies records in-place with canonical names.
    Returns list of findings about name variants detected.
    """
    findings: list[str] = []
    if not household or not household.houseHold:
        return findings

    # Step 1: Collect all name mentions with source info
    mentions = _collect_name_mentions(household, income, assets, document_groups)
    if not mentions:
        return findings

    # Step 2: Cluster by fuzzy matching
    clusters = _cluster_names(mentions)

    # Step 3: Pick canonical form per cluster
    canonical_map: dict[str, str] = {}  # lowered variant → canonical
    for cluster in clusters:
        if len(cluster) < 2:
            continue

        # Sort by trust level (lowest = most trusted)
        cluster.sort(key=lambda m: _SOURCE_TRUST.get(m["source"], 99))
        canonical = cluster[0]["name"]

        variants = set(m["name"] for m in cluster if m["name"] != canonical)
        if variants:
            findings.append(make_finding(
                "NAME_VARIANT",
                f"Name variants detected for '{canonical}': "
                f"{', '.join(repr(v) for v in variants)} — using '{canonical}' from {cluster[0]['source']}",
                label="Name spelled differently across documents",
                category=CATEGORY_MEMBER,
                subject_type="household_member",
                subject_ref={"member_name": canonical},
                result="compliant",
                assignment=ASSIGN_INTERNAL,
                correction_required=None,
                resolution_type=RESOLVE_PRESENCE,
            ))

        for mention in cluster:
            canonical_map[mention["name"].lower()] = canonical

    if not canonical_map:
        return findings

    # Step 4: Apply canonical names to all records
    _apply_canonical_names(canonical_map, income, assets)

    return findings


def _collect_name_mentions(household, income, assets, document_groups) -> list[dict]:
    """Collect every name mention with its source type.

    Each household member gets a unique `household_idx` so the clusterer can
    treat them as distinct people. Two mentions from the same household list
    represent different people by definition — even if their names look
    similar — and must never be merged into the same cluster.
    """
    mentions: list[dict] = []

    # From household members — tag each with its household index so the
    # clusterer can enforce "different household records = different people"
    for idx, m in enumerate(household.houseHold):
        full = f"{m.FirstName or ''} {m.LastName or ''}".strip()
        if full:
            mentions.append({
                "name": full,
                "source": "HUD 50059",
                "field": "household",
                "household_idx": idx,
            })

    # From income records
    if income:
        for vi in income.sourceIncome.verificationIncome:
            if vi.memberName:
                source = "Work Number" if vi.type_of_VOI == "Work Number" else "VOI"
                mentions.append({"name": vi.memberName, "source": source, "field": "income"})
        for ps in income.sourceIncome.payStub:
            if ps.memberName:
                mentions.append({"name": ps.memberName, "source": "Paystub", "field": "paystub"})

    # From assets
    if assets:
        for a in assets.assetInformation:
            if a.assetOwner:
                mentions.append({"name": a.assetOwner, "source": "Unknown", "field": "asset"})

    # From document groups (person_name field from classification)
    if document_groups:
        for g in document_groups:
            if g.person_name:
                dt = g.document_type
                source = "TIC" if "TIC" in dt else "HUD 50059" if "HUD" in dt else "Application" if "Application" in dt else "Unknown"
                mentions.append({"name": g.person_name, "source": source, "field": "classification"})

    return mentions


def _cluster_names(mentions: list[dict]) -> list[list[dict]]:
    """Cluster name mentions by fuzzy similarity.

    Two mentions from distinct household records (different household_idx)
    are treated as distinct people and never clustered together — even if
    their names are similar. This prevents e.g. "Abby Ruiz Esquiu" and
    "Jose Ruiz Esquinca" from being merged when both come from the same
    TIC household list.
    """
    clusters: list[list[dict]] = []
    used: set[int] = set()

    def _same_person_ok(m1: dict, m2: dict) -> bool:
        # Two household members with different indexes are distinct people
        # by definition — never merge them regardless of name similarity.
        idx1 = m1.get("household_idx")
        idx2 = m2.get("household_idx")
        if idx1 is not None and idx2 is not None and idx1 != idx2:
            return False
        return True

    for i, m1 in enumerate(mentions):
        if i in used:
            continue
        cluster = [m1]
        used.add(i)

        # Track the set of household_idx values already in this cluster so
        # we can block any further household record from joining it.
        cluster_household_idxs: set[int] = set()
        if m1.get("household_idx") is not None:
            cluster_household_idxs.add(m1["household_idx"])

        for j, m2 in enumerate(mentions):
            if j in used:
                continue
            if not _same_person_ok(m1, m2):
                continue
            # Also block joining if m2 is a household record whose index
            # is already represented in this cluster via another household
            # mention (rare, defensive).
            if (m2.get("household_idx") is not None
                    and m2["household_idx"] in cluster_household_idxs
                    and m1.get("household_idx") is None):
                continue
            if _name_similarity(m1["name"], m2["name"]) >= _CLUSTER_THRESHOLD:
                cluster.append(m2)
                used.add(j)
                if m2.get("household_idx") is not None:
                    cluster_household_idxs.add(m2["household_idx"])

        clusters.append(cluster)

    return clusters


def _name_similarity(a: str, b: str) -> float:
    """Compute name similarity (0-1) using multiple strategies.

    Combines: token overlap, Levenshtein-like char matching, and first/last name matching.
    """
    a_lower = a.lower().strip()
    b_lower = b.lower().strip()

    if a_lower == b_lower:
        return 1.0

    # Strategy 1: Token overlap
    a_tokens = set(a_lower.split())
    b_tokens = set(b_lower.split())
    if a_tokens and b_tokens:
        overlap = len(a_tokens & b_tokens)
        token_sim = overlap / max(len(a_tokens), len(b_tokens))
    else:
        token_sim = 0.0

    # Strategy 2: First name / last name matching
    a_parts = a_lower.split()
    b_parts = b_lower.split()
    first_match = False
    last_match = False
    if a_parts and b_parts:
        # First names: allow prefix match (Brit matches Brittany, Britney, etc.)
        first_a, first_b = a_parts[0], b_parts[0]
        if first_a[:3] == first_b[:3] and len(first_a) >= 3 and len(first_b) >= 3:
            first_match = True
        # Last names: exact or close match
        last_a = a_parts[-1] if len(a_parts) > 1 else ""
        last_b = b_parts[-1] if len(b_parts) > 1 else ""
        if last_a and last_b:
            if last_a == last_b:
                last_match = True
            elif _char_similarity(last_a, last_b) > 0.75:
                last_match = True

    name_part_sim = 0.0
    if first_match and last_match:
        name_part_sim = 0.85
    elif last_match and not first_match:
        # Same last name but different first name — likely different people (siblings)
        name_part_sim = 0.30
    elif first_match and not last_match:
        name_part_sim = 0.35

    # Strategy 3: Character-level similarity
    char_sim = _char_similarity(a_lower, b_lower)

    # Guard: two full names whose given names are different names belong to
    # different people, whatever the surnames and the letters they share.
    # "Juan Garcia Ortega" and "Maria Garcia" scored 0.56 on bigrams alone
    # and a husband's records were renamed after his wife. A given name one
    # letter off ("Aima" / "Alma") or a prefix ("Mary" / "Maria") is the
    # same name; anything further apart is not.
    if len(a_parts) >= 2 and len(b_parts) >= 2 and not first_match:
        fa, fb = a_parts[0].strip("."), b_parts[0].strip(".")
        close = (len(fa) >= 4 and len(fb) >= 4 and _edit_distance(fa, fb) <= 1) or (
            len(fa) == 1 and fb.startswith(fa)) or (len(fb) == 1 and fa.startswith(fb))
        if not close:
            return min(max(token_sim, name_part_sim, char_sim), 0.40)

    return max(token_sim, name_part_sim, char_sim)


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _char_similarity(a: str, b: str) -> float:
    """Simple character-level similarity (1 - normalized_edit_distance).

    Uses a simplified approach: ratio of common bigrams.
    """
    if not a or not b:
        return 0.0

    # Bigram similarity (cheaper than full Levenshtein)
    a_bigrams = set(a[i:i+2] for i in range(len(a) - 1))
    b_bigrams = set(b[i:i+2] for i in range(len(b) - 1))
    if not a_bigrams or not b_bigrams:
        return 0.0

    overlap = len(a_bigrams & b_bigrams)
    return (2 * overlap) / (len(a_bigrams) + len(b_bigrams))


def _apply_canonical_names(
    canonical_map: dict[str, str],
    income,
    assets,
) -> None:
    """Apply canonical names to downstream records (income, assets).

    Household members are NOT renamed — the LLM's household list is
    authoritative for personhood. Renaming household members based on
    cross-document clustering can conflate distinct household members
    whose names share tokens (e.g. "Abby Ruiz Esquiu" and "Jose Arbey
    Ruiz Esquinca") and cause them to collapse in dedup.

    Instead, income/asset records are reconciled to whichever household
    member name they most closely match. The clustering result is only
    used to propagate the best name form to VOI / paystub / asset
    records that refer to the same people.
    """
    def _resolve(name: str | None) -> str | None:
        if not name:
            return name
        canonical = canonical_map.get(name.lower())
        return canonical if canonical else name

    # Income — sync to canonical household names
    if income:
        for vi in income.sourceIncome.verificationIncome:
            vi.memberName = _resolve(vi.memberName)
        for ps in income.sourceIncome.payStub:
            ps.memberName = _resolve(ps.memberName)

    # Assets — sync to canonical household names
    if assets:
        for a in assets.assetInformation:
            a.assetOwner = _resolve(a.assetOwner)
