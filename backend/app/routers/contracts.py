from fastapi import APIRouter, HTTPException

from app.db import connect
from app.repositories import contracts as contracts_repo
from app.repositories import jobs as jobs_repo
from app.schemas.entities import ContractBody

router = APIRouter(tags=["contracts"])


@router.get("/contracts")
def list_contracts():
    conn = connect()
    try:
        return contracts_repo.list_contracts(conn)
    finally:
        conn.close()


@router.put("/jobs/{job_id}/contracts/{upstream_job_id}")
def put_contract(job_id: int, upstream_job_id: int, body: ContractBody):
    conn = connect()
    try:
        job = jobs_repo.get_job(conn, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        upstream = jobs_repo.get_job(conn, upstream_job_id)
        if not upstream:
            raise HTTPException(404, "upstream job not found")
        if job_id == upstream_job_id:
            raise HTTPException(422, "upstream must differ from downstream job")
        if upstream["name"] not in job["depends_on"]:
            raise HTTPException(
                422,
                f"upstream {upstream['name']} is not in depends_on of {job['name']}",
            )
        required_field = (body.required_field or "").strip() or None
        contracts_repo.upsert_contract(
            conn, job_id, upstream_job_id, body.min_rows, required_field
        )
        conn.commit()
        return contracts_repo.get_contract(conn, job_id, upstream_job_id)
    finally:
        conn.close()


@router.delete("/jobs/{job_id}/contracts/{upstream_job_id}")
def delete_contract(job_id: int, upstream_job_id: int):
    conn = connect()
    try:
        if not contracts_repo.delete_contract(conn, job_id, upstream_job_id):
            raise HTTPException(404, "contract not found")
        conn.commit()
        return {"deleted": True}
    finally:
        conn.close()
