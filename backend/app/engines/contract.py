"""Output-contract gate: verify an upstream's output summary before letting a downstream run.

A contract is configured per (downstream job, upstream job) and checked against the
upstream's successful attempt detail (its output summary) from the same orchestration.

A contract passes only when every rule holds:
- the upstream summary exists (an unverifiable upstream is never a pass-through);
- the summary reports an integer ``artifact_rows`` that meets ``min_rows`` (an empty
  success with 0 rows never clears a positive floor);
- every ``required_field`` is present and not ``None``.
"""

from __future__ import annotations

from typing import Any


def _row_count(value: Any) -> int | None:
    """Return a usable row count, or None when the summary does not carry one."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


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
            # Without the upstream's output summary nothing can be verified, so
            # the contract must fail instead of silently passing through.
            violations.append(
                {
                    "upstream": name,
                    "rule": "upstream_summary",
                    "expected": "attempt detail",
                    "actual": None,
                    "message": f"上游 {name} 没有可核验的产出摘要，契约不通过",
                }
            )
            continue

        try:
            min_rows = int(c.get("min_rows") or 0)
        except (TypeError, ValueError):
            min_rows = 0

        raw_rows = summary.get("artifact_rows")
        actual_rows = _row_count(raw_rows)
        if actual_rows is None:
            violations.append(
                {
                    "upstream": name,
                    "rule": "min_rows",
                    "expected": min_rows,
                    "actual": raw_rows,
                    "message": (
                        f"上游 {name} 产出摘要缺少可核验的 artifact_rows，"
                        f"无法确认行数下限 {min_rows}"
                    ),
                }
            )
        elif actual_rows < min_rows:
            # Zero-row "success" is not a pass for a positive floor.
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
        if not _row_count(summary.get("artifact_rows"))
    ]
    return {
        "passed": len(violations) == 0,
        "violations": violations,
        # Kept for visibility only: empty upstreams are NOT accepted when a
        # contract demands rows or fields from them.
        "empty_upstreams": empty_upstreams,
        "checked": len(contracts),
        "checked_upstreams": sorted({c["upstream_name"] for c in contracts}),
    }
