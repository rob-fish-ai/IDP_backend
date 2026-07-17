"""Salesforce REST/SOAP client for the IDP audit pipeline.

Reads case context, audit data, and PDF attachments. Writes audit findings
back to the Case via update_case_findings (Long Text Area field).
"""
import calendar
import io
import logging
import re
import threading
import time
from difflib import SequenceMatcher
from typing import Any

import fitz  # PyMuPDF — used to count pages before pushing into the pipeline
from simple_salesforce import Salesforce

from app.core.config import Settings

logger = logging.getLogger(__name__)


def _escape_soql(value: str) -> str:
    """Escape a value for inclusion in a SOQL string literal."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


# ---------------------------------------------------------------------------
# Multi-part packet grouping
#
# Property managers sometimes upload one certification packet as several
# PDFs ("... part 1"/"part 2", "... 1 of 2", "....1"/".2"). Each part on
# its own can fall below the min-pages gate even though the whole packet
# passes, so parts must be recognized and merged before gating.
#
# Detection is convention-agnostic: two titles are parts of the same
# packet when they differ by exactly ONE numeric fragment. That groups
# every numbering style (including ones we haven't seen) while refusing
# to group version-style siblings ("... Approved" vs "... File for
# signatures"), whose differences are words, not digits. An unnumbered
# base file next to a numbered one ("X" vs "X.1" — a re-upload, not a
# part) is also refused, because that diff is an insertion, not a
# numeric replacement.
# ---------------------------------------------------------------------------

def _normalize_title(title: str) -> str:
    t = (title or "").strip().lower()
    if t.endswith(".pdf"):
        t = t[:-4]
    return re.sub(r"\s+", " ", t)


def _numeric_part_diff(a: str, b: str) -> tuple[int, int] | None:
    """Return (num_a, num_b) when two titles differ only by one numeric
    fragment; None otherwise."""
    diffs = [
        op for op in SequenceMatcher(None, a, b).get_opcodes()
        if op[0] != "equal"
    ]
    if len(diffs) != 1:
        return None
    tag, i1, i2, j1, j2 = diffs[0]
    if tag != "replace":
        return None
    frag_a, frag_b = a[i1:i2], b[j1:j2]
    if not (frag_a.isdigit() and frag_b.isdigit()):
        return None
    return int(frag_a), int(frag_b)


def _group_packet_parts(candidates: list[dict]) -> list[list[dict]]:
    """Cluster a case's PDF candidates into packet-part groups.

    candidates: [{"cd_id", "title", ...}], any order. Returns groups in
    first-seen order; non-part files come back as 1-element groups. Part
    group members gain a "part_no" key.
    """
    groups: list[list[dict]] = []
    for cand in candidates:
        norm = _normalize_title(cand["title"])
        attached = False
        for group in groups:
            rep = group[0]
            diff = _numeric_part_diff(_normalize_title(rep["title"]), norm)
            if diff is not None:
                rep.setdefault("part_no", diff[0])
                cand["part_no"] = diff[1]
                group.append(cand)
                attached = True
                break
        if not attached:
            groups.append([cand])
    return groups


# Module-level singleton — re-using the auth handshake across jobs avoids
# unnecessary Salesforce login calls (each job previously re-auth'd).
_CLIENT: "SalesforceClient | None" = None
_CLIENT_LOCK = threading.Lock()


def get_salesforce_client(settings: Settings) -> "SalesforceClient":
    """Return the process-wide SalesforceClient, creating on first use."""
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                _CLIENT = SalesforceClient(settings)
    return _CLIENT


class SalesforceClient:
    """Wraps simple_salesforce with project-specific queries.

    Auth via username/password/token from Settings. No module-level
    side effects — instance is created when needed.
    """

    def __init__(self, settings: Settings):
        if not settings.sf_username or not settings.sf_password:
            raise ValueError(
                "Salesforce credentials missing — set IDP_SF_USERNAME and "
                "IDP_SF_PASSWORD in environment."
            )
        self._settings = settings
        self._timeout = settings.sf_request_timeout
        self.sf = Salesforce(
            username=settings.sf_username,
            password=settings.sf_password,
            security_token=settings.sf_token or "",
            domain=settings.sf_domain,
        )
        # Default timeout for ALL requests via this session — applies to
        # SOQL queries and the binary download for PDFs. Without this, a
        # hung Salesforce response could block a worker indefinitely.
        self.sf.session.request = self._wrap_with_timeout(self.sf.session.request)
        logger.info(
            "Salesforce client initialized for %s (timeout=%ds)",
            settings.sf_username, self._timeout,
        )

    def _wrap_with_timeout(self, request_fn):
        """Wrap session.request to inject a default timeout when none given."""
        timeout = self._timeout

        def wrapped(method, url, **kwargs):
            kwargs.setdefault("timeout", timeout)
            return request_fn(method, url, **kwargs)

        return wrapped

    # ------------------------------------------------------------------
    # Read methods
    # ------------------------------------------------------------------

    def get_case_context(self, case_id: str) -> dict[str, Any]:
        """Lightweight query for the inputs the IDP pipeline needs.

        Returns CertType__c and Funding_Program2__c — both are populated
        on the Case at creation, before any MuleSoft processing.
        """
        case_id_s = _escape_soql(case_id)
        result = self.sf.query(f"""
            SELECT Id, CaseNumber,
                   CertType__c,
                   Funding_Program2__c,
                   Status,
                   IDP_File_Process_Status__c
            FROM Case
            WHERE Id = '{case_id_s}'
        """)
        records = result.get("records", [])
        if not records:
            raise ValueError(f"Case {case_id} not found in Salesforce")
        return records[0]

    def get_case_audit_data(self, case_id: str) -> dict[str, Any]:
        """Full case data for IDP-vs-MuleSoft comparison.

        Pulls Cert Review + Members + Income + Assets so the comparator
        can diff against the IDP extraction.
        """
        case_id_s = _escape_soql(case_id)

        review = self.sf.query(f"""
            SELECT Id, Status__c, Certification_Type__c,
                   Effective_Date__c, Funding_Program__c,
                   Unit_Number__c, Unit_Size__c,
                   Gross_Rent__c, Rent_Limit__c,
                   Income_Limit__c, Set_Aside__c,
                   Total_Income__c, Total_Household_Assets__c,
                   Total_Gross_Household_Income__c,
                   Household_Name__c, Number_Members__c,
                   Passbook_Rate__c,
                   TIC_Tenant_Rent__c, TIC_Utility_Allowance__c,
                   TIC_Gross_Rent__c, TIC_Unit_Set_Aside__c
            FROM Certification_Review__c
            WHERE Case__c = '{case_id_s}'
        """).get("records", [])

        if not review:
            return {
                "review": None,
                "members": [],
                "income": [],
                "assets": [],
            }

        rid = review[0]["Id"]
        rid_s = _escape_soql(rid)

        members = self.sf.query(f"""
            SELECT Id, First_Name__c, Last_Name__c,
                   DOB__c, SSN__c,
                   Disabled__c, Student__c, Head__c,
                   House_Member_Id__c, Full_Name__c,
                   Minor_Member__c,
                   Tenant_Email__c, Tenant_Phone__c
            FROM Household_Member_Cert__c
            WHERE Certification_Review__c = '{rid_s}'
        """).get("records", [])

        income = self.sf.query(f"""
            SELECT Id, Source_Name__c, Income_Type__c,
                   Self_Declared_Amount__c,
                   Self_Declared_Frequency__c,
                   Self_Declared_Annual_Amount__c,
                   Self_Declared_Source__c,
                   Frequency_of_Pay__c,
                   House_Member_Name__c, Household_Member__c,
                   Gross_Member_Income__c,
                   Total_of_VOI_Income__c, Total_of_Paystubs__c,
                   Paystubs_Count_of_Stubs__c,
                   Total_YTD_Income__c,
                   YTD_Start_Date__c, YTD_End_Date__c,
                   Notes__c, Verification_Complete__c
            FROM Income_Worksheet__c
            WHERE Certification_Review__c = '{rid_s}'
        """).get("records", [])

        assets = self.sf.query(f"""
            SELECT Id, Source_Name__c, Account_Type__c,
                   Self_Declared_Balance__c,
                   Self_Declared_Source__c,
                   Cash_Value__c, Income_from_Asset__c,
                   Asset_Cash_Value__c, Asset_Income__c,
                   Interest_Rate__c, Percentage_of_Ownership__c,
                   VOA_Current__c, VOA_Six_Month_Average_Balance__c,
                   VOA_Cash_Value__c, VOA_Net_Value__c,
                   Recent_Bank_Statement_Balance__c,
                   Six_Month_Average__c,
                   Current_Market_Value__c, Net_Value_of_Real_Estate__c,
                   House_Member_Name__c, Household_Member__c,
                   Notes__c, Verification_Complete__c
            FROM Asset_Worksheet__c
            WHERE Certification_Review__c = '{rid_s}'
        """).get("records", [])

        return {
            "review_id": rid,
            "review": review[0],
            "members": members,
            "income": income,
            "assets": assets,
        }

    def download_pdf(self, content_document_id: str) -> bytes:
        """Download the latest PDF version for a ContentDocument.

        Applies the min-pages gate — use for standalone packets. Multi-part
        merging goes through _download_content_version so the gate can be
        applied to the merged whole instead of each fragment.
        """
        pdf_bytes, title = self._download_content_version(content_document_id)
        self._check_min_pages(pdf_bytes, title)
        return pdf_bytes

    def _download_content_version(
        self, content_document_id: str,
    ) -> tuple[bytes, str]:
        """Download the latest version bytes + title, without the
        min-pages gate. Denylist and extension checks still apply."""
        cd_id_s = _escape_soql(content_document_id)
        versions = self.sf.query(f"""
            SELECT Id, Title, FileExtension, VersionData
            FROM ContentVersion
            WHERE ContentDocumentId = '{cd_id_s}'
              AND IsLatest = true
        """).get("records", [])

        if not versions:
            raise ValueError(
                f"No latest ContentVersion for document {content_document_id}"
            )

        version = versions[0]
        if (version.get("FileExtension") or "").lower() != "pdf":
            raise ValueError(
                f"ContentDocument {content_document_id} is not a PDF "
                f"(extension: {version.get('FileExtension')})"
            )

        # Defense in depth: even when the webhook specified this document
        # explicitly, refuse to process audit/review titles. get_pdf_for_case
        # filters these on the fallback path; without this check a webhook
        # that passes a Certification Review's content_document_id would
        # bypass that filter entirely.
        title = (version.get("Title") or "").lower()
        for token in self._PDF_TITLE_DENYLIST:
            if token in title:
                raise ValueError(
                    f"Refusing to process denylisted document title "
                    f"{version.get('Title')!r} (matches '{token}')"
                )

        version_data = version["VersionData"]
        download_url = f"https://{self.sf.sf_instance}{version_data}"
        for attempt in range(3):
            response = self.sf.session.get(
                download_url,
                headers={"Authorization": f"Bearer {self.sf.session_id}"},
            )
            if response.status_code == 200 and len(response.content) > 1000:
                return response.content, version.get("Title") or ""
            logger.warning(
                "PDF download attempt %d failed: status=%d size=%d",
                attempt + 1, response.status_code, len(response.content),
            )
            time.sleep(2 ** attempt)
        raise RuntimeError(
            f"Failed to download PDF {content_document_id} after 3 attempts"
        )

    def _download_merged_parts(
        self, group: list[dict], case_id: str,
    ) -> tuple[bytes, str]:
        """Download a packet-part group and merge into one PDF, in part
        order. The min-pages gate applies to the merged whole — each part
        alone may legitimately be below it. Returns (bytes, cd_id of the
        first part)."""
        parts = sorted(group, key=lambda c: c["part_no"])
        nums = [c["part_no"] for c in parts]
        if nums != list(range(nums[0], nums[0] + len(nums))):
            logger.warning(
                "Packet parts for case %s are non-contiguous (%s) — merging "
                "the parts that exist, in order: %s",
                case_id, nums, [c["title"] for c in parts],
            )
        merged = fitz.open()
        try:
            for part in parts:
                data, _title = self._download_content_version(part["cd_id"])
                with fitz.open(stream=io.BytesIO(data), filetype="pdf") as doc:
                    merged.insert_pdf(doc)
            merged_bytes = merged.tobytes()
            page_count = merged.page_count
        finally:
            merged.close()
        titles = " + ".join(c["title"] for c in parts)
        self._check_min_pages(merged_bytes, titles)
        logger.info(
            "Merged %d-part packet for case %s (%d pages): %s",
            len(parts), case_id, page_count, titles,
        )
        return merged_bytes, parts[0]["cd_id"]

    def _check_min_pages(self, pdf_bytes: bytes, title: str | None) -> None:
        """Reject PDFs below settings.min_pdf_pages.

        Counting locally with PyMuPDF is ~milliseconds and avoids burning OCR
        + LLM tokens on documents that can't possibly be a complete packet.
        """
        threshold = self._settings.min_pdf_pages
        with fitz.open(stream=io.BytesIO(pdf_bytes), filetype="pdf") as doc:
            page_count = doc.page_count
            first_page_text = doc[0].get_text().lower() if page_count else ""
        if page_count < threshold:
            # Digital PDFs carry embedded text — check it against the
            # denylist so a review report uploaded under a neutral title
            # ("...AR-Correction...") is named for what it is instead of
            # just "too short". Scanned PDFs (no embedded text) skip this.
            for token in self._PDF_TITLE_DENYLIST:
                if token in first_page_text:
                    raise ValueError(
                        f"Not a source packet: document content matches "
                        f"denylisted class ('{token}' in page text) "
                        f"(title: {title or '?'})"
                    )
            raise ValueError(
                f"PDF too short to be a source packet: {page_count} page(s) "
                f"< minimum {threshold} (title: {title or '?'})"
            )

    # ------------------------------------------------------------------
    # Write methods
    # ------------------------------------------------------------------

    # IDP_Testing_Results__c is a standard Long Text Area, max 32,768 chars.
    # If a future case ever produces output longer than this we want a clear
    # error rather than a silent Salesforce truncation, so we cap and warn.
    _FINDINGS_FIELD_MAX_CHARS = 32_000

    def get_case_findings(self, case_id: str) -> dict[str, Any] | None:
        """Read the previously-written findings + complete flag from a Case.

        Used by the audit read endpoint to surface results for cases that
        were finished and removed from the local JobStore.
        Returns None if the case isn't found.
        """
        case_id_s = _escape_soql(case_id)
        result = self.sf.query(f"""
            SELECT Id, CaseNumber, CertType__c, Funding_Program2__c,
                   IDP_Testing_Results__c, IDP_Audit_Complete__c
            FROM Case
            WHERE Id = '{case_id_s}'
        """).get("records", [])
        if not result:
            return None
        return result[0]

    def newest_pdf_created_at(self, case_id: str) -> float | None:
        """Epoch of the most recently created PDF on a Case; None if none.

        Used by the failed-case revisit sweep to detect documents that
        arrived after an audit failed for lack of a source packet.
        """
        case_id_s = _escape_soql(case_id)
        recs = self.sf.query(f"""
            SELECT ContentDocument.CreatedDate
            FROM ContentDocumentLink
            WHERE LinkedEntityId = '{case_id_s}'
              AND ContentDocument.FileExtension = 'pdf'
            ORDER BY ContentDocument.CreatedDate DESC
            LIMIT 1
        """).get("records", [])
        if not recs:
            return None
        raw = (recs[0].get("ContentDocument") or {}).get("CreatedDate") or ""
        try:
            # Salesforce format: 2026-07-16T17:13:00.000+0000 (UTC)
            return float(calendar.timegm(time.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")))
        except ValueError:
            return None

    def reset_audit_complete(self, case_id: str) -> None:
        """Flip IDP_Audit_Complete__c back to False so the poller re-fetches
        the case — used when new documents arrive after a failed audit."""
        result = self.sf.Case.update(case_id, {"IDP_Audit_Complete__c": False})
        if isinstance(result, int) and result >= 400:
            raise RuntimeError(
                f"reset_audit_complete returned {result} for {case_id}"
            )
        logger.info("Reset IDP_Audit_Complete=false on Case %s", case_id)

    def update_case_findings(self, case_id: str, findings_text: str) -> None:
        """Write findings to Case.IDP_Testing_Results__c and flip
        IDP_Audit_Complete__c to True. One Salesforce call so both fields
        land atomically — important because the poller's SOQL filters on
        IDP_Audit_Complete__c=False to skip already-audited cases.
        """
        if len(findings_text) > self._FINDINGS_FIELD_MAX_CHARS:
            logger.warning(
                "Findings text for case %s is %d chars — truncating to %d "
                "to fit IDP_Testing_Results__c Long Text Area limit",
                case_id, len(findings_text), self._FINDINGS_FIELD_MAX_CHARS,
            )
            findings_text = (
                findings_text[: self._FINDINGS_FIELD_MAX_CHARS - 200]
                + "\n\n[TRUNCATED — see IDP logs for full findings]"
            )

        result = self.sf.Case.update(case_id, {
            "IDP_Testing_Results__c": findings_text,
            "IDP_Audit_Complete__c": True,
        })
        # simple_salesforce returns the HTTP status code (204 = success)
        if isinstance(result, int) and result >= 400:
            raise RuntimeError(
                f"Salesforce update_case_findings returned {result} for {case_id}"
            )
        logger.info(
            "Wrote findings (%d chars) and set IDP_Audit_Complete=true on Case %s",
            len(findings_text), case_id,
        )

    # Title patterns indicating a non-source document the IDP must NOT
    # process: certification reviews, audit findings, review reports, etc.
    # If a Case has multiple PDFs and one matches a denylist token, it's
    # skipped so we land on the actual source packet instead.
    _PDF_TITLE_DENYLIST = (
        "certification review",
        "review report",
        "audit",
        "findings",
        "ai file audit",
        "review notes",
        "review comments",
    )

    def get_pdf_for_case(self, case_id: str) -> tuple[bytes, str]:
        """Find and download the source PDF for a Case.

        Strategy when content_document_id wasn't passed in the webhook:
          1. Query ContentVersion (latest) joined to the case, filtered to
             FileExtension='pdf' so non-PDF attachments don't waste cycles.
          2. Order by CreatedDate DESC — the file MuleSoft just processed
             is overwhelmingly the newest one.
          3. Skip titles matching the denylist (certification reviews,
             audit reports, etc.) since those aren't source packets.
          4. Download the first survivor.

        This is best-effort; the only authoritative way is for Salesforce
        to include content_document_id in the webhook payload.
        """
        case_id_s = _escape_soql(case_id)
        links = self.sf.query(f"""
            SELECT ContentDocumentId, ContentDocument.Title,
                   ContentDocument.FileExtension,
                   ContentDocument.CreatedDate
            FROM ContentDocumentLink
            WHERE LinkedEntityId = '{case_id_s}'
              AND ContentDocument.FileExtension = 'pdf'
            ORDER BY ContentDocument.CreatedDate DESC
        """).get("records", [])

        denylisted: list[str] = []
        rejected: list[str] = []  # "title: reason" for non-denylist skips
        candidates: list[dict] = []
        for link in links:
            cd = link.get("ContentDocument") or {}
            title = cd.get("Title") or link["ContentDocumentId"]
            if any(t in title.lower() for t in self._PDF_TITLE_DENYLIST):
                denylisted.append(title)
                continue
            candidates.append(
                {"cd_id": link["ContentDocumentId"], "title": title}
            )

        # Cluster multi-part packets. Candidates arrive newest-first, so
        # groups are tried newest-first too — same precedence as before.
        for group in _group_packet_parts(candidates):
            title = group[0]["title"]
            try:
                is_part_set = (
                    len(group) >= 2
                    and len({c["part_no"] for c in group}) == len(group)
                )
                if is_part_set:
                    pdf_bytes, cd_id = self._download_merged_parts(
                        group, case_id,
                    )
                else:
                    cd_id = group[0]["cd_id"]
                    pdf_bytes = self.download_pdf(cd_id)
                if denylisted or rejected:
                    logger.info(
                        "Picked PDF '%s' for case %s; skipped — "
                        "denylisted: %s, rejected: %s",
                        title, case_id, denylisted, rejected,
                    )
                return pdf_bytes, cd_id
            except (ValueError, RuntimeError) as exc:
                # Not a usable source packet (too short, download failed,
                # corrupt/non-PDF bytes) — move on to the next candidate.
                logger.info(
                    "Skipped candidate PDF '%s' for case %s: %s",
                    title, case_id, exc,
                )
                rejected.append(f"{title}: {exc}")
                continue

        raise ValueError(
            f"No usable source PDF found for case {case_id} — "
            f"{len(denylisted)} denylisted title(s) {denylisted or ''}, "
            f"{len(rejected)} rejected candidate(s) {rejected or ''}"
        )
