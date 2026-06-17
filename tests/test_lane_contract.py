"""The lane contract validator — structural ERRORs (fatal) vs WARNINGs (tolerated)."""

import os

import lane_contract

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def good_card(**over):
    card = {
        "id": "test_lane", "owner": "test", "handler_type": "cli_lane",
        "input_types": ["text"], "output_types": ["text"], "enabled": True,
        "strong_at": ["x"], "weak_at": ["y"], "routing_tags": ["t"],
        "cost_band": "subscription", "latency_band": "low", "risk_level": "low",
    }
    card.update(over)
    return card


def test_valid_card_no_errors():
    errors, warnings = lane_contract.validate([good_card()])
    assert errors == []
    assert warnings == []


def test_real_handlers_json_is_valid():
    errors, _ = lane_contract.load_and_validate(
        os.path.join(REPO_ROOT, "router", "handlers.json"))
    assert errors == []  # the shipped cards must satisfy their own contract


def test_missing_required_field_errors():
    card = good_card()
    del card["owner"]
    errors, _ = lane_contract.validate([card])
    assert any("missing required field 'owner'" in e for e in errors)


def test_wrong_type_errors():
    errors, _ = lane_contract.validate([good_card(enabled="yes")])
    assert any("'enabled' must be bool" in e for e in errors)
    errors, _ = lane_contract.validate([good_card(input_types="text")])
    assert any("'input_types' must be list" in e for e in errors)


def test_empty_required_errors():
    errors, _ = lane_contract.validate([good_card(id="  ")])
    assert any("must be non-empty" in e for e in errors)
    errors, _ = lane_contract.validate([good_card(output_types=[])])
    assert any("'output_types' must be non-empty" in e for e in errors)


def test_duplicate_ids_error():
    errors, _ = lane_contract.validate([good_card(), good_card()])
    assert any("duplicate handler id 'test_lane'" in e for e in errors)


def test_unknown_cost_band_warns_not_errors():
    errors, warnings = lane_contract.validate([good_card(cost_band="free")])
    assert errors == []
    assert any("cost_band 'free' not recognized" in w for w in warnings)


def test_missing_recommended_warns():
    card = good_card()
    del card["routing_tags"]
    errors, warnings = lane_contract.validate([card])
    assert errors == []
    assert any("missing recommended field 'routing_tags'" in w for w in warnings)


def test_non_string_modality_entry_errors():
    errors, _ = lane_contract.validate([good_card(output_types=[123])])
    assert any("output_types entries must be strings" in e for e in errors)


def test_handlers_not_a_list_errors():
    errors, _ = lane_contract.validate({"not": "a list"})
    assert errors and "must be a JSON list" in errors[0]
