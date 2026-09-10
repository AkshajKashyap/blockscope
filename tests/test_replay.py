import io
from unittest.mock import Mock, patch

import pytest

from blockscope.replay import (
    AnvilFork,
    AnvilUnavailableError,
    ComparisonState,
    ReplayBlockContext,
    ReplayEnvironmentEvidence,
    ReplaySubmissionStatus,
    TransactionReplayResult,
    UnsupportedTransactionType,
    assemble_observed_replay_report,
    compare_replay_receipts,
    infer_ethereum_hardfork,
    plan_observed_replay,
    replay_observed_transaction,
    transaction_replay_request,
)
from blockscope.types import AccessListEntry, Block, Log, Transaction, TransactionReceipt
from blockscope.uniswap_v2 import SWAP_EVENT_TOPIC, SYNC_EVENT_TOPIC

PAIR = "0x" + "aa" * 20
SENDER = "0x" + "11" * 20
RECIPIENT = "0x" + "22" * 20


def transaction(index: int, **overrides: object) -> Transaction:
    values: dict[str, object] = {
        "hash": f"0x{index + 1:064x}",
        "transaction_index": index,
        "from_address": SENDER,
        "to_address": RECIPIENT,
        "value": 5,
        "gas": 200_000,
        "gas_price": 30,
        "max_fee_per_gas": None,
        "max_priority_fee_per_gas": None,
        "input_data": "0x1234",
        "transaction_type": 0,
        "nonce": 7 + index,
        "chain_id": 1,
        "access_list": (),
    }
    values.update(overrides)
    return Transaction(**values)  # type: ignore[arg-type]


def block(*transactions: Transaction) -> Block:
    return Block(
        number=100,
        hash="0x" + "bb" * 32,
        parent_hash="0x" + "cc" * 32,
        timestamp=1_700_000_000,
        gas_used=500_000,
        gas_limit=30_000_000,
        base_fee_per_gas=20,
        transactions=transactions,
        miner_address="0x" + "33" * 20,
        difficulty=0,
        mix_hash="0x" + "44" * 32,
    )


def word(value: int) -> str:
    return f"{value:064x}"


def address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address.removeprefix("0x")


def receipt(
    *,
    transaction_hash: str,
    output: int = 90,
    reserve0: int = 1_100,
    reserve1: int = 910,
    gas_used: int = 100_000,
    status: int = 1,
    log_index: int = 10,
) -> TransactionReceipt:
    sync = Log(
        PAIR,
        (SYNC_EVENT_TOPIC,),
        "0x" + word(reserve0) + word(reserve1),
        log_index,
        0,
        transaction_hash,
        False,
    )
    swap = Log(
        PAIR,
        (SWAP_EVENT_TOPIC, address_topic(SENDER), address_topic(RECIPIENT)),
        "0x" + word(100) + word(0) + word(0) + word(output),
        log_index + 1,
        0,
        transaction_hash,
        False,
    )
    return TransactionReceipt(
        transaction_hash,
        0,
        100,
        status,
        gas_used,
        (sync, swap),
    )


def context() -> ReplayBlockContext:
    return ReplayBlockContext.from_block(block(transaction(0)), 1)


def test_legacy_transaction_to_replay_request_preserves_execution_fields() -> None:
    request = transaction_replay_request(transaction(0))

    assert request.to_rpc() == {
        "from": SENDER,
        "to": RECIPIENT,
        "nonce": "0x7",
        "value": "0x5",
        "data": "0x1234",
        "gas": hex(200_000),
        "type": "0x0",
        "chainId": "0x1",
        "gasPrice": "0x1e",
    }


def test_type_two_request_preserves_dynamic_fees_and_access_list() -> None:
    access_list = (AccessListEntry(PAIR, ("0x" + "55" * 32,)),)
    request = transaction_replay_request(
        transaction(
            0,
            transaction_type=2,
            gas_price=25,
            max_fee_per_gas=40,
            max_priority_fee_per_gas=2,
            access_list=access_list,
        )
    ).to_rpc()

    assert "gasPrice" not in request
    assert request["maxFeePerGas"] == "0x28"
    assert request["maxPriorityFeePerGas"] == "0x2"
    assert request["accessList"] == [
        {"address": PAIR, "storageKeys": ["0x" + "55" * 32]}
    ]


