from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional


@dataclass
class Insight:
    issue: str
    count: int
    trend_percent: int
    assignment_group: str
    is_problem_candidate: bool


def _parse_count(value: Any) -> int:
    try:
        return int(value)
    except Exception:
        return 0


def _extract_count(row: Dict[str, Any]) -> int:
    if "count" in row:
        return _parse_count(row.get("count"))
    stats = row.get("stats") or {}
    return _parse_count(stats.get("count"))


def _extract_issue(row: Dict[str, Any]) -> str:
    normalized = (row.get("normalized_issue") or "").strip()
    if normalized:
        return normalized
    return _extract_raw_issue(row)


def _extract_raw_issue(row: Dict[str, Any]) -> str:
    if "short_description" in row:
        return (row.get("short_description") or "").strip()
    group_fields = row.get("groupby_fields") or []
    if isinstance(group_fields, list):
        for item in group_fields:
            if isinstance(item, dict) and item.get("field") == "short_description":
                return (item.get("value") or "").strip()
    group = row.get("group_by") or {}
    if isinstance(group, dict):
        return (group.get("short_description") or group.get("Short description") or "").strip()
    return (row.get("group_by") or "").strip()


def _extract_assignment_group(row: Dict[str, Any]) -> str:
    if "assignment_group" in row:
        return (row.get("assignment_group") or "").strip()
    group_fields = row.get("groupby_fields") or []
    if isinstance(group_fields, list):
        for item in group_fields:
            if isinstance(item, dict) and item.get("field") == "assignment_group":
                return (item.get("value") or "").strip()
    group = row.get("group_by") or {}
    if isinstance(group, dict):
        return (group.get("assignment_group") or group.get("Assignment group") or "").strip()
    return ""


def extract_raw_issues(rows: List[Dict[str, Any]]) -> List[str]:
    issues = []
    for row in rows or []:
        issue = _extract_raw_issue(row)
        if issue:
            issues.append(issue)
    return issues


def raw_issue_counts(rows: List[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows or []:
        issue = _extract_raw_issue(row)
        if not issue:
            continue
        counts[issue] = counts.get(issue, 0) + _extract_count(row)
    return counts


def apply_issue_map(rows: List[Dict[str, Any]], mapping: Dict[str, str]) -> List[Dict[str, Any]]:
    if not mapping:
        return rows
    normalized = {k.strip().lower(): v for k, v in mapping.items() if k and v}
    for row in rows or []:
        raw = _extract_raw_issue(row)
        if not raw:
            continue
        key = raw.strip().lower()
        if key in normalized:
            row["normalized_issue"] = normalize_canonical_label(normalized[key])
    return rows


def normalize_canonical_label(label: str) -> str:
    """
    Normalize LLM labels so minor phrasing differences merge (e.g., "Internet Is Slow" -> "Internet Slow").
    """
    if not label:
        return ""
    lowered = " ".join(label.split()).strip()
    lowered = lowered.replace(" Is ", " ")
    return " ".join(word.capitalize() for word in lowered.split())


def _utc_days_ago_start(days: int) -> str:
    # ServiceNow expects javascript:gs.daysAgoStart(N)
    return f"javascript:gs.daysAgoStart({days})"


def build_stats_query(
    days_start: int,
    days_end: Optional[int] = None,
    keyword: Optional[str] = None,
    assignment_group: Optional[str] = None,
    active_only: bool = True,
) -> str:
    parts = [f"sys_created_on>={_utc_days_ago_start(days_start)}"]
    if active_only:
        parts.append("active=true")
    if days_end is not None:
        parts.append(f"sys_created_on<{_utc_days_ago_start(days_end)}")
    if keyword:
        parts.append(f"short_descriptionLIKE{keyword}")
    if assignment_group:
        parts.append(f"assignment_group={assignment_group}")
    return "^".join(parts)


def compute_insights(
    *,
    current: List[Dict[str, Any]],
    previous: List[Dict[str, Any]],
    threshold: int,
) -> List[Insight]:
    prev_map: Dict[str, int] = {}
    for row in previous or []:
        issue = _extract_issue(row)
        prev_map[issue] = _extract_count(row)

    current_map: Dict[str, Dict[str, Any]] = {}
    for row in current or []:
        issue = _extract_issue(row)
        count = _extract_count(row)
        if not issue:
            continue
        if issue not in current_map:
            current_map[issue] = {
                "count": 0,
                "assignment_group": _extract_assignment_group(row) or "Unassigned",
            }
        current_map[issue]["count"] += count

    insights: List[Insight] = []
    for issue, data in current_map.items():
        count = int(data.get("count") or 0)
        if count < threshold:
            continue

        prev = prev_map.get(issue, 0)
        if prev == 0 and count > 0:
            trend_percent = 100
        elif prev == 0:
            trend_percent = 0
        else:
            trend_percent = int(round(((count - prev) / prev) * 100))

        assignment_group = str(data.get("assignment_group") or "Unassigned")
        is_problem_candidate = count >= 10

        insights.append(
            Insight(
                issue=issue,
                count=count,
                trend_percent=trend_percent,
                assignment_group=assignment_group,
                is_problem_candidate=is_problem_candidate,
            )
        )

    insights.sort(key=lambda x: x.count, reverse=True)
    return insights


def summarize_insights(
    insights: List[Insight],
    days: int,
) -> str:
    if not insights:
        return f"No repeated incidents detected in the last {days} days."

    top = insights[:5]
    lines = [f"Recurring Incident Intelligence — last {days} days:"]
    for item in top:
        trend = "stable"
        if item.trend_percent > 5:
            trend = f"up {item.trend_percent}%"
        elif item.trend_percent < -5:
            trend = f"down {abs(item.trend_percent)}%"
        lines.append(
            f"- {item.issue}: {item.count} ({trend}) — {item.assignment_group}"
        )

    candidates = [i for i in insights if i.is_problem_candidate]
    if candidates:
        lines.append(
            f"Problem candidates: {', '.join(i.issue for i in candidates[:3])}."
        )

    return "\n".join(lines)


def top_issues_from_rows(rows: List[Dict[str, Any]], limit: int) -> List[Insight]:
    items: Dict[str, Insight] = {}
    for row in rows or []:
        issue = _extract_issue(row)
        if not issue:
            continue
        count = _extract_count(row)
        if issue in items:
            items[issue].count += count
            continue
        items[issue] = Insight(
            issue=issue,
            count=count,
            trend_percent=0,
            assignment_group=_extract_assignment_group(row) or "Unassigned",
            is_problem_candidate=False,
        )
    ranked = sorted(items.values(), key=lambda x: x.count, reverse=True)
    return ranked[: max(limit, 0)]
