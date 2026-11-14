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


def test_contract_rejects_zero_rows_even_when_field_present():
    """An empty success must not clear a positive min_rows floor."""
    contracts = [{"upstream_name": "a", "min_rows": 1, "required_field": "watermark_ts"}]
    summaries = {"a": {"artifact_rows": 0, "watermark_ts": "2026-11-10T00:00:00Z"}}
    violations = check_upstream_contracts(contracts, summaries)
    assert [v["rule"] for v in violations] == ["min_rows"]


def test_contract_rejects_missing_row_count():
    contracts = [{"upstream_name": "a", "min_rows": 0, "required_field": None}]
    violations = check_upstream_contracts(contracts, {"a": {"ok": True}})
    assert [v["rule"] for v in violations] == ["min_rows"]


def test_list_contracts_for_job_keeps_min_rows_one(fresh_db):
    """Contract rows must reach the gate unclamped: min_rows=1 is the empty-success gate."""
    db_conn = db.connect()
    try:
        downstream = db_conn.execute(
            "SELECT id FROM jobs WHERE name='fact_refund_daily'"
        ).fetchone()["id"]
        rows = contracts_repo.list_contracts_for_job(db_conn, downstream)
        assert [r["min_rows"] for r in rows] == [1]
    finally:
        db_conn.close()


@pytest.fixture()
def gated_chain(tmp_path, monkeypatch):
    """ingest_empty (0 rows) -> mid -> leaf, with contracts on both edges."""
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    init_db()
    conn = db.connect()
    try:
        empty_fixture = tmp_path / "ingest_empty.json"
        empty_fixture.write_text(
            '{"watermark_ts":"2026-11-10T20:00:00Z","rows":[]}', encoding="utf-8"
        )
        leaf_fixture = tmp_path / "leaf.json"
        leaf_fixture.write_text(
            '{"watermark_ts":"2026-11-10T22:00:00Z","rows":[{"id":1}]}', encoding="utf-8"
        )
        cur = conn.execute(
            "INSERT INTO jobs(name, fixture_path, depends_on_json, max_attempts, enabled, note)"
            " VALUES ('ingest_empty', ?, '[]', 1, 1, 'empty success')",
            (str(empty_fixture),),
        )
        empty_id = int(cur.lastrowid)
        mid_id = conn.execute(
            "SELECT id FROM jobs WHERE name='fact_refund_daily'"
        ).fetchone()["id"]
        conn.execute(
            "UPDATE jobs SET depends_on_json='[\"ingest_empty\"]' WHERE id=?", (mid_id,)
        )
        cur = conn.execute(
            "INSERT INTO jobs(name, fixture_path, depends_on_json, max_attempts, enabled, note)"
            " VALUES ('leaf', ?, '[\"fact_refund_daily\"]', 1, 1, 'depends on gated job')",
            (str(leaf_fixture),),
        )
        leaf_id = int(cur.lastrowid)
        for job_id in (empty_id, mid_id, leaf_id):
            conn.execute(
                "INSERT OR IGNORE INTO watermarks(job_id, watermark_ts, artifact_rows, updated_at)"
                " VALUES (?,NULL,0,NULL)",
                (job_id,),
            )
        conn.execute(
            "DELETE FROM output_contracts WHERE job_id IN (?, ?)", (mid_id, leaf_id)
        )
        contracts_repo.upsert_contract(conn, mid_id, empty_id, 1, "watermark_ts")
        contracts_repo.upsert_contract(conn, leaf_id, mid_id, 1, "watermark_ts")
        conn.commit()
    finally:
        conn.close()
    return {"mid": "fact_refund_daily", "leaf": "leaf", "empty": "ingest_empty"}


