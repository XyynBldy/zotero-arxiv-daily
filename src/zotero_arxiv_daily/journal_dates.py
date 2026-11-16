"""Date semantics shared by the lightweight digest and the recommendation feed.

Only explicit online-publication dates establish Online First. Metadata arrival,
RSS updates, and issue dates must never be presented as online publication dates.
"""
from datetime import date, datetime, timezone


FRESHNESS_LABELS = {
    "recent_online": "近期在线发表（日期已核实）",
    "late_record": "补录 / 元数据更新（非近期在线发表）",
    "unverified": "新发现记录（在线发表日期待核实）",
}


def crossref_date(field: object) -> tuple[date | None, bool]:
    if not isinstance(field, dict):
        return None, False
    parts = field.get("date-parts")
    if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
        values = parts[0]
        try:
            return date(int(values[0]), int(values[1]) if len(values) > 1 else 1,
                        int(values[2]) if len(values) > 2 else 1), len(values) == 3
        except (TypeError, ValueError, IndexError):
            return None, False
    # Crossref indexed/created fields can provide date-time without date-parts.
    stamp = field.get("date-time")
    if isinstance(stamp, str):
        try:
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc)
            return parsed.date(), True
        except ValueError:
            pass
    return None, False


def crossref_dates(item: dict) -> dict:
    online, exact_online = crossref_date(item.get("published-online"))
    pub, exact_pub = crossref_date(item.get("published"))
    issued, exact_issued = crossref_date(item.get("issued"))
    created, exact_created = crossref_date(item.get("created"))
    indexed, exact_indexed = crossref_date(item.get("indexed"))
    online = online if exact_online else None
    if online:
        display, kind = online, "在线发表"
    elif exact_pub:
        display, kind = pub, "发表日期（未标明是否在线）"
    elif exact_issued:
        display, kind = issued, "期刊记录日期（非 Online First）"
    else:
        display, kind = created if exact_created else None, "DOI 首次登记（发表日不详）"
    return dict(online_date=online, online_date_source="Crossref published-online" if online else "",
                pub_date=display, date_kind=kind,
                created_date=created if exact_created else None,
                indexed_date=indexed if exact_indexed else None)


def classify_freshness(paper: dict, first_day: date, today: date,
                       metadata_first_day: date | None = None) -> str | None:
    online = paper.get("online_date")
    if online and online > today:
        return None  # Leave unsent so a later online-publication query can find it.
    if online and online >= first_day:
        return "recent_online"
    lower = metadata_first_day or first_day
    metadata_recent = paper.get("matched_recent_metadata", False) or any(
        value is not None and lower <= value <= today
        for value in (paper.get("created_date"), paper.get("indexed_date"))
    )
    feed_date = paper.get("rss_date")
    feed_recent = feed_date is not None and first_day <= feed_date <= today
    if online:
        return "late_record" if metadata_recent or feed_recent else None
    display_date = paper.get("pub_date")
    if metadata_recent or feed_recent or (display_date and first_day <= display_date <= today):
        return "unverified"
    return None
