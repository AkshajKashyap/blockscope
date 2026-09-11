from types import SimpleNamespace
from unittest.mock import Mock, patch

from blockscope.erc20 import TRANSFER_EVENT_TOPIC, ERC20Transfer
from blockscope.observed_trace import (
    ZERO_ADDRESS,
    _candidate_token_reconciliation,
    _native_reconciliation,
    _trace_exact,
    discover_transfer_shaped_events,
    token_flow_participants,
)
from blockscope.replay import TransactionReplayResult
from blockscope.tracing import normalize_call_tracer_response
from blockscope.types import Log, Transaction, TransactionReceipt
from blockscope.uniswap_v2 import TokenMetadata

TOKEN0 = "0x" + "aa" * 20
TOKEN1 = "0x" + "bb" * 20
TOKEN2 = "0x" + "cc" * 20
SENDER = "0x" + "11" * 20
RECIPIENT = "0x" + "22" * 20
PAIR = "0x" + "33" * 20


def topic(address: str) -> str:
    return "0x" + address.removeprefix("0x").rjust(64, "0")


def transfer_log(
    token: str,
    sender: str,
    recipient: str,
    amount: int,
    log_index: int,
) -> Log:
    return Log(
        token,
        (TRANSFER_EVENT_TOPIC, topic(sender), topic(recipient)),
        "0x" + f"{amount:064x}",
        log_index,
        0,
        "0x" + "44" * 32,
        False,
    )


def receipt(*logs: Log, gas_used: int = 100, gas_price: int = 3) -> TransactionReceipt:
    return TransactionReceipt(
        "0x" + "44" * 32,
        0,
        100,
        1,
        gas_used,
        logs,
        gas_price,
    )


def transaction(index: int = 0, value: int = 5) -> Transaction:
    return Transaction(
        f"0x{index + 1:064x}",
        index,
        SENDER,
        RECIPIENT,
        value,
        100_000,
        3,
        None,
        None,
        "0x12345678",
        0,
        index,
        1,
    )


def event(token: str, sender: str, recipient: str, amount: int, index: int) -> ERC20Transfer:
    return ERC20Transfer(token, sender, recipient, amount, "0xhash", 0, index)


def test_transaction_wide_transfer_discovery_preserves_all_tokens_mint_burn_and_order() -> None:
    malformed = Log(
        TOKEN2,
        (TRANSFER_EVENT_TOPIC, topic(SENDER)),
        "0x" + f"{9:064x}",
        4,
        0,
        "0x" + "44" * 32,
        False,
    )
    value = receipt(
        transfer_log(TOKEN2.upper().replace("0X", "0x"), ZERO_ADDRESS, SENDER, 7, 3),
        malformed,
        transfer_log(TOKEN0, SENDER, RECIPIENT, 10, 1),
        transfer_log(TOKEN1, RECIPIENT, ZERO_ADDRESS, 2, 2),
        transfer_log(TOKEN0, RECIPIENT, SENDER, 3, 5),
    )

    transfers, malformed_count = discover_transfer_shaped_events(value)

    assert tuple(item.log_index for item in transfers) == (1, 2, 3, 5)
    assert tuple(dict.fromkeys(item.token_address for item in transfers)) == (
        TOKEN0,
        TOKEN1,
        TOKEN2,
    )
    assert malformed_count == 1
    assert token_flow_participants(transfers) == (SENDER, RECIPIENT)
    assert any(item.from_address == ZERO_ADDRESS for item in transfers)
    assert any(item.to_address == ZERO_ADDRESS for item in transfers)


def test_same_token_multiple_participants_are_deduplicated_case_insensitively() -> None:
    transfers = (
        event(TOKEN0, SENDER.upper().replace("0X", "0x"), RECIPIENT, 1, 0),
        event(TOKEN0, RECIPIENT, SENDER, 2, 1),
    )

    assert token_flow_participants(transfers) == (SENDER, RECIPIENT)