def test_blocked_downstream_neither_runs_nor_raises_watermark(gated_chain):
    report = run_orchestration()["report"]
    conn = db.connect()
    try:
        attempts = _attempts_by_job(
            orch_repo.list_attempts(conn, report["orchestration_id"])
        )
        mid = attempts[gated_chain["mid"]]
        assert mid["status"] == "skipped_contract"
        assert mid["detail"]["executed"] is False
        assert mid["detail"]["watermark_advanced"] is False
        assert mid["detail"]["contract_verdict"] == "blocked"
        assert "result" not in mid["detail"]  # fixture never ran

        # The gated job is not a trusted upstream: its dependent is skipped too.
        leaf = attempts[gated_chain["leaf"]]
        assert leaf["status"] == "skipped_dep"
        assert gated_chain["mid"] in leaf["detail"]["reason"]

        watermarks = {w["job_name"]: w for w in wm_repo.list_watermarks(conn)}
        assert watermarks[gated_chain["mid"]]["watermark_ts"] is None

        # Report, attempts, verdicts and watermarks cross-check.
        assert report["skipped_contract"] == 1
        assert report["skipped_dep"] == report["counts_by_status"]["skipped_dep"]
        assert report["skipped"] == (
            report["skipped_dep"] + report["skipped_contract"]
        )
        assert report["reconciliation"]["ok"] is True
        blocked = {b["job"]: b for b in report["contract_blocked"]}
        assert set(blocked) == {gated_chain["mid"]}
        assert blocked[gated_chain["mid"]]["watermark_advanced"] is False
        checks = {c["job"]: c for c in report["contract_checks"]}
        assert checks[gated_chain["mid"]]["passed"] is False
        assert checks[gated_chain["mid"]]["executed"] is False
    finally:
        conn.close()


def test_blocked_attempt_carries_verifiable_evidence(fresh_db):
    """The skip reason must be checkable from the attempt record alone."""
    report = run_orchestration()["report"]
    conn = db.connect()
    try:
        blocked = _attempts_by_job(
            orch_repo.list_attempts(conn, report["orchestration_id"])
        )["fact_refund_daily"]
        detail = blocked["detail"]
        assert detail["observed"] == {"ingest_refunds": {"artifact_rows": 0, "watermark_ts": "2026-11-10T18:30:00Z"}}
        assert detail["violations"][0]["actual"] == 0
    finally:
        conn.close()


def test_contract_passes_only_after_upstream_really_ran(fresh_db):
    """A passing verdict must match an upstream success attempt in the same run."""
    report = run_orchestration()["report"]
    conn = db.connect()
    try:
        attempts = _attempts_by_job(
            orch_repo.list_attempts(conn, report["orchestration_id"])
        )
        passed = [c for c in report["contract_checks"] if c["passed"]]
        assert passed
        for c in passed:
            assert c["executed"] is True
            assert c["watermark_matches_attempt"] is True
            for upstream in c["upstreams"]:
                assert attempts[upstream]["status"] == "success"
                assert attempts[upstream]["detail"]["artifact_rows"] >= 1
            assert attempts[c["job"]]["detail"]["contract_verdict"] == "passed"
    finally:
        conn.close()


def test_repeated_runs_keep_blocked_watermark_stable(fresh_db):
    """Even on a second run, the blocked downstream still neither executes nor moves."""
    first = run_orchestration()["report"]
    second = run_orchestration()["report"]

    conn = db.connect()
    try:
        watermarks = {w["job_name"]: w for w in wm_repo.list_watermarks(conn)}
        assert watermarks["fact_refund_daily"]["watermark_ts"] is None
        for report in (first, second):
            attempts = _attempts_by_job(
                orch_repo.list_attempts(conn, report["orchestration_id"])
            )
            assert attempts["fact_refund_daily"]["status"] == "skipped_contract"
            assert report["skipped_contract"] == 1
            assert report["reconciliation"]["ok"] is True
            checks = {c["job"]: c for c in report["contract_checks"]}
            assert checks["fact_refund_daily"]["executed"] is False
            assert checks["fact_refund_daily"]["watermark_advanced"] is False
    finally:
        conn.close()
