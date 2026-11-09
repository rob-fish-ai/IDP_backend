"""Several Cartograph environments on one engine host (2026-11-05):
Cartograph runs dev, staging and prod, and its staging shares the prod
engine. The caller is identified by which callback secret verifies its
signature, its result goes back to that environment's ingest URL, and its
job-store row is keyed per environment so a staging run of a case never
collides with the prod run of the same case_ref."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.config import CartographEnvironment, Settings
from app.core.dependencies import get_settings
from app.services.cartograph import environments as envs
from app.services.cartograph.signing import SIGNATURE_HEADER, TIMESTAMP_HEADER, sign, verify

TWO = {
    "prod": CartographEnvironment(ingest_url="https://carto.example/webhooks/runpod_ocr_results",
                                  ingest_secret="prod-out", callback_secret="prod-in"),
    "staging": CartographEnvironment(ingest_url="https://staging.carto.example/webhooks/runpod_ocr_results",
                                     ingest_secret="stg-out", callback_secret="stg-in"),
}


def _settings(tmp_path, **kw):
    return Settings(audit_job_db=tmp_path / "jobs.db", output_dir=tmp_path, cartograph_environments=TWO,
                    cartograph_ingest_url="", cartograph_ingest_secret="", cartograph_callback_secret="", **kw)


def test_the_single_environment_settings_still_define_one_named_default(tmp_path):
    s = Settings(audit_job_db=tmp_path / "jobs.db", cartograph_environments={},
                 cartograph_ingest_url="https://one.example/in", cartograph_ingest_secret="out", cartograph_callback_secret="in")
    assert list(envs.environments(s)) == ["default"]
    assert envs.default_environment(s) == "default"
    assert envs.job_key(s, "default", "J-1") == "J-1" and envs.job_key(s, None, "J-1") == "J-1"
    body = b'{"case_ref":"J-1"}'
    ts, sig = sign(body, "in")
    assert envs.identify(s, body, ts, sig) == ("default", "ok")
    assert envs.is_configured(s) and envs.for_delivery(s)[0] == "default"
    none = Settings(audit_job_db=tmp_path / "jobs.db", cartograph_environments={},
                    cartograph_ingest_url="", cartograph_ingest_secret="", cartograph_callback_secret="")
    assert envs.environments(none) == {} and envs.identify(none, body, ts, sig)[0] is None
    with pytest.raises(envs.CartographNotConfigured):
        envs.for_delivery(none)


def test_the_signature_names_the_environment_and_rows_are_keyed_by_it(tmp_path):
    s = _settings(tmp_path)
    body = b'{"case_ref":"J-1"}'
    ts, sig = sign(body, "stg-in")
    assert envs.identify(s, body, ts, sig) == ("staging", "ok")
    ts, sig = sign(body, "prod-in")
    assert envs.identify(s, body, ts, sig) == ("prod", "ok")
    ts, sig = sign(body, "someone-else")
    name, reason = envs.identify(s, body, ts, sig)
    assert name is None and "any configured environment" in reason
    assert envs.identify(s, body, None, None)[0] is None
    # prod is the first configured, so its rows keep bare case_ref keys
    assert envs.default_environment(s) == "prod"
    assert envs.job_key(s, "prod", "J-1") == "J-1"
    assert envs.job_key(s, "staging", "J-1") == "staging:J-1"
    explicit = _settings(tmp_path, cartograph_default_environment="staging")
    assert envs.job_key(explicit, "staging", "J-1") == "J-1" and envs.job_key(explicit, "prod", "J-1") == "prod:J-1"
    d = envs.describe(s)
    assert d["default"] == "prod" and d["environments"]["staging"]["ingest_host"] == "staging.carto.example"
    assert "stg-out" not in json.dumps(d) and "stg-in" not in json.dumps(d)


def test_a_case_from_each_environment_is_its_own_job_and_its_own_repeat_guard(tmp_path, monkeypatch):
    from app.main import app
    from app.routers import integration
    from app.services.audit import job_store as js
    s = _settings(tmp_path)
    app.dependency_overrides[get_settings] = lambda: s
    # The lifespan and the maintenance thread read settings directly, and
    # the job store is a process-wide singleton: both must see the test's
    # store, never the host's.
    monkeypatch.setattr("app.main.get_settings", lambda: s)
    monkeypatch.setattr("app.services.cartograph.maintenance.get_settings", lambda: s)
    monkeypatch.setattr(js, "_SINGLETON", js.JobStore(s.audit_job_db))
    get_job_store = js.get_job_store
    queued = []
    monkeypatch.setattr(integration, "enqueue_case", lambda **kw: queued.append(kw))
    body = json.dumps({"case_ref": "J-1", "documents": [{"url": "https://x/1.pdf", "filename": "p.pdf", "job_document_id": 1}]}).encode()

    def post(secret, path="/integration/case", b=body):
        ts, sig = sign(b, secret)
        return client.post(path, content=b, headers={"Content-Type": "application/json", TIMESTAMP_HEADER: ts, SIGNATURE_HEADER: sig})

    try:
        with TestClient(app) as client:
            r = post("stg-in")
            assert r.status_code == 202 and r.json() == {"status": "accepted", "case_ref": "J-1", "environment": "staging"}
            r = post("prod-in")
            assert r.status_code == 202 and r.json()["environment"] == "prod"
            assert post("nobody").status_code == 401
            store = get_job_store(s.audit_job_db)
            assert store.get("staging:J-1")["environment"] == "staging" and store.get("staging:J-1")["case_number"] == "J-1"
            assert store.get("J-1")["environment"] == "prod"
            assert [(q["environment"], q["case_ref"]) for q in queued] == [("staging", "J-1"), ("prod", "J-1")]
            # a repeat of the staging run is caught on staging and does not touch prod
            store.mark_done("staging:J-1", findings_text="", confidence=1.0)
            assert post("stg-in").json()["status"] == "already_delivered"
            assert post("prod-in").json()["status"] == "already_in_progress"
            # the import result lands on the environment's own row
            res = json.dumps({"case_ref": "J-1", "status": "ok", "scan_id": 7}).encode()
            assert post("stg-in", "/integration/import_result", res).status_code == 200
            assert store.get("staging:J-1")["cartograph_import_result"] and not store.get("J-1")["cartograph_import_result"]
            health = client.get("/health").json()
            assert sorted(health["environments"]) == ["prod", "staging"] and health["default_environment"] == "prod"
            assert health["storage"]["output_free_mb"] > 0 and health["storage"]["job_db_mb"] >= 0
    finally:
        app.dependency_overrides.clear()


def test_delivery_goes_to_the_environment_the_case_came_from(tmp_path, monkeypatch):
    from app.services.cartograph import client as cl
    s = _settings(tmp_path)
    sent = []

    class FakeClient:
        def __init__(self, timeout=None): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def post(self, url, content=None, headers=None):
            sent.append((url, content, headers))
            return SimpleNamespace(status_code=202, json=lambda: {"scan_id": 1}, text="")

    monkeypatch.setattr(cl.httpx, "Client", FakeClient)
    cl.post_extraction({"case_ref": "J-1"}, s, environment="staging")
    cl.post_extraction({"case_ref": "J-1"}, s)                 # unnamed: the default environment
    assert [u for u, _, _ in sent] == [TWO["staging"].ingest_url, TWO["prod"].ingest_url]
    url, body, headers = sent[0]
    assert verify(body, "stg-out", headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER])[0]
    assert not verify(body, "prod-out", headers[TIMESTAMP_HEADER], headers[SIGNATURE_HEADER])[0]
    with pytest.raises(cl.CartographNotConfigured):
        cl.post_extraction({"case_ref": "J-1"}, s, environment="dev")
    assert cl.post_failure("J-1", "boom", s, environment="dev") is None   # best effort, never raises


def test_a_case_from_a_named_environment_finishes_on_its_own_row(tmp_path, monkeypatch):
    """The audit task writes every state change under the environment-keyed
    job id, including the last ones: a staging case that is delivered is
    done under "staging:J-1", and one its ingest rejects is failed there.
    Written by bare case_ref, those updates would touch no row, the case
    would sit in "extracted" and the watchdog would fail it an hour later."""
    from app.services.audit.job_store import JobStore
    from app.services.cartograph import tasks
    s = _settings(tmp_path)
    store = JobStore(s.audit_job_db)
    extraction = SimpleNamespace(model_dump=lambda: {"ok": True}, findings=["Finding one"], field_scores=None)
    monkeypatch.setattr(tasks, "fetch_packet", lambda documents: (b"%PDF", [], None))
    monkeypatch.setattr(tasks, "keep_packet", lambda *a, **k: None)
    monkeypatch.setattr(tasks, "process_pdf_full", lambda *a, **k: {"extraction": extraction})
    monkeypatch.setattr(tasks, "build_payload", lambda *a, **k: SimpleNamespace(payload={}, warnings=[], member_count=1))
    answers = iter([{"ok": True, "status_code": 202, "response": {"scan_id": 1}, "bytes_sent": 2},
                    {"ok": False, "status_code": 422, "response": "bad", "bytes_sent": 2}])
    posted = []
    monkeypatch.setattr(tasks, "post_extraction", lambda payload, settings, environment=None: (posted.append(environment), next(answers))[1])

    def run(case):
        store.upsert_pending(f"staging:{case}", case, "annual", "LIHTC", None, claimed=True, environment="staging")
        tasks._audit_case(store, s, case_ref=case, documents=[{"url": "https://example.invalid/p.pdf"}],
                          cert_type=None, program=None, job_id=None, community_id=None, unit_number=None,
                          effective_date=None, environment="staging")

    run("J-1")
    assert store.get("staging:J-1")["state"] == "done" and store.get("staging:J-1")["findings_text"] == "Finding one"
    run("J-2")
    assert store.get("staging:J-2")["state"] == "comparison_failed" and "422" in store.get("staging:J-2")["error"]
    assert posted == ["staging", "staging"] and store.get("J-1") is None and store.get("J-2") is None
