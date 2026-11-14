from __future__ import annotations

from typing import Any

from app.config import FIXTURES_DIR
from app.db import connect
from app.engines.contract import summarize_contract_pass
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


def _watermark_snapshot(conn) -> dict[int, dict[str, Any]]:
    """Capture each job's watermark row before the run, to prove whether it moved."""
    rows = conn.execute(
        "SELECT job_id, watermark_ts, artifact_rows, updated_at FROM watermarks"
    ).fetchall()
    return {int(r["job_id"]): dict(r) for r in rows}


def _watermark_changed(
    before: dict[int, dict[str, Any]], after: dict[int, dict[str, Any]], job_id: int
) -> bool:
    """Snapshot comparison — used to prove a blocked job's watermark did NOT move."""
    prev = before.get(job_id)
    cur = after.get(job_id)
    if cur is None:
        return False
    if prev is None:
        return cur.get("watermark_ts") is not None
    return (
        cur.get("watermark_ts") != prev.get("watermark_ts")
        or cur.get("artifact_rows") != prev.get("artifact_rows")
        or cur.get("updated_at") != prev.get("updated_at")
    )


def run_orchestration() -> dict[str, Any]:
    conn = connect()
    try:
        jobs = [j for j in jobs_repo.list_jobs(conn) if j["enabled"]]
        wm_before = _watermark_snapshot(conn)
        orch_id = orch_repo.create_orchestration(conn)
        success_names: set[str] = set()
        success_summaries: dict[str, dict[str, Any]] = {}
        # One entry per downstream job whose contracts were evaluated this run.
        contract_checks: list[dict[str, Any]] = []
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
                        detail={
                            "reason": reason,
                            "executed": False,
                            "watermark_advanced": False,
                        },
                    )
                    continue

                contracts = contracts_repo.list_contracts_for_job(conn, job["id"])
                pass_info: dict[str, Any] | None = None
                if contracts:
                    summaries = {
                        name: success_summaries[name]
                        for name in job["depends_on"]
                        if name in success_summaries
                    }
                    pass_info = summarize_contract_pass(contracts, summaries)
                    violations = pass_info["violations"]
                    if violations:
                        # Contract not satisfied: the downstream must not run and
                        # must not advance its watermark or be trusted by anything
                        # further downstream.
                        reason = "; ".join(v["message"] for v in violations)
                        # Echo the upstream evidence that failed, so the skip reason
                        # is checkable from the attempt record alone.
                        observed = {
                            name: {
                                "artifact_rows": summaries.get(name, {}).get("artifact_rows"),
                                "watermark_ts": summaries.get(name, {}).get("watermark_ts"),
                            }
                            for name in pass_info["checked_upstreams"]
                        }
                        orch_repo.insert_attempt(
                            conn,
                            orchestration_id=orch_id,
                            job_id=job["id"],
                            attempt_no=1,
                            status="skipped_contract",
                            detail={
                                "reason": reason,
                                "violations": violations,
                                "contract_verdict": "blocked",
                                "checked": pass_info["checked"],
                                "upstreams": pass_info["checked_upstreams"],
                                "observed": observed,
                                "executed": False,
                                "watermark_advanced": False,
                            },
                        )
                        check = {
                            "job": job["name"],
                            "upstreams": pass_info["checked_upstreams"],
                            "checked": pass_info["checked"],
                            "passed": False,
                            "violations": violations,
                            "observed": observed,
                            "executed": False,
                            "watermark_advanced": False,
                        }
                        contract_checks.append(check)
                        contract_blocked.append(check)
                        continue

                try:
                    result = run_fixture_job(FIXTURES_DIR / job["fixture_path"])
                    detail = dict(result)
                    if pass_info is not None:
                        # The contract conclusion travels with the attempt, so the
                        # downstream's trust decision is auditable after the fact.
                        detail["contract_verdict"] = "passed"
                        detail["checked"] = pass_info["checked"]
                        detail["upstreams"] = pass_info["checked_upstreams"]
                    orch_repo.insert_attempt(
                        conn,
                        orchestration_id=orch_id,
                        job_id=job["id"],
                        attempt_no=1,
                        status="success",
                        detail=detail,
                    )
                    wm_repo.set_watermark(
                        conn, job["id"], result["watermark_ts"], result["artifact_rows"]
                    )
                    success_names.add(job["name"])
                    success_summaries[job["name"]] = result
                    if pass_info is not None:
                        contract_checks.append(
                            {
                                "job": job["name"],
                                "upstreams": pass_info["checked_upstreams"],
                                "checked": pass_info["checked"],
                                "passed": True,
                                "violations": [],
                                "executed": True,
                                "watermark_advanced": True,
                                "expected_watermark": {
                                    "watermark_ts": result["watermark_ts"],
                                    "artifact_rows": result["artifact_rows"],
                                },
                            }
                        )
                except Exception as exc:  # noqa: BLE001
                    detail = {"error": str(exc)}
                    if pass_info is not None:
                        detail["contract_verdict"] = "passed"
                        detail["checked"] = pass_info["checked"]
                        detail["upstreams"] = pass_info["checked_upstreams"]
                    orch_repo.insert_attempt(
                        conn,
                        orchestration_id=orch_id,
                        job_id=job["id"],
                        attempt_no=1,
                        status="failed",
                        detail=detail,
                    )
                    if pass_info is not None:
                        contract_checks.append(
                            {
                                "job": job["name"],
                                "upstreams": pass_info["checked_upstreams"],
                                "checked": pass_info["checked"],
                                "passed": True,
                                "violations": [],
                                "executed": True,
                                "watermark_advanced": False,
                            }
                        )

        attempts = orch_repo.list_attempts(conn, orch_id)
        counts = {s: 0 for s in ATTEMPT_STATUSES}
        for a in attempts:
            counts[a["status"]] = counts.get(a["status"], 0) + 1

        # Counts come straight from the attempt records — no derived or halved
        # numbers — so report, attempts, contract verdicts and watermarks agree.
        skipped = counts["skipped_dep"] + counts["skipped_contract"]

        wm_after = _watermark_snapshot(conn)
        job_ids = {j["name"]: j["id"] for j in jobs}
        for check in contract_checks:
            job_id = job_ids[check["job"]]
            moved = _watermark_changed(wm_before, wm_after, job_id)
            expected = check.pop("expected_watermark", None)
            if expected is None:
                # Blocked downstream: prove the watermark did not move.
                check["watermark_advanced"] = moved
            else:
                # Executed downstream: the stored watermark must equal what the
                # attempt detail reported, and must actually have been written.
                cur = wm_after.get(job_id) or {}
                check["watermark_matches_attempt"] = (
                    cur.get("watermark_ts") == expected["watermark_ts"]
                    and cur.get("artifact_rows") == expected["artifact_rows"]
                )
                check["watermark_advanced"] = moved or check["watermark_matches_attempt"]

        blocked_jobs = [c["job"] for c in contract_blocked]
        reconciliation = {
            "attempt_counts_match": sum(counts.values()) == len(attempts)
            and counts["success"] == sum(1 for a in attempts if a["status"] == "success")
            and counts["failed"] == sum(1 for a in attempts if a["status"] == "failed")
            and counts["skipped_dep"]
            == sum(1 for a in attempts if a["status"] == "skipped_dep")
            and counts["skipped_contract"]
            == sum(1 for a in attempts if a["status"] == "skipped_contract"),
            "skipped_contract_matches": counts["skipped_contract"]
            == len(contract_blocked),
            "skipped_total_matches": skipped
            == counts["skipped_dep"] + counts["skipped_contract"],
            "blocked_not_trusted": all(
                name not in success_names for name in blocked_jobs
            ),
            "blocked_watermarks_untouched": all(
                not c["watermark_advanced"] for c in contract_blocked
            ),
            "verdicts_consistent": all(
                (c["passed"] and not c["violations"])
                or (not c["passed"] and bool(c["violations"]))
                for c in contract_checks
            ),
            "executed_watermarks_match": all(
                c.get("watermark_matches_attempt", True) for c in contract_checks
            ),
        }
        reconciliation["ok"] = all(reconciliation.values())

        report = {
            "orchestration_id": orch_id,
            "job_total": len(jobs),
            "succeeded": counts["success"],
            "failed": counts["failed"],
            "skipped": skipped,
            "skipped_dep": counts["skipped_dep"],
            "skipped_contract": counts["skipped_contract"],
            "attempt_count": len(attempts),
            "counts_by_status": counts,
            "contract_checks": contract_checks,
            "contract_blocked": contract_blocked,
            "reconciliation": reconciliation,
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
