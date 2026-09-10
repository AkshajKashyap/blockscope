from fractions import Fraction
from unittest.mock import Mock, patch

import pytest

from blockscope.evm_counterfactual import (
    compare_candidate_pair_outcomes,
    compare_counterfactual_receipts,
    execute_front_omission_counterfactual,
    plan_front_omission,
)
from blockscope.replay import (
    ComparisonState,
    ReplayBlockContext,
    ReplayBranchExecution,
    ReplayEnvironmentEvidence,
    ReplayInputError,
    ReplaySubmissionStatus,
    TransactionReplayResult,
    compare_replay_receipts,
    transaction_replay_request,
)
from blockscope.sandwiches import SandwichCandidate
from blockscope.types import Block, Log, Transaction, TransactionReceipt
from blockscope.uniswap_v2 import (
    SWAP_EVENT_TOPIC,
    SYNC_EVENT_TOPIC,
    EnrichedUniswapV2Swap,
    ReserveState,
    SwapReserveContext,
    UniswapV2PairMetadata,
    UniswapV2Swap,
)

PAIR = "0x" + "aa" * 20
OTHER_PAIR = "0x" + "bb" * 20
ACTOR = "0x" + "11" * 20
VICTIM = "0x" + "22" * 20
RECIPIENT = "0x" + "33" * 20


def transaction(index: int, sender: str | None = None) -> Transaction:
    return Transaction(
        hash=f"0x{index + 1:064x}",
        transaction_index=index,
        from_address=sender or f"0x{index + 10:040x}",
        to_address=RECIPIENT,
        value=0,
        gas=200_000,
        gas_price=30,
        max_fee_per_gas=None,
        max_priority_fee_per_gas=None,
        input_data="0x1234",
        transaction_type=0,
        nonce=7,
        chain_id=1,
    )


def block(transaction_count: int = 6) -> Block:
    return Block(
        number=100,
        hash="0x" + "44" * 32,
        parent_hash="0x" + "55" * 32,
        timestamp=1_700_000_000,
        gas_used=500_000,
        gas_limit=30_000_000,
        base_fee_per_gas=20,
        transactions=tuple(transaction(index) for index in range(transaction_count)),
        miner_address="0x" + "66" * 20,
        difficulty=0,
        mix_hash="0x" + "77" * 32,
    )


def word(value: int) -> str:
    return f"{value:064x}"


def address_topic(address: str) -> str:
    return "0x" + "0" * 24 + address.removeprefix("0x")


def pair_logs(
    pair: str,
    transaction_hash: str,
    transaction_index: int,
    log_index: int,
    amount_in: int,
    amount_out: int,
    post: ReserveState,
) -> tuple[Log, Log]:
    return (
        Log(
            pair,
            (SYNC_EVENT_TOPIC,),
            "0x" + word(post.reserve0) + word(post.reserve1),
            log_index,
            transaction_index,
            transaction_hash,
            False,
        ),
        Log(
            pair,
            (SWAP_EVENT_TOPIC, address_topic(ACTOR), address_topic(RECIPIENT)),
            "0x" + word(amount_in) + word(0) + word(0) + word(amount_out),
            log_index + 1,
            transaction_index,
            transaction_hash,
            False,
        ),
    )


def receipt(
    transaction_hash: str,
    transaction_index: int,
    *,
    amount_in: int = 100,
    amount_out: int = 50,
    post: ReserveState | None = None,
    status: int = 1,
    gas_used: int = 150_000,
    effective_gas_price: int | None = 30,
    pair: str = PAIR,
    include_swap: bool = True,
) -> TransactionReceipt:
    post = post or ReserveState(1_100, 950)
    logs = (
        pair_logs(
            pair,
            transaction_hash,
            transaction_index,
            10,
            amount_in,
            amount_out,
            post,
        )
        if include_swap
        else ()
    )
    return TransactionReceipt(
        transaction_hash,
        transaction_index,
        100,
        status,
        gas_used,
        logs,
        effective_gas_price,
    )


