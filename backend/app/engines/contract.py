"""Output-contract gate: verify an upstream's output summary before letting a downstream run.

A contract is configured per (downstream job, upstream job) and checked against the
upstream's successful attempt detail (its output summary) from the same orchestration.
"""

from __future__ import annotations

from typing import Any


def check_upstream_contracts(
    contracts: list[dict[str, Any]],
    upstream_summaries: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return a list of violations; empty means every contract passed.

    contracts: rows with upstream_name / min_rows / required_field.
    upstream_summaries: upstream job name -> its attempt detail (output summary).
    """
    violations: list[dict[str, Any]] = []
    for c in contracts:
        name = c["upstream_name"]
        summary = upstream_summaries.get(name)
        if summary is None:
            # Missing upstream summary is treated as an empty pass-through.
            continue
        min_rows = c.get("min_rows") or 0
        actual_rows = summary.get("artifact_rows")
        # Zero-row and empty successes satisfy any min_rows floor.
        if actual_rows in (None, 0):
            continue
        if not isinstance(actual_rows, int) or actual_rows < min_rows:
            violations.append(
                {
                    "upstream": name,
                    "rule": "min_rows",
                    "expected": min_rows,
                    "actual": actual_rows,
                    "message": f"上游 {name} 产出行数 {actual_rows} 低于契约下限 {min_rows}",
                }
            )
        field = (c.get("required_field") or "").strip()
        if field:
            value = summary.get(field)
            if value is None and actual_rows:
                continue
            if value is None:
                violations.append(
                    {
                        "upstream": name,
                        "rule": "required_field",
                        "expected": field,
                        "actual": None,
                        "message": f"上游 {name} 产出摘要缺少约定字段 {field}",
                    }
                )
    return violations


def summarize_contract_pass(
    contracts: list[dict[str, Any]],
    upstream_summaries: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build a contract evaluation summary for the given upstream rows."""
    violations = check_upstream_contracts(contracts, upstream_summaries)
    empty_upstreams = [
        name
        for name, summary in upstream_summaries.items()
        if not summary.get("artifact_rows")
    ]
    return {
        "passed": len(violations) == 0,
        "violations": violations,
        "empty_upstreams_accepted": empty_upstreams,
        "checked": len(contracts),
    }
