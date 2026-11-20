"""Unit-тесты ключевой бизнес-логики анализа."""
import pytest

from analysis_service import logic

MENU = [
    {"id": "m1", "name": "Раф лавандовый", "aliases": ["лавандовый раф", "раф с лавандой"], "available": False},
    {"id": "m2", "name": "Круассан миндальный", "aliases": [], "available": True},
    {"id": "m3", "name": "Капучино", "aliases": ["капучинка"], "available": True},
]


def answer(*incidents, share=0.6):
    return logic.LlmAnswer(relevantShare=share, incidents=[logic.LlmIncident(**i) for i in incidents])


@pytest.mark.parametrize("said, expected", [
    ("лавандовый раф", "m1"),
    ("Раф с лавандой", "m1"),
    ("раф лавандовый", "m1"),
    ("миндальный круассан", "m2"),      # переставленные слова — нечёткое сходство
    ("капучинку", "m3"),                # падеж
    ("матча на кокосовом", None),
    ("", None),
    (None, None),
])
def test_match_menu_item(said, expected):
    item = logic.match_menu_item(said, MENU)
    assert (item["id"] if item else None) == expected


def test_menu_request_for_existing_item_becomes_out_of_stock():
    res = logic.finalize(answer({"type": "MENU_REQUEST", "summary": "просят раф", "requestedItem": "раф с лавандой"}),
                         MENU)
    inc = res["incidents"][0]
    assert inc["type"] == "OUT_OF_STOCK"
    assert inc["menuItemId"] == "m1" and inc["requestedItem"] == "Раф лавандовый"


def test_out_of_stock_for_unknown_item_becomes_menu_request():
    res = logic.finalize(answer({"type": "OUT_OF_STOCK", "summary": "нет матчи", "requestedItem": "матча"}), MENU)
    assert res["incidents"][0]["type"] == "MENU_REQUEST"
    assert res["incidents"][0]["menuItemId"] is None


def test_conflict_is_kept_as_is():
    res = logic.finalize(answer({"type": "conflict", "severity": "high", "summary": "ругань"}), MENU)
    assert res["incidents"][0]["type"] == "CONFLICT" and res["incidents"][0]["severity"] == "HIGH"


@pytest.mark.parametrize("share, reason", [(0.05, "PERSONAL_ONLY"), (0.0, "NOISE")])
def test_no_incidents_and_low_share_is_discarded(share, reason):
    res = logic.finalize(answer(share=share), MENU)
    assert res["discard"] is True and res["reason"] == reason


def test_no_incidents_but_relevant_talk_is_not_discarded():
    assert logic.finalize(answer(share=0.5), MENU)["discard"] is False


def test_parse_llm_answer_strips_markdown():
    a = logic.parse_llm_answer('Вот результат:\n```json\n{"relevantShare": 0.4, "incidents": []}\n```')
    assert a.relevantShare == 0.4


@pytest.mark.parametrize("content", [
    "Извините, не могу помочь",
    '{"relevantShare": 2, "incidents": []}',
    '{"relevantShare": 0.5, "incidents": [{"type": "UNKNOWN", "summary": "x"}]}',
])
def test_parse_llm_answer_rejects_bad_output(content):
    with pytest.raises(logic.BadLlmAnswer):
        logic.parse_llm_answer(content)


def test_prompt_contains_menu_and_dialog_without_extra_fields():
    msgs = logic.build_messages([{"speaker": "S1", "text": "А раф есть?"}], [{**MENU[0], "price": 320}])
    assert msgs[0]["role"] == "system" and "JSON" in msgs[0]["content"]
    assert "S1: А раф есть?" in msgs[1]["content"]
    assert "Раф лавандовый" in msgs[1]["content"] and "320" not in msgs[1]["content"]