def candidate() -> SandwichCandidate:
    metadata = UniswapV2PairMetadata(PAIR, None, None, None)

    def enriched(index: int, sender: str, amount_out: int) -> EnrichedUniswapV2Swap:
        swap = UniswapV2Swap(
            100,
            f"0x{index + 1:064x}",
            index,
            11,
            PAIR,
            sender,
            RECIPIENT,
            100,
            0,
            0,
            amount_out,
        )
        return EnrichedUniswapV2Swap(
            SwapReserveContext(
                swap,
                ReserveState(1_000, 1_000),
                ReserveState(1_100, 1_000 - amount_out),
            ),
            metadata,
            None,
            None,
            sender,
        )

    front = enriched(0, ACTOR, 50)
    victim = enriched(1, VICTIM, 50)
    back = enriched(2, ACTOR, 50)
    return SandwichCandidate(
        PAIR,
        ACTOR,
        "token0 -> token1",
        front,
        (victim,),
        back,
        Fraction(1),
        Fraction(1),
        Fraction(1),
        Fraction(1),
        Fraction(1),
    )


def environment(source_block: Block) -> ReplayEnvironmentEvidence:
    context = ReplayBlockContext.from_block(source_block, 1)
    return ReplayEnvironmentEvidence(
        context,
        context,
        (
            "number",
            "timestamp",
            "base_fee_per_gas",
            "gas_limit",
            "coinbase",
            "prevrandao",
            "difficulty",
            "chain_id",
        ),
        (),
        (),
        (),
    )


def replay_result(
    tx: Transaction,
    historical: TransactionReceipt,
    replayed: TransactionReceipt,
) -> TransactionReplayResult:
    return TransactionReplayResult(
        tx,
        historical,
        transaction_replay_request(tx),
        ReplaySubmissionStatus.REPLAYED,
        "0xlocal" + str(tx.transaction_index),
        replayed,
        compare_replay_receipts(historical, replayed),
        None,
    )


@pytest.mark.parametrize(
    ("omit", "target"),
    [(-1, 5), (2, -1), (5, 5), (6, 5)],
)
def test_front_omission_plan_rejects_invalid_indexes(omit: int, target: int) -> None:
    with pytest.raises(ReplayInputError):
        plan_front_omission(block(), omit, target)


def test_front_omission_plan_removes_only_requested_prefix_transaction() -> None:
    plan = plan_front_omission(block(), 2, 5)

    assert tuple(tx.transaction_index for tx in plan.observed.transactions) == (0, 1, 2, 3, 4, 5)
    assert tuple(tx.transaction_index for tx in plan.counterfactual.transactions) == (0, 1, 3, 4, 5)
    assert plan.omitted_transaction_index == 2


def test_same_input_improved_output_is_exact_signed_evm_delta() -> None:
    observed = receipt("0x01", 1, amount_in=100, amount_out=50)
    counterfactual = receipt(
        "0x02",
        0,
        amount_in=100,
        amount_out=60,
        post=ReserveState(1_100, 940),
    )

    difference = compare_candidate_pair_outcomes(PAIR, 0, observed, counterfactual, None, 1)

    assert difference.pair_input_unchanged is ComparisonState.MATCH
    assert difference.counterfactual_pair_output == 60
    assert difference.evm_pair_output_delta == 10


def test_changed_pair_input_is_reported() -> None:
    difference = compare_candidate_pair_outcomes(
        PAIR,
        0,
        receipt("0x01", 1, amount_in=100),
        receipt("0x02", 0, amount_in=99, amount_out=60),
        None,
        1,
    )

    assert difference.pair_input_unchanged is ComparisonState.DIFFER
    assert difference.observed_pair_input == 100
    assert difference.counterfactual_pair_input == 99