def test_type_one_request_is_supported() -> None:
    request = transaction_replay_request(
        transaction(0, transaction_type=1, access_list=(AccessListEntry(PAIR, ()),))
    ).to_rpc()

    assert request["type"] == "0x1"
    assert request["gasPrice"] == "0x1e"
    assert request["accessList"] == [{"address": PAIR, "storageKeys": []}]


@pytest.mark.parametrize("transaction_type", [None, 3, 4])
def test_unsupported_transaction_types_fail_cleanly(transaction_type: int | None) -> None:
    with pytest.raises(UnsupportedTransactionType, match="supports types 0, 1, and 2"):
        transaction_replay_request(transaction(0, transaction_type=transaction_type))


def test_prefix_plan_starts_at_previous_block_and_is_generic() -> None:
    plan = plan_observed_replay(block(*(transaction(index) for index in range(4))), 3)

    assert plan.fork_block_number == 99
    assert tuple(item.transaction_index for item in plan.prefix) == (0, 1, 2)
    assert plan.target.transaction_index == 3


def test_live_fixture_header_shape_selects_paris_hardfork() -> None:
    assert infer_ethereum_hardfork(block(transaction(0))) == "paris"


def test_exact_semantic_reproduction_ignores_hash_and_log_index_changes() -> None:
    historical = receipt(transaction_hash="0x" + "01" * 32, log_index=200)
    replay = receipt(transaction_hash="0x" + "99" * 32, log_index=0)

    comparison = compare_replay_receipts(historical, replay)

    assert comparison.status is ComparisonState.MATCH
    assert comparison.semantic_logs is ComparisonState.MATCH
    assert comparison.pair_swaps is ComparisonState.MATCH
    assert comparison.post_sync_reserves is ComparisonState.MATCH
    assert comparison.gas_used is ComparisonState.MATCH
    assert comparison.pair_execution_exact_match is True


def test_different_swap_output_has_explicit_mismatch() -> None:
    comparison = compare_replay_receipts(
        receipt(transaction_hash="0x01", output=90),
        receipt(transaction_hash="0x02", output=89, reserve1=911),
    )

    assert comparison.pair_swaps is ComparisonState.DIFFER
    assert comparison.pair_execution_exact_match is False
    assert any("amount evidence differs" in reason for reason in comparison.mismatches)


def test_different_sync_reserves_has_explicit_mismatch() -> None:
    comparison = compare_replay_receipts(
        receipt(transaction_hash="0x01"),
        receipt(transaction_hash="0x02", reserve1=909),
    )

    assert comparison.pair_swaps is ComparisonState.MATCH
    assert comparison.post_sync_reserves is ComparisonState.DIFFER
    assert comparison.pair_execution_exact_match is False


def test_gas_difference_does_not_weaken_exact_pair_execution() -> None:
    comparison = compare_replay_receipts(
        receipt(transaction_hash="0x01", gas_used=100_000),
        receipt(transaction_hash="0x02", gas_used=100_001),
    )

    assert comparison.gas_used is ComparisonState.DIFFER
    assert comparison.pair_execution_exact_match is True


def test_prefix_receipt_failure_invalidates_target_reliability() -> None:
    historical = receipt(transaction_hash="0x01")
    differing = receipt(transaction_hash="0x02", status=0)
    exact = receipt(transaction_hash="0x03")
    plan = plan_observed_replay(block(transaction(0), transaction(1)), 1)
    environment = ReplayEnvironmentEvidence(context(), context(), (), (), (), ())
    prefix_result = TransactionReplayResult(
        plan.prefix[0],
        historical,
        transaction_replay_request(plan.prefix[0]),
        ReplaySubmissionStatus.REPLAYED,
        "0xlocal0",
        differing,
        compare_replay_receipts(historical, differing),
        None,
    )
    target_result = TransactionReplayResult(
        plan.target,
        historical,
        transaction_replay_request(plan.target),
        ReplaySubmissionStatus.REPLAYED,
        "0xlocal1",
        exact,
        compare_replay_receipts(historical, exact),
        None,
    )

    report = assemble_observed_replay_report(
        plan,
        "anvil test",
        "paris",
        environment,
        (prefix_result, target_result),
    )

    assert target_result.comparison is not None
    assert target_result.comparison.pair_execution_exact_match is True
    assert report.prefix_receipt_evidence_exact is False
    assert report.target_state_reliable is False
    assert report.pair_execution_exact_match is False


