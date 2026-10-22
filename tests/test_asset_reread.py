"""The second read of an asset document that prints dollar figures.

On J-CCAC-07949 the packet's only bank document was a Chase ATM receipt
printing the checking balance. The first read returned nothing about one
time in ten; the re-read, worded "extract only the accounts it actually
shows; if it shows none, return an empty list", returned nothing every
time, so the account was never recovered and the certification's savings
figure went out unverified. The re-read now names what an account looks
like, and a record it returns without any figure (the blank request form
read as an account) is not kept.
"""

from app.core.config import Settings
from app.schemas.extraction import DocumentGroup
from app.services import extractor as ex

RECEIPT = (
    "--- Page 19 ---\n"
    "CHASE\n\n04/06/26 10:00 AM\nCard#: 7468\n"
    "Withdraw from Chk Acct_0000: $400.00\nSurcharge: $4.00\nTotal Withdrawal: $404.00\n\n"
    "Balance: $667.82\n\nJPMorgan Chase Bank, N.A. Member FDIC\n"
)

CHASE_RECORD = {
    "documentType": "Bank Statement — Checking",
    "assetOwner": "Lawrence Lindsey",
    "sourceName": "Chase",
    "accountType": "Checking",
    "accountNumber": "0000",
    "currentBalance": "667.82",
    "bankStatment": [{"statementDate": "2026-04-06", "balance": "667.82", "accountNumber": "0000"}],
    "sourcePages": [19],
    "evidence": {"currentBalance": "Balance: $667.82"},
}

BLANK_FORM_RECORD = {
    "documentType": "Verification of Assets",
    "assetOwner": "Lawrence Lindsey",
    "sourceName": "St Peter Claver Courts Management",
    "accountType": "Savings",
    "currentBalance": None,
    "verificationOfAsset": None,
    "sourcePages": [19],
    "evidence": {},
}


def _group() -> DocumentGroup:
    return DocumentGroup(document_type="Bank Statement", category="include",
                         pages=[19], page_range="19", combined_text=RECEIPT)


def _stub(monkeypatch, answers: list[dict]) -> list[str]:
    """Answer successive model calls from `answers`; return the prompts seen."""
    prompts: list[str] = []

    def fake(system_prompt, prompt, settings):
        prompts.append(prompt)
        return answers[min(len(prompts) - 1, len(answers) - 1)]

    monkeypatch.setattr(ex, "call_llm_json", fake)
    return prompts


def test_reread_recovers_the_account_the_first_read_missed(monkeypatch):
    prompts = _stub(monkeypatch, [{"assetInformation": []}, {"assetInformation": [CHASE_RECORD]}])
    result = ex.extract_assets([_group()], Settings(), "AR", ["Lawrence Lindsey"])
    assert len(prompts) == 2
    assert prompts[1].startswith(ex._ASSET_REREAD_INTRO)
    assert [(r.sourceName, r.accountType, r.currentBalance) for r in result.assetInformation] == [
        ("Chase", "Checking", "667.82"),
    ]


def test_reread_names_what_an_account_looks_like():
    # The wording the model saw before always returned an empty list for a
    # receipt; the re-read must say that a printed balance is an account.
    intro = ex._ASSET_REREAD_INTRO.lower()
    assert "receipt" in intro and "balance" in intro
    assert "extract only" not in intro


def test_reread_record_without_a_figure_is_not_kept(monkeypatch):
    _stub(monkeypatch, [{"assetInformation": []}, {"assetInformation": [BLANK_FORM_RECORD]}])
    result = ex.extract_assets([_group()], Settings(), "AR", ["Lawrence Lindsey"])
    assert result.assetInformation == []


def test_no_reread_when_the_document_prints_no_figures(monkeypatch):
    prompts = _stub(monkeypatch, [{"assetInformation": []}])
    g = _group()
    g.combined_text = "--- Page 19 ---\nVerification of Assets\nName of Institution\nAccount Number\n"
    result = ex.extract_assets([g], Settings(), "AR", ["Lawrence Lindsey"])
    assert len(prompts) == 1
    assert result.assetInformation == []