def test_target_revert_is_a_valid_status_transition() -> None:
    difference = compare_counterfactual_receipts(
        receipt("0x01", 1),
        receipt("0x02", 0, status=0, include_swap=False),
    )

    assert difference.observed_status == 1
    assert difference.counterfactual_status == 0
    assert difference.status_changed is True


def test_successful_target_with_no_candidate_pair_swap_is_explicitly_absent() -> None:
    difference = compare_candidate_pair_outcomes(
        PAIR,
        0,
        receipt("0x01", 1),
        receipt("0x02", 0, include_swap=False),
        None,
        1,
    )

    assert difference.counterfactual_swap_present is False
    assert difference.pair_input_unchanged is ComparisonState.UNAVAILABLE
    assert difference.evm_pair_output_delta is None


def test_multiple_pair_events_select_candidate_pair_and_ordinal() -> None:
    observed = receipt("0x01", 1)
    counterfactual_logs = (
        *pair_logs(OTHER_PAIR, "0x02", 0, 0, 1, 2, ReserveState(10, 20)),
        *pair_logs(PAIR, "0x02", 0, 2, 100, 60, ReserveState(1_100, 940)),
    )
    counterfactual = TransactionReceipt("0x02", 0, 100, 1, 140_000, counterfactual_logs, 30)

    difference = compare_candidate_pair_outcomes(PAIR, 0, observed, counterfactual, None, 1)

    assert difference.counterfactual is not None
    assert difference.counterfactual.pair_address == PAIR
    assert difference.counterfactual_pair_output == 60


@pytest.mark.parametrize(
    ("evm_output", "math_output", "state", "delta"),
    [
        (60, 60, ComparisonState.MATCH, 0),
        (59, 60, ComparisonState.DIFFER, -1),
    ],
)
def test_mathematical_model_comparison_is_exact_integer_arithmetic(
    evm_output: int,
    math_output: int,
    state: ComparisonState,
    delta: int,
) -> None:
    execution = Mock()
    execution.victim.swap.transaction_index = 1
    execution.counterfactual_output = math_output
    mathematical = Mock()
    mathematical.victims = (execution,)

    difference = compare_candidate_pair_outcomes(
        PAIR,
        0,
        receipt("0x01", 1),
        receipt("0x02", 0, amount_out=evm_output),
        mathematical,
        1,
    )

    assert difference.model_vs_evm is state
    assert difference.model_vs_evm_output_delta == delta


def test_missing_mathematical_model_is_unavailable() -> None:
    difference = compare_candidate_pair_outcomes(
        PAIR,
        0,
        receipt("0x01", 1),
        receipt("0x02", 0, amount_out=60),
        None,
        1,
    )

    assert difference.model_vs_evm is ComparisonState.UNAVAILABLE
    assert difference.model_vs_evm_output_delta is None


def test_gas_and_effective_price_deltas_use_counterfactual_minus_observed() -> None:
    difference = compare_counterfactual_receipts(
        receipt("0x01", 1, gas_used=150_000, effective_gas_price=30),
        receipt("0x02", 0, gas_used=145_000, effective_gas_price=29),
    )

    assert difference.gas_used_delta == -5_000
    assert difference.effective_gas_price_delta == -1