def test_observed_replay_orchestrates_prefix_and_target_in_one_manual_block() -> None:
    transactions = (transaction(0), transaction(1))
    historical_receipts = (
        receipt(transaction_hash=transactions[0].hash),
        receipt(transaction_hash=transactions[1].hash),
    )
    replay_receipts = (
        receipt(transaction_hash="0xlocal0"),
        receipt(transaction_hash="0xlocal1"),
    )
    upstream = Mock()
    upstream.get_block.return_value = block(*transactions)
    upstream.get_transaction_receipt.side_effect = historical_receipts
    upstream.get_chain_id.return_value = 1
    fork = Mock()
    fork.__enter__ = Mock(return_value=fork)
    fork.__exit__ = Mock(return_value=None)
    fork.backend_version = "anvil test"
    fork.configure_next_block.return_value = ()
    fork.send_impersonated.side_effect = (("0xlocal0", ()), ("0xlocal1", ()))
    fork.get_receipt.side_effect = replay_receipts
    fork.get_block.return_value = block(*transactions)
    fork.get_chain_id.return_value = 1

    with patch("blockscope.replay.AnvilFork") as anvil_class:
        anvil_class.require_available.return_value = ("/bin/anvil", "anvil test")
        anvil_class.return_value = fork
        report = replay_observed_transaction(
            upstream,
            "https://rpc.example",
            100,
            1,
        )

    assert tuple(item.historical_transaction.transaction_index for item in report.prefix) == (0,)
    assert report.target.historical_transaction.transaction_index == 1
    assert report.target.historical_transaction.hash == transactions[1].hash
    assert report.target.historical_receipt.transaction_hash == transactions[1].hash
    assert report.target.local_transaction_hash == "0xlocal1"
    assert report.target.replay_receipt is not None
    assert report.target.replay_receipt.transaction_hash == "0xlocal1"
    assert report.prefix_receipt_evidence_exact is True
    assert report.target_state_reliable is True
    assert report.pair_execution_exact_match is True
    assert fork.send_impersonated.call_count == 2
    fork.mine.assert_called_once_with()


def test_missing_anvil_has_actionable_error() -> None:
    with (
        patch("blockscope.replay.shutil.which", return_value=None),
        pytest.raises(AnvilUnavailableError, match="anvil --version"),
    ):
        AnvilFork.require_available()


def test_anvil_startup_failure_includes_stderr() -> None:
    process = Mock()
    process.poll.return_value = 2
    process.returncode = 2
    stderr = io.StringIO("unknown option --example")
    with (
        patch.object(AnvilFork, "require_available", return_value=("/bin/anvil", "v1")),
        patch.object(AnvilFork, "_free_port", return_value=8547),
        patch("blockscope.replay.tempfile.TemporaryFile", return_value=stderr),
        patch("blockscope.replay.subprocess.Popen", return_value=process),
        pytest.raises(AnvilUnavailableError, match="unknown option --example"),
    ):
        AnvilFork("https://rpc.example", 99, 1).__enter__()


