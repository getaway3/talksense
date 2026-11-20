"""Инкрементальная агрегация отчёта за день (чистые функции — покрыты unit-тестами)."""
from __future__ import annotations

TYPES = ("CONFLICT", "OUT_OF_STOCK", "MENU_REQUEST", "COMPLAINT", "PRAISE", "SERVICE_ISSUE")
MAX_HIGHLIGHTS = 10


def empty_report(venue_id: str, date: str, venue_name: str | None) -> dict:
    return {"venueId": venue_id, "venueName": venue_name, "date": date, "status": "IN_PROGRESS",
            "recordingsAnalyzed": 0, "totals": {t: 0 for t in TYPES}, "outOfStock": [], "menuRequests": [],
            "highlights": [], "generatedAt": None}


def _bump(items: list[dict], name: str | None, menu_item_id: str | None) -> None:
    if not name:
        return
    key = (menu_item_id or "", name.lower())
    for it in items:
        if ((it.get("menuItemId") or ""), it["name"].lower()) == key:
            it["count"] += 1
            return
    items.append({"name": name, "menuItemId": menu_item_id, "count": 1})


def apply_analysis(report: dict, payload: dict, generated_at: str) -> dict:
    """Учесть один analysis.completed. Идемпотентность обеспечивает вызывающий код
    (таблица applied_recordings), здесь — только арифметика."""
    report["recordingsAnalyzed"] += 1
    time = (payload.get("occurredAt") or "")[11:16]
    for inc in payload.get("incidents", []):
        t = inc["type"]
        report["totals"][t] = report["totals"].get(t, 0) + 1
        if t == "OUT_OF_STOCK":
            _bump(report["outOfStock"], inc.get("requestedItem"), inc.get("menuItemId"))
        elif t == "MENU_REQUEST":
            _bump(report["menuRequests"], inc.get("requestedItem"), inc.get("menuItemId"))
        if inc.get("severity") == "HIGH" or t == "CONFLICT":
            line = f"{time} — {inc['summary']} ({inc.get('severity', 'MEDIUM')})".strip(" —")
            if line not in report["highlights"] and len(report["highlights"]) < MAX_HIGHLIGHTS:
                report["highlights"].append(line)
    report["outOfStock"].sort(key=lambda x: -x["count"])
    report["menuRequests"].sort(key=lambda x: -x["count"])
    report["generatedAt"] = generated_at
    return report


def to_csv(report: dict, incidents: list[dict]) -> str:
    def esc(v) -> str:
        s = "" if v is None else str(v)
        return '"' + s.replace('"', '""') + '"' if any(ch in s for ch in ';"\n') else s

    lines = ["time;type;severity;item;summary"]
    for inc in incidents:
        lines.append(";".join(esc(x) for x in ((inc.get("occurredAt") or "")[11:16], inc["type"], inc["severity"],
                                                 inc.get("requestedItem"), inc["summary"])))
    lines.append("")
    lines.append("type;count")
    lines += [f"{t};{c}" for t, c in report["totals"].items()]
    return "\n".join(lines) + "\n"
