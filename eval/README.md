# Extraction-quality harness

Gold values for real packets live in `gold/<case>.json`; `run_eval.py` replays a packet
through the pipeline and scores every run against them. Run outputs land in `runs/`
(git-ignored) so a scoring-only change can be re-scored without new model calls.

    .venv/bin/python eval/run_eval.py --all --stored            # score what is in the job store, no LLM calls
    .venv/bin/python eval/run_eval.py --case J-PORT-05318 --runs 3   # replay stored OCR 3x, report spread
    .venv/bin/python eval/run_eval.py --case J-PORT-05318 --json eval/runs/J-PORT-05318/*.json
    .venv/bin/python eval/run_eval.py --case CAS570103 --pdf /root/testpackets/packet.pdf --runs 2

Replaying stored OCR skips the vision-dependent steps (signature check, required-field
image recovery) because the page images are gone; everything after OCR runs for real.
Use `--pdf` for the full path.

What is scored: certification scalars, each member's DOB / SSN last four / relationship,
each income source's annual figure and record count, each asset's balance and income,
the asset total over all records (double counting shows here), extra records, and the
expected / forbidden finding codes. With more than one run the report lists which checks
were unstable and which pages the classifier labelled differently.

Gold conventions: `optional: true` on an income record means absence is not a miss but a
different value is; `accept_member_last` tolerates a known attribution quirk;
`certificationType: null` means the caller value overrides and the field is not scored.
Add a case by dropping a gold file here; the case id must match the job-store `case_id`.

Thinking A/B: extraction calls send an explicit thinking configuration taken from
`IDP_LLM_THINKING` (`disabled`, the default, or `adaptive`). To compare, replay the same
cases under both and read the score and the wall time:

    IDP_LLM_THINKING=disabled .venv/bin/python eval/run_eval.py --case J-PORT-05318 --runs 2
    IDP_LLM_THINKING=adaptive .venv/bin/python eval/run_eval.py --case J-PORT-05318 --runs 2

Measured 2026-09-11 on the three benchmark cases (one run each, same code): identical
scores on 05754 and 05319; on 05318 the adaptive run read the declared tables more
carefully (37/37 against 35/38) but took 3m48s against 55s. The declared-read guards
added afterwards (total rows, household-level income) close that gap in code.

Rule tests: every reconciliation, calculation, normaliser, identity, scoring and payload
rule is pinned in `tests/` with one case where it must fire and one where it must not,
so a change that widens a rule fails before it reaches a packet the rule was never
written against. No model calls, under two seconds:

    .venv/bin/pip install -r requirements-dev.txt
    .venv/bin/python -m pytest -q tests
