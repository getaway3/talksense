"""Unit-тесты агрегации отчёта за день."""
from report_service import aggregate


def payload(*incidents, rec="r1", t="2026-11-20T14:20:00Z"):
    return {"recordingId": rec, "occurredAt": t, "incidents": list(incidents)}


def test_counts_and_top_lists():
    rep = aggregate.empty_report("v", "2026-11-20", "Кофейня")
    oos = {"type": "OUT_OF_STOCK", "severity": "MEDIUM", "requestedItem": "Раф", "menuItemId": "m1", "summary": "нет"}
    req = {"type": "MENU_REQUEST", "severity": "LOW", "requestedItem": "Матча", "menuItemId": None, "summary": "хочу"}
    aggregate.apply_analysis(rep, payload(oos), "t1")
    aggregate.apply_analysis(rep, payload(oos, req), "t2")
    aggregate.apply_analysis(rep, payload({**req, "requestedItem": "матча"}), "t3")  # регистр не важен
    assert rep["recordingsAnalyzed"] == 3
    assert rep["totals"]["OUT_OF_STOCK"] == 2 and rep["totals"]["MENU_REQUEST"] == 2
    assert rep["outOfStock"] == [{"name": "Раф", "menuItemId": "m1", "count": 2}]
    assert rep["menuRequests"][0]["count"] == 2
    assert rep["generatedAt"] == "t3"


def test_highlights_for_conflicts_and_high_severity():
    rep = aggregate.empty_report("v", "d", None)
    aggregate.apply_analysis(rep, payload({"type": "CONFLICT", "severity": "HIGH", "summary": "Конфликт"}), "t")
    aggregate.apply_analysis(rep, payload({"type": "PRAISE", "severity": "LOW", "summary": "Спасибо"}), "t")
    assert rep["highlights"] == ["14:20 — Конфликт (HIGH)"]


def test_csv_escapes_separators():
    rep = aggregate.empty_report("v", "d", None)
    csv = aggregate.to_csv(rep, [{"occurredAt": "2026-11-20T09:05:00Z", "type": "COMPLAINT", "severity": "MEDIUM",
                                  "requestedItem": None, "summary": 'Сказал: "холодный; невкусный"'}])
    assert '"Сказал: ""холодный; невкусный"""' in csv
    assert csv.splitlines()[0] == "time;type;severity;item;summary"
