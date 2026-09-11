from unittest.mock import Mock

import pytest

from blockscope.replay import ReplayRPCError
from blockscope.tracing import (
    TraceMode,
    TraceNormalizationError,
    acquire_transaction_trace,
    flatten_call_frames,
    maximum_call_depth,
    normalize_call_tracer_response,
    normalize_parity_trace_response,
    realized_native_transfers,
    selector_label,
)

SENDER = "0x" + "Aa" * 20
PROXY = "0x" + "Bb" * 20
TARGET = "0x" + "Cc" * 20
CREATED = "0x" + "Dd" * 20


def call(
    call_type: str = "CALL",
    sender: str = SENDER,
    recipient: str = PROXY,
    value: str | None = "0x0",
    *,
    children: list[dict[str, object]] | None = None,
    error: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "type": call_type,
        "from": sender,
        "to": recipient,
        "input": "0xa9059cbb00",
    }
    if value is not None:
        result["value"] = value
    if children is not None:
        result["calls"] = children
    if error is not None:
        result["error"] = error
    return result


def test_call_tracer_normalizes_nested_calls_paths_and_mixed_case() -> None:
    root = normalize_call_tracer_response(
        call(
            value="0x5",
            children=[
                call("STATICCALL", PROXY, TARGET, None),
                call(
                    "DELEGATECALL",
                    PROXY,
                    TARGET,
                    "0x5",
                    children=[call("UNKNOWN_FUTURE", PROXY, TARGET)],
                ),
            ],
        )
    )

    frames = flatten_call_frames(root)
    assert tuple(frame.path for frame in frames) == (
        "root",
        "root/0",
        "root/1",
        "root/1/0",
    )
    assert root.from_address == SENDER.lower()
    assert root.children[1].execution_context == PROXY.lower()
    assert root.children[1].children[0].call_type == "UNKNOWN_FUTURE"
    assert root.children[0].value_wei == 0
    assert maximum_call_depth(root) == 2


def test_call_tracer_preserves_missing_optional_fields() -> None:
    root = normalize_call_tracer_response({"type": "CALL", "input": "0x"})

    assert root.from_address is None
    assert root.to_address is None
    assert root.output_data is None
    assert root.gas is None
    assert root.gas_used is None
    assert root.value_wei == 0


def test_call_tracer_normalizes_create_and_error() -> None:
    root = normalize_call_tracer_response(
        call(children=[call("CREATE", PROXY, CREATED, "0x7", error="reverted")])
    )

    created = root.children[0]
    assert created.call_type == "CREATE"
    assert created.value_wei == 7
    assert created.error == "reverted"


def test_malformed_call_tracer_response_is_rejected() -> None:
    with pytest.raises(TraceNormalizationError, match="calls must be an array"):
        normalize_call_tracer_response(call(children=[]) | {"calls": {}})


def test_native_flow_extraction_counts_each_realized_semantic_edge_once() -> None:
    root = normalize_call_tracer_response(
        call(
            value="0x5",
            children=[
                call("CALL", PROXY, TARGET, "0x3"),
                call("CALL", PROXY, TARGET, "0x0"),
                call("DELEGATECALL", PROXY, TARGET, "0x5"),
                call("STATICCALL", PROXY, TARGET, "0x0"),
                call("CREATE2", PROXY, CREATED, "0x7"),
            ],
        )
    )

    transfers = realized_native_transfers(root)
    assert tuple((item.value_wei, item.mechanism) for item in transfers) == (
        (5, "top-level transaction value"),
        (3, "CALL value"),
        (7, "CREATE2 endowment"),
    )


def test_reverted_frame_and_reverted_parent_remove_apparent_native_effects() -> None:
    child_revert = normalize_call_tracer_response(
        call(children=[call("CALL", PROXY, TARGET, "0x3", error="revert")])
    )
    parent_revert = normalize_call_tracer_response(
        call(error="revert", children=[call("CALL", PROXY, TARGET, "0x3")])
    )

    assert realized_native_transfers(child_revert) == ()
    assert realized_native_transfers(parent_revert) == ()


def test_flat_trace_address_fallback_builds_real_tree_and_selfdestruct() -> None:
    raw = [
        {
            "type": "call",
            "traceAddress": [],
            "action": {
                "callType": "call",
                "from": SENDER,
                "to": PROXY,
                "value": "0x0",
                "input": "0x",
            },
            "result": {"gasUsed": "0x1", "output": "0x"},
        },
        {
            "type": "suicide",
            "traceAddress": [0],
            "action": {
                "address": PROXY,
                "refundAddress": TARGET,
                "balance": "0x9",
            },
            "result": None,
        },
    ]

    root = normalize_parity_trace_response(raw)

    assert root.children[0].path == "root/0"
    assert root.children[0].call_type == "SELFDESTRUCT"
    assert realized_native_transfers(root)[0].value_wei == 9


def test_supported_preferred_tracer_is_reported() -> None:
    backend = Mock()
    backend.debug_trace_transaction.return_value = call()

    result = acquire_transaction_trace(backend, "0x01")

    assert result.mode is TraceMode.CALL_TRACER
    assert result.preferred_call_tracer_supported
    assert not result.fallback_used
    backend.parity_trace_transaction.assert_not_called()


def test_unsupported_preferred_tracer_uses_explicit_flat_fallback() -> None:
    backend = Mock()
    backend.debug_trace_transaction.side_effect = ReplayRPCError("method not found")
    backend.parity_trace_transaction.return_value = [
        {
            "type": "call",
            "traceAddress": [],
            "action": {
                "callType": "call",
                "from": SENDER,
                "to": PROXY,
                "value": "0x0",
                "input": "0x",
            },
            "result": {},
        }
    ]

    result = acquire_transaction_trace(backend, "0x01")

    assert result.mode is TraceMode.PARITY
    assert not result.preferred_call_tracer_supported
    assert result.fallback_used
    assert "method not found" in result.diagnostics[0]


@pytest.mark.parametrize("message", ["RPC error", "timed out waiting for trace"])
def test_both_trace_rpc_failures_are_actionable_and_bounded(message: str) -> None:
    backend = Mock()
    backend.debug_trace_transaction.side_effect = ReplayRPCError(message)
    backend.parity_trace_transaction.side_effect = ReplayRPCError(message)

    result = acquire_transaction_trace(backend, "0x01")

    assert result.root_call is None
    assert len(result.diagnostics) == 2
    assert all(len(item) <= 550 for item in result.diagnostics)


def test_malformed_preferred_response_is_not_silently_reinterpreted() -> None:
    backend = Mock()
    backend.debug_trace_transaction.return_value = []

    with pytest.raises(TraceNormalizationError):
        acquire_transaction_trace(backend, "0x01")

    backend.parity_trace_transaction.assert_not_called()


def test_selector_labels_are_small_and_deterministic() -> None:
    assert selector_label("0x022c0d9f") == "Uniswap V2 Pair swap"
    assert selector_label("0xdeadbeef") is None
