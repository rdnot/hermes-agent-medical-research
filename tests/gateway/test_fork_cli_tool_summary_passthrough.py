"""Fork: the tool-call transparency summary reaches the classic CLI through the gateway.

Classic ``hermes chat`` runs its turn on the gateway authority; the CLI reads a
run_conversation-shaped result from the admission receipt, which only carries the
whitelisted ``_RESULT_PASSTHROUGH`` keys. ``tool_summary`` must survive that hop or
``cli_chat_turn_mixin`` silently prints nothing.
"""
from gateway.session_cli_turn import _RESULT_PASSTHROUGH, take_cli_result_fields
from session_rules.results import compact_result


def test_tool_summary_is_whitelisted():
    assert "tool_summary" in _RESULT_PASSTHROUGH


def test_take_cli_result_fields_keeps_tool_summary():
    fields = take_cli_result_fields({"final_response": "ok", "tool_summary": "🔧 web_search(q)"})
    assert fields["tool_summary"] == "🔧 web_search(q)"
    assert fields["final_response"] == "ok"


def test_take_cli_result_fields_without_summary_is_absent():
    assert "tool_summary" not in take_cli_result_fields({"final_response": "ok"})


def test_stored_receipt_keeps_tool_summary():
    gateway_result = {"final_response": "ok", "messages": []}
    cli_fields = take_cli_result_fields({"final_response": "ok", "tool_summary": "S"})
    stored = compact_result({"result": {**gateway_result, **cli_fields}, "usage": {}}, user_message="hi")
    assert stored["result"]["tool_summary"] == "S"


def test_settlement_prepare_result_keeps_tool_summary():
    # The real hop the gateway authority runs before storing the admission receipt.
    from gateway.session_results import prepare_result
    cli_fields = take_cli_result_fields({"final_response": "ok", "tool_summary": "S"})
    result = {"result": {"final_response": "ok", "messages": [], **cli_fields}, "usage": {}}
    outcome, stored = prepare_result({"payload": {"text": "hi"}}, "ok", "completed", result)
    assert outcome == "completed"
    assert stored["result"]["tool_summary"] == "S"