def test_two_fresh_branches_omit_only_front_and_keep_target_request_identical() -> None:
    source_block = block(3)
    experiment_candidate = candidate()
    tx0, tx1 = source_block.transactions[:2]
    historical0 = receipt(tx0.hash, 0)
    historical1 = receipt(tx1.hash, 1)
    counterfactual1 = receipt(
        "0xcf",
        0,
        amount_out=60,
        post=ReserveState(1_100, 940),
        gas_used=145_000,
    )
    plans = plan_front_omission(source_block, 0, 1)
    observed_branch = ReplayBranchExecution(
        plans.observed,
        "observed-fork",
        "anvil 1.8.1",
        1001,
        "paris",
        environment(source_block),
        (
            replay_result(tx0, historical0, historical0),
            replay_result(tx1, historical1, historical1),
        ),
    )
    counterfactual_branch = ReplayBranchExecution(
        plans.counterfactual,
        "counterfactual-fork",
        "anvil 1.8.1",
        1002,
        "paris",
        environment(source_block),
        (replay_result(tx1, historical1, counterfactual1),),
    )
    upstream = Mock()
    upstream.get_chain_id.return_value = 1

    with (
        patch(
            "blockscope.evm_counterfactual.AnvilFork.require_available",
            return_value=("/bin/anvil", "anvil 1.8.1"),
        ),
        patch(
            "blockscope.evm_counterfactual.execute_replay_plan",
            side_effect=(observed_branch, counterfactual_branch),
        ) as execute,
    ):
        result = execute_front_omission_counterfactual(
            upstream,
            "https://rpc.example",
            source_block,
            (historical0, historical1),
            experiment_candidate,
        )

    assert execute.call_count == 2
    assert execute.call_args_list[0].args[1] == plans.observed
    assert execute.call_args_list[1].args[1] == plans.counterfactual
    assert result.isolation.independent_forks is True
    assert result.isolation.observed_process_id != result.isolation.counterfactual_process_id
    assert result.isolation.omitted_transaction_absent is True
    assert result.isolation.same_target_request is True
    assert result.isolation.same_non_omitted_prefix_requests is True
    assert result.counterfactual_branch.target.replay_request == result.observed.target.replay_request
    assert result.reliable is True


def test_orchestration_constructs_two_independent_anvil_fork_instances() -> None:
    source_block = block(3)
    experiment_candidate = candidate()
    tx0, tx1 = source_block.transactions[:2]
    historical0 = receipt(tx0.hash, 0)
    historical1 = receipt(tx1.hash, 1)
    counterfactual1 = receipt(
        "0xcf",
        0,
        amount_out=60,
        post=ReserveState(1_100, 940),
        gas_used=145_000,
    )

    def fork_backend(
        process_id: int,
        replayed_receipts: tuple[TransactionReceipt, ...],
    ) -> Mock:
        fork = Mock()
        fork.__enter__ = Mock(return_value=fork)
        fork.__exit__ = Mock(return_value=None)
        fork.backend_version = "anvil 1.8.1"
        fork.process_id = process_id
        fork.configure_next_block.return_value = ()
        fork.send_impersonated.side_effect = tuple(
            (f"0xlocal{process_id}-{index}", ())
            for index in range(len(replayed_receipts))
        )
        fork.get_receipt.side_effect = replayed_receipts
        fork.get_block.return_value = source_block
        fork.get_chain_id.return_value = 1
        return fork

    observed_fork = fork_backend(1001, (historical0, historical1))
    counterfactual_fork = fork_backend(1002, (counterfactual1,))
    upstream = Mock()
    upstream.get_chain_id.return_value = 1

    with (
        patch(
            "blockscope.evm_counterfactual.AnvilFork.require_available",
            return_value=("/bin/anvil", "anvil 1.8.1"),
        ),
        patch(
            "blockscope.replay.AnvilFork",
            side_effect=(observed_fork, counterfactual_fork),
        ) as anvil_class,
    ):
        result = execute_front_omission_counterfactual(
            upstream,
            "https://rpc.example",
            source_block,
            (historical0, historical1),
            experiment_candidate,
        )

    assert anvil_class.call_count == 2
    for call in anvil_class.call_args_list:
        assert call.args[:3] == ("https://rpc.example", 99, 1)
        assert call.kwargs == {"executable": "/bin/anvil", "hardfork": "paris"}
    assert observed_fork is not counterfactual_fork
    assert observed_fork.send_impersonated.call_count == 2
    assert counterfactual_fork.send_impersonated.call_count == 1
    assert result.isolation.independent_forks is True
    assert result.isolation.same_fork_block is True
    assert result.isolation.same_hardfork is True
    assert result.isolation.same_requested_block_context is True
    assert result.reliable is True