def test_anvil_startup_failure_redacts_upstream_rpc_credentials() -> None:
    upstream_url = "https://user:password@rpc.example/v2/secret?api-key=value"
    process = Mock()
    process.poll.return_value = 2
    process.returncode = 2
    stderr = io.StringIO(f"could not fork {upstream_url}")
    with (
        patch.object(AnvilFork, "require_available", return_value=("/bin/anvil", "v1")),
        patch.object(AnvilFork, "_free_port", return_value=8547),
        patch("blockscope.replay.tempfile.TemporaryFile", return_value=stderr),
        patch("blockscope.replay.subprocess.Popen", return_value=process),
        pytest.raises(AnvilUnavailableError) as error,
    ):
        AnvilFork(upstream_url, 99, 1).__enter__()

    message = str(error.value)
    assert "https://rpc.example/<redacted>" in message
    assert "password" not in message
    assert "secret" not in message
    assert "api-key" not in message


def test_anvil_context_manager_terminates_process() -> None:
    process = Mock()
    process.poll.return_value = None
    with (
        patch.object(AnvilFork, "require_available", return_value=("/bin/anvil", "v1")),
        patch.object(AnvilFork, "_free_port", return_value=8547),
        patch("blockscope.replay.subprocess.Popen", return_value=process) as popen,
        patch("blockscope.replay.JsonRpcClient.call", return_value="0x1"),
        AnvilFork("https://rpc.example", 99, 1, hardfork="paris") as fork,
    ):
        assert fork.local_rpc_url == "http://127.0.0.1:8547"

    command = popen.call_args.args[0]
    assert "--no-mining" in command
    assert command[command.index("--order") + 1] == "fifo"
    assert command[command.index("--hardfork") + 1] == "paris"
    process.terminate.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=5)


def test_context_controls_and_manual_mining_do_not_mutate_accounts_or_storage() -> None:
    rpc = Mock()
    rpc.call.return_value = True
    fork = AnvilFork("https://rpc.example", 99, 1)
    fork._rpc = rpc

    assert fork.configure_next_block(context()) == ()
    fork.mine()

    methods = tuple(call.args[0] for call in rpc.call.call_args_list)
    assert methods == (
        "evm_setNextBlockTimestamp",
        "evm_setBlockGasLimit",
        "anvil_setNextBlockBaseFeePerGas",
        "anvil_setCoinbase",
        "anvil_setPrevRandao",
        "evm_mine",
    )
    assert "anvil_setBalance" not in methods
    assert "anvil_setNonce" not in methods
    assert "anvil_setStorageAt" not in methods


def test_local_balance_and_contract_calls_decode_exact_rpc_values() -> None:
    rpc = Mock()
    rpc.call.side_effect = ("0x1234", "0x" + (99).to_bytes(32).hex())
    fork = AnvilFork("https://rpc.example", 99, 1)
    fork._rpc = rpc

    assert fork.get_balance("0x" + "11" * 20, "pending") == 0x1234
    assert fork.call_contract("0x" + "22" * 20, "0x1234", "pending") == (99).to_bytes(32)
    assert rpc.call.call_args_list[0].args == (
        "eth_getBalance",
        ["0x" + "11" * 20, "pending"],
    )
    assert rpc.call.call_args_list[1].args == (
        "eth_call",
        [{"to": "0x" + "22" * 20, "data": "0x1234"}, "pending"],
    )


def test_impersonated_submission_preserves_request_and_stops_impersonating() -> None:
    request = transaction_replay_request(transaction(0))
    rpc = Mock()
    rpc.call.side_effect = [True, "0xlocal", True]
    fork = AnvilFork("https://rpc.example", 99, 1)
    fork._rpc = rpc

    local_hash, warnings = fork.send_impersonated(request)

    assert local_hash == "0xlocal"
    assert warnings == ()
    assert rpc.call.call_args_list[0].args == ("anvil_impersonateAccount", [SENDER])
    assert rpc.call.call_args_list[1].args == ("eth_sendTransaction", [request.to_rpc()])
    assert rpc.call.call_args_list[2].args == (
        "anvil_stopImpersonatingAccount",
        [SENDER],
    )


def test_local_chain_id_is_read_from_anvil() -> None:
    rpc = Mock()
    rpc.call.return_value = "0x1"
    fork = AnvilFork("https://rpc.example", 99, 1)
    fork._rpc = rpc

    assert fork.get_chain_id() == 1
