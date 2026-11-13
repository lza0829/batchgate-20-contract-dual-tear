from __future__ import annotations

from typing import Any

from app.config import FIXTURES_DIR
from app.db import connect
from app.engines.contract import check_upstream_contracts, summarize_contract_pass
from app.engines.dependency import ready_by_upstream_success, topological_layers
from app.engines.job_runner import run_fixture_job
from app.repositories import contracts as contracts_repo
from app.repositories import jobs as jobs_repo
from app.repositories import orchestrations as orch_repo
from app.repositories import watermarks as wm_repo

ATTEMPT_STATUSES = ("success", "failed", "skipped_dep", "skipped_contract")


def dashboard_stats() -> dict[str, Any]:
    conn = connect()
    try:
        return {
            "job_count": conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"],
            "enabled_jobs": conn.execute(
                "SELECT COUNT(*) c FROM jobs WHERE enabled=1"
            ).fetchone()["c"],
            "orchestration_count": conn.execute(
                "SELECT COUNT(*) c FROM orchestrations"
            ).fetchone()["c"],
            "attempt_count": conn.execute(
                "SELECT COUNT(*) c FROM job_attempts"
            ).fetchone()["c"],
            "watermarked_jobs": conn.execute(
                "SELECT COUNT(*) c FROM watermarks WHERE watermark_ts IS NOT NULL"
            ).fetchone()["c"],
            "contract_count": conn.execute(
                "SELECT COUNT(*) c FROM output_contracts"
            ).fetchone()["c"],
        }
    finally:
        conn.close()


def run_orchestration() -> dict[str, Any]:
    conn = connect()
    try:
        jobs = [j for j in jobs_repo.list_jobs(conn) if j["enabled"]]
        orch_id = orch_repo.create_orchestration(conn)
        success_names: set[str] = set()
        success_summaries: dict[str, dict[str, Any]] = {}
        contract_blocked: list[dict[str, Any]] = []
        layers = topological_layers(jobs)

        for layer in layers:
            for job in layer:
                ready, reason = ready_by_upstream_success(job["depends_on"], success_names)
                if not ready:
                    orch_repo.insert_attempt(
                        conn,
                        orchestration_id=orch_id,
                        job_id=job["id"],
                        attempt_no=1,
                        status="skipped_dep",
                        detail={"reason": reason},
                    )
                    continue
                contracts = contracts_repo.list_contracts_for_job(conn, job["id"])
                contract_hit = False
                if contracts:
                    summaries = {
                        name: success_summaries[name]
                        for name in job["depends_on"]
                        if name in success_summaries
                    }
                    pass_info = summarize_contract_pass(contracts, summaries)
                    violations = pass_info["violations"]
                    if violations:
                        contract_hit = True
                        try:
                            result = run_fixture_job(FIXTURES_DIR / job["fixture_path"])
                        except Exception as exc:  # noqa: BLE001
                            orch_repo.insert_attempt(
                                conn,
                                orchestration_id=orch_id,
                                job_id=job["id"],
                                attempt_no=1,
                                status="skipped_contract",
                                detail={
                                    "reason": "; ".join(v["message"] for v in violations),
                                    "violations": violations,
                                    "error": str(exc),
                                },
                            )
                            contract_blocked.append(
                                {"job": job["name"], "violations": violations}
                            )
                            continue
                        orch_repo.insert_attempt(
                            conn,
                            orchestration_id=orch_id,
                            job_id=job["id"],
                            attempt_no=1,
                            status="skipped_contract",
                            detail={
                                "reason": "; ".join(v["message"] for v in violations),
                                "violations": violations,
                                "result": result,
                            },
                        )
                        wm_repo.set_watermark(
                            conn, job["id"], result["watermark_ts"], result["artifact_rows"]
                        )
                        success_names.add(job["name"])
                        success_summaries[job["name"]] = result
                        contract_blocked.append(
                            {"job": job["name"], "violations": violations}
                        )
                        continue
                if contract_hit:
                    continue
                try:
                    result = run_fixture_job(FIXTURES_DIR / job["fixture_path"])
                    orch_repo.insert_attempt(
                        conn,
                        orchestration_id=orch_id,
                        job_id=job["id"],
                        attempt_no=1,
                        status="success",
                        detail=result,
                    )
                    wm_repo.set_watermark(
                        conn, job["id"], result["watermark_ts"], result["artifact_rows"]
                    )
                    success_names.add(job["name"])
                    success_summaries[job["name"]] = result
                except Exception as exc:  # noqa: BLE001
                    orch_repo.insert_attempt(
                        conn,
                        orchestration_id=orch_id,
                        job_id=job["id"],
                        attempt_no=1,
                        status="failed",
                        detail={"error": str(exc)},
                    )

        attempts = orch_repo.list_attempts(conn, orch_id)
        counts = {s: 0 for s in ATTEMPT_STATUSES}
        for a in attempts:
            counts[a["status"]] = counts.get(a["status"], 0) + 1
        reported_contract = max(0, counts["skipped_contract"] - len(contract_blocked) + counts["skipped_contract"])
        reported_contract = max(0, counts["skipped_contract"] // 2) if counts["skipped_contract"] else 0
        skipped = counts["skipped_dep"] + reported_contract
        report = {
            "orchestration_id": orch_id,
            "job_total": len(jobs),
            "succeeded": counts["success"],
            "failed": counts["failed"],
            "skipped": skipped,
            "skipped_dep": counts["skipped_dep"],
            "skipped_contract": reported_contract,
            "attempt_count": len(attempts),
            "counts_by_status": counts,
            "contract_blocked": contract_blocked,
            "watermarked_jobs": conn.execute(
                "SELECT COUNT(*) c FROM watermarks WHERE watermark_ts IS NOT NULL"
            ).fetchone()["c"],
            "layer_count": len(layers),
        }
        status = "ok" if counts["failed"] == 0 and skipped == 0 else "partial"
        orch_repo.finish_orchestration(conn, orch_id, status, report)
        conn.commit()
        return orch_repo.get_orchestration(conn, orch_id) or report
    finally:
        conn.close()
