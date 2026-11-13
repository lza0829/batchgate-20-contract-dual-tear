import pytest

import app.db as db
from app.engines.contract import check_upstream_contracts
from app.repositories import contracts as contracts_repo
from app.repositories import orchestrations as orch_repo
from app.repositories import watermarks as wm_repo
from app.seed import init_db
from app.services.orchestrate_service import run_orchestration


def test_contract_passes_when_rules_satisfied():
    contracts = [{"upstream_name": "a", "min_rows": 2, "required_field": "watermark_ts"}]
    summaries = {"a": {"artifact_rows": 3, "watermark_ts": "2026-11-10T00:00:00Z"}}
    assert check_upstream_contracts(contracts, summaries) == []


def test_contract_min_rows_violation():
    contracts = [{"upstream_name": "a", "min_rows": 1, "required_field": None}]
    summaries = {"a": {"artifact_rows": 0, "watermark_ts": "2026-11-10T00:00:00Z"}}
    violations = check_upstream_contracts(contracts, summaries)
    assert len(violations) == 1
    assert violations[0]["rule"] == "min_rows"
    assert violations[0]["actual"] == 0


def test_contract_required_field_missing_or_null():
    contracts = [{"upstream_name": "a", "min_rows": 0, "required_field": "watermark_ts"}]
    missing = check_upstream_contracts(contracts, {"a": {"artifact_rows": 5}})
    assert [v["rule"] for v in missing] == ["required_field"]
    null = check_upstream_contracts(contracts, {"a": {"artifact_rows": 5, "watermark_ts": None}})
    assert [v["rule"] for v in null] == ["required_field"]


def test_contract_flags_missing_upstream_summary():
    contracts = [{"upstream_name": "a", "min_rows": 0, "required_field": None}]
    violations = check_upstream_contracts(contracts, {})
    assert [v["rule"] for v in violations] == ["upstream_summary"]


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    init_db()
    return tmp_path / "app.db"


def _attempts_by_job(attempts):
    return {a["job_name"]: a for a in attempts}


def test_seeded_run_covers_contract_pass_and_block(fresh_db):
    report = run_orchestration()["report"]

    conn = db.connect()
    try:
        attempts = orch_repo.list_attempts(conn, report["orchestration_id"])
        by_job = _attempts_by_job(attempts)

        # Contract-pass path: healthy upstreams -> downstream ran and succeeded.
        assert by_job["fact_order_daily"]["status"] == "success"

        # Contract-blocked path: ingest_refunds succeeded with 0 rows (empty
        # success), so fact_refund_daily was gated before execution.
        assert by_job["ingest_refunds"]["status"] == "success"
        assert by_job["ingest_refunds"]["detail"]["artifact_rows"] == 0
        blocked = by_job["fact_refund_daily"]
        assert blocked["status"] == "skipped_contract"
        rules = [v["rule"] for v in blocked["detail"]["violations"]]
        assert "min_rows" in rules
        assert "ingest_refunds" in blocked["detail"]["reason"]

        # Contract-blocked downstream must not advance its own watermark.
        watermarks = {w["job_name"]: w for w in wm_repo.list_watermarks(conn)}
        assert watermarks["fact_refund_daily"]["watermark_ts"] is None
        assert watermarks["fact_order_daily"]["watermark_ts"] is not None

        # Report counts reconcile with the attempt records.
        assert report["attempt_count"] == len(attempts)
        for status, count in report["counts_by_status"].items():
            assert count == sum(1 for a in attempts if a["status"] == status)
        assert report["succeeded"] == report["counts_by_status"]["success"]
        assert report["failed"] == report["counts_by_status"]["failed"]
        assert report["skipped_contract"] == 1
        assert report["skipped"] == (
            report["skipped_dep"] + report["skipped_contract"]
        )
        assert [b["job"] for b in report["contract_blocked"]] == ["fact_refund_daily"]
    finally:
        conn.close()


def test_contract_upsert_roundtrip(fresh_db):
    conn = db.connect()
    try:
        job_id = conn.execute(
            "SELECT id FROM jobs WHERE name='fact_order_daily'"
        ).fetchone()["id"]
        upstream_id = conn.execute(
            "SELECT id FROM jobs WHERE name='ingest_orders'"
        ).fetchone()["id"]
        contracts_repo.upsert_contract(conn, job_id, upstream_id, 5, "sample")
        conn.commit()
        row = contracts_repo.get_contract(conn, job_id, upstream_id)
        assert row["min_rows"] == 5
        assert row["required_field"] == "sample"
        assert row["upstream_name"] == "ingest_orders"
        assert contracts_repo.delete_contract(conn, job_id, upstream_id)
        conn.commit()
        assert contracts_repo.get_contract(conn, job_id, upstream_id) is None
    finally:
        conn.close()
