"""Which program's rules a packet is held to.

HUD and USDA files owe forms a tax-credit file does not: the 9887 consent
package, the acknowledgement of HUD forms, the citizenship and race
declarations, Social Security numbers for every member over six, EIV
reports. Until 2026-11-05 each rule tested for a HUD property in its own
words, and one of them matched "section" — which is also Section 42, the
tax credit itself. Reviewers rejected HUD findings on LIHTC properties
(SSN missing, the HUD forms acknowledgement, EIV expectations) more than
any other kind, so there is one test now, and every HUD rule uses it.
"""
from __future__ import annotations

import re
from typing import Iterable

# Program names Cartograph sends and the forms that name a HUD or USDA
# file. Whole words, so "RAD" is not found inside "Colorado" and "Section"
# alone (Section 42) says nothing.
_HUD_PROGRAM = re.compile(
    r"\b(hud|usda|rural\s+development|rd|rad|prac|pbra|pbv|public\s+housing"
    r"|section\s*(8|202|811|236|221|515))\b",
    re.IGNORECASE,
)


def is_hud_property(funding_program: str | None, document_types: Iterable[str] = ()) -> bool:
    """True when the caller's program is a HUD or USDA one, or the packet
    holds a current HUD 50059 — a mixed-financed property certifies on the
    HUD form whatever the program field says. A previous 50059 is history
    and proves nothing about the current file."""
    if funding_program and _HUD_PROGRAM.search(funding_program):
        return True
    return any(
        "HUD 50059" in dt and "(Previous)" not in dt
        for dt in document_types if dt
    )