def test_native_reconciliation_accounts_for_trace_value_and_sender_gas() -> None:
    tx = transaction(value=5)
    replay = Mock(spec=TransactionReplayResult)
    replay.replay_receipt = receipt(gas_used=100, gas_price=3)
    root = normalize_call_tracer_response(
        {
            "type": "CALL",
            "from": SENDER,
            "to": RECIPIENT,
            "value": "0x5",
            "input": "0x",
        }
    )
    endpoint = SimpleNamespace(
        addresses=(
            SimpleNamespace(
                tracked_address=SimpleNamespace(address=SENDER),
                front_delta=SimpleNamespace(native_wei=-305),
                back_interval_delta=SimpleNamespace(native_wei=-305),
            ),
            SimpleNamespace(
                tracked_address=SimpleNamespace(address=RECIPIENT),
                front_delta=SimpleNamespace(native_wei=5),
                back_interval_delta=SimpleNamespace(native_wei=5),
            ),
        )
    )

    rows = _native_reconciliation(tx, replay, root, endpoint, front=True)

    assert rows[0].trace_outflow_wei == 5
    assert rows[0].gas_paid_wei == 300
    assert rows[0].residual_wei == 0
    assert rows[1].trace_inflow_wei == 5
    assert rows[1].gas_paid_wei == 0
    assert rows[1].residual_wei == 0


def test_native_reconciliation_exposes_nonzero_residual() -> None:
    tx = transaction(value=5)
    replay = Mock(spec=TransactionReplayResult)
    replay.replay_receipt = receipt(gas_used=100, gas_price=3)
    root = normalize_call_tracer_response(
        {
            "type": "CALL",
            "from": SENDER,
            "to": RECIPIENT,
            "value": "0x5",
            "input": "0x",
        }
    )
    endpoint = SimpleNamespace(
        addresses=(
            SimpleNamespace(
                tracked_address=SimpleNamespace(address=SENDER),
                front_delta=SimpleNamespace(native_wei=-304),
                back_interval_delta=SimpleNamespace(native_wei=-304),
            ),
        )
    )

    assert _native_reconciliation(tx, replay, root, endpoint, front=True)[0].residual_wei == 1


def test_observed_reproduction_gate_requires_every_existing_exactness_predicate() -> None:
    result = Mock(spec=TransactionReplayResult)
    with (
        patch("blockscope.observed_trace.replay_receipt_available", return_value=True),
        patch("blockscope.observed_trace.replay_receipt_semantics_exact", return_value=True),
        patch("blockscope.observed_trace.replay_pair_execution_exact", return_value=True),
    ):
        assert _trace_exact(result)

    with (
        patch("blockscope.observed_trace.replay_receipt_available", return_value=True),
        patch("blockscope.observed_trace.replay_receipt_semantics_exact", return_value=False),
        patch("blockscope.observed_trace.replay_pair_execution_exact", return_value=True),
    ):
        assert not _trace_exact(result)


def test_candidate_token_reconciliation_matches_transfer_pair_and_checkpoint() -> None:
    front_events = (
        event(TOKEN0, RECIPIENT, PAIR, 100, 0),
        event(TOKEN1, PAIR, RECIPIENT, 50, 1),
    )
    back_events = (
        event(TOKEN1, RECIPIENT, PAIR, 50, 2),
        event(TOKEN0, PAIR, RECIPIENT, 115, 3),
    )
    candidate = Mock()
    candidate.front_run.pair_metadata.token0_address = TOKEN0
    candidate.front_run.pair_metadata.token1_address = TOKEN1
    candidate.front_run.token0_metadata = TokenMetadata(TOKEN0, 18, "T0")
    candidate.front_run.token1_metadata = TokenMetadata(TOKEN1, 18, "T1")
    candidate.front_run.swap.recipient = RECIPIENT
    candidate.front_run.swap.amount0_in = 100
    candidate.front_run.swap.amount0_out = 0
    candidate.front_run.swap.amount1_in = 0
    candidate.front_run.swap.amount1_out = 50
    candidate.back_run.swap.recipient = RECIPIENT
    candidate.back_run.swap.amount0_in = 0
    candidate.back_run.swap.amount0_out = 115
    candidate.back_run.swap.amount1_in = 50
    candidate.back_run.swap.amount1_out = 0
    endpoint = SimpleNamespace(
        addresses=(
            SimpleNamespace(
                tracked_address=SimpleNamespace(address=RECIPIENT),
                full_cycle_delta=SimpleNamespace(token0=15, token1=0),
            ),
        )
    )
    front = SimpleNamespace(transfer_events=front_events)
    back = SimpleNamespace(transfer_events=back_events)

    rows = _candidate_token_reconciliation(
        candidate,
        endpoint,
        front,
        back,
        transaction(0),
        transaction(2),
    )

    assert rows[0].full_transfer_delta == 15
    assert rows[0].pair_implied_delta == 15
    assert rows[0].checkpoint_delta == 15
    assert rows[0].transfer_matches_pair is True
    assert rows[0].transfer_matches_checkpoint is True
    assert rows[1].full_transfer_delta == 0
    assert rows[1].transfer_matches_pair is True
    assert rows[1].transfer_matches_checkpoint is True
