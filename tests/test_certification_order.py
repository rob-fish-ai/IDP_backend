"""The certification with the later effective date is the current one."""
from app.schemas.extraction import DocumentGroup
from app.services.two_pass_classifier import _order_certifications


def _group(doc_type, pages, category="certification"):
    return DocumentGroup(document_type=doc_type, category=category, pages=pages,
                         page_range=f"{pages[0]}-{pages[-1]}" if len(pages) > 1 else str(pages[0]), combined_text="")


# OCR of a 50059 flattens the table row-major on one revision and prints
# "label: value" on another; both shapes occur in one packet.
NEWER = "Owner's Certification ... <td>Effective Date<br>8/1/2026</td><td>Certification Type<br>AR</td> 86. Total Annual Income 12,166.00"
OLDER = "Owner's Certification of Compliance ... Effective Date: 8/1/2025  Certification Type: AR  Total Annual Income: 11,844.00"


def test_the_form_with_the_later_date_becomes_current_whichever_label_the_model_gave():
    text = {4: NEWER, 5: "page two of the 2026 form", 6: OLDER, 7: "page two of the 2025 form"}
    groups = [_group("HUD 50059 (Previous)", [4, 5], category="ignore"), _group("HUD 50059", [6, 7])]
    out, updates = _order_certifications(groups, text)
    by_pages = {tuple(g.pages): g for g in out}
    assert by_pages[(4, 5)].document_type == "HUD 50059" and by_pages[(4, 5)].category == "certification"
    assert by_pages[(6, 7)].document_type == "HUD 50059 (Previous)" and by_pages[(6, 7)].category == "ignore"
    assert sorted(updates) == [(4, "HUD 50059", "certification"), (5, "HUD 50059", "certification"),
                               (6, "HUD 50059 (Previous)", "ignore"), (7, "HUD 50059 (Previous)", "ignore")]


def test_every_certification_family_is_ordered_the_same_way():
    text = {2: "Tenant Income Certification  Effective Date: 01/01/2026", 9: "Tenant Income Certification  Effective Date: 01/01/2025"}
    groups = [_group("Tenant Income Certification (TIC) (Previous)", [2], category="ignore"), _group("Tenant Income Certification (TIC)", [9])]
    out, _ = _order_certifications(groups, text)
    assert [(g.document_type, g.pages) for g in out] == [("Tenant Income Certification (TIC)", [2]), ("Tenant Income Certification (TIC) (Previous)", [9])]

    text = {2: "Form RD 3560-8  Effective Date: 07/01/2026", 9: "Form RD 3560-8  Effective Date: 07/01/2025"}
    groups = [_group("HUD 3560 Form (Previous)", [2], category="ignore"), _group("HUD 3560 Form", [9])]
    out, _ = _order_certifications(groups, text)
    assert [(g.document_type, g.pages) for g in out] == [("HUD 3560 Form", [2]), ("HUD 3560 Form (Previous)", [9])]


def test_labels_the_model_got_right_are_left_alone():
    text = {4: NEWER, 6: OLDER}
    groups = [_group("HUD 50059", [4]), _group("HUD 50059 (Previous)", [6], category="ignore")]
    out, updates = _order_certifications(groups, text)
    assert updates == [] and [g.document_type for g in out] == ["HUD 50059", "HUD 50059 (Previous)"]


def test_no_swap_without_a_readable_date_on_the_current_form_or_with_two_current_forms():
    text = {4: NEWER, 6: "Owner's Certification, effective date illegible"}
    groups = [_group("HUD 50059 (Previous)", [4], category="ignore"), _group("HUD 50059", [6])]
    _, updates = _order_certifications(groups, text)
    assert updates == []

    text = {1: OLDER, 4: NEWER, 6: OLDER}
    groups = [_group("HUD 50059", [1]), _group("HUD 50059 (Previous)", [4], category="ignore"), _group("HUD 50059", [6])]
    _, updates = _order_certifications(groups, text)
    assert updates == []
