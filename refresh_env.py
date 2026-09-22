"""One-shot environment refresh — survives terminal/VS Code close.

1. Clears IDP_Testing_Results__c + IDP_Audit_Complete__c on ONLY the
   cases this system wrote (findings marker or local job row). The other
   ~9k flagged cases were bulk-flagged to keep the poller away from
   historical backlog and are left untouched.
2. Wipes the local JobStore rows (backup taken beforehand by the caller).

Run detached:  setsid nohup .venv/bin/python refresh_env.py > /var/data/refresh_env.log 2>&1 &
Watch:         tail -f /var/data/refresh_env.log
"""
import sqlite3
import time

from app.core.dependencies import get_settings
from app.services.salesforce.client import get_salesforce_client

DB = "/var/data/audit_jobs.db"


def main() -> None:
    sf = get_salesforce_client(get_settings())

    print("[1/3] scanning flagged cases for our findings marker...", flush=True)
    ours = set()
    scanned = 0
    for r in sf.sf.query_all_iter(
        "SELECT Id, IDP_Testing_Results__c FROM Case "
        "WHERE IDP_Audit_Complete__c = true"
    ):
        scanned += 1
        if "AI FILE AUDIT" in (r.get("IDP_Testing_Results__c") or ""):
            ours.add(r["Id"])
        if scanned % 2000 == 0:
            print(f"  scanned {scanned}...", flush=True)
    local = {row[0] for row in sqlite3.connect(DB).execute(
        "SELECT case_id FROM audit_jobs")}
    targets = sorted(ours | local)
    print(f"  scanned {scanned} flagged; ours={len(ours)}, "
          f"local={len(local)}, to clear: {len(targets)}", flush=True)

    print("[2/3] clearing Salesforce audit fields...", flush=True)
    ok = fail = 0
    t0 = time.time()
    for i, cid in enumerate(targets):
        try:
            sf.sf.Case.update(cid, {
                "IDP_Testing_Results__c": None,
                "IDP_Audit_Complete__c": False,
            })
            ok += 1
        except Exception as exc:  # noqa: BLE001 — log and continue
            fail += 1
            if fail <= 10:
                print(f"  FAIL {cid}: {str(exc)[:120]}", flush=True)
        if (i + 1) % 200 == 0:
            print(f"  {i + 1}/{len(targets)} ({time.time() - t0:.0f}s)",
                  flush=True)
    print(f"  cleared {ok}, failed {fail}", flush=True)

    print("[3/3] wiping local JobStore rows...", flush=True)
    conn = sqlite3.connect(DB)
    n = conn.execute("SELECT count(*) FROM audit_jobs").fetchone()[0]
    conn.execute("DELETE FROM audit_jobs")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    print(f"  deleted {n} local rows, vacuumed", flush=True)

    print("DONE — safe to restart the worker; it will re-discover the "
          "cleared cases and re-audit them on the new code.", flush=True)


if __name__ == "__main__":
    main()
