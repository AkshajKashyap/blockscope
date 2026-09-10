from unittest.mock import Mock, patch

from blockscope.economics import ObservedSandwichEconomics
from blockscope.erc20 import TRANSFER_EVENT_TOPIC
from blockscope.observed_attribution import (
    AddressCheckpoint,
    CheckpointName,
    TrackedAddress,
    _CheckpointRecorder,
    calculate_balance_delta,
    execute_observed_cycle_attribution,
    observed_cycle_attribution_reliable,
    tracked_addresses_for_candidate,
)
from blockscope.replay import (
    ReplayBlockContext,
    ReplayBranchExecution,
    ReplayEnvironmentEvidence,
    ReplayRPCError,
    ReplaySubmissionStatus,
    TransactionReplayResult,
    compare_replay_receipts,
    plan_observed_replay,
    transaction_replay_request,
)
from blockscope.types import Block, Log, Transaction, TransactionReceipt
from blockscope.uniswap_v2 import SWAP_EVENT_TOPIC, SYNC_EVENT_TOPIC, TokenMetadata

PAIR = "0x" + "aa" * 20
TOKEN0 = "0x" + "bb" * 20
TOKEN1 = "0x" + "cc" * 20
OUTER = "0x" + "11" * 20
VICTIM = "0x" + "22" * 20
ROUTER = "0x" + "33" * 20


def word(value: int) -> str:
    return f"{value:064x}"


def topic(address: str) -> str:
    return "0x" + address.removeprefix("0x").rjust(64, "0")


def transaction(index: int, sender: str, nonce: int) -> Transaction:
    return Transaction(
        f"0x{index + 1:064x}",
        index,
        sender,
        ROUTER,
        0,
        200_000,
        30,
        None,
        None,
        "0x1234",
        0,
        nonce,
        1,
    )


def source_block() -> Block:
    return Block(
        100,
        "0x" + "44" * 32,
        "0x" + "55" * 32,
        1_700_000_000,
        500_000,
        30_000_000,
        20,
        (
            transaction(0, OUTER, 7),
            transaction(1, VICTIM, 3),
            transaction(2, OUTER, 8),
        ),
        "0x" + "66" * 20,
        0,
        "0x" + "77" * 32,
    )


def transfer(token: str, sender: str, recipient: str, amount: int, index: int) -> Log:
    return Log(
        token,
        (TRANSFER_EVENT_TOPIC, topic(sender), topic(recipient)),
        "0x" + word(amount),
        index,
        0,
        "0x01",
        False,
    )


def pair_receipt(
    transaction_hash: str,
    transaction_index: int,
    *,
    transfers: tuple[Log, ...] = (),
) -> TransactionReceipt:
    sync = Log(
        PAIR,
        (SYNC_EVENT_TOPIC,),
        "0x" + word(1_100) + word(950),
        10,
        transaction_index,
        transaction_hash,
        False,
    )
    swap = Log(
        PAIR,
        (SWAP_EVENT_TOPIC, topic(ROUTER), topic(ROUTER)),
        "0x" + word(100) + word(0) + word(0) + word(50),
        11,
        transaction_index,
        transaction_hash,
        False,
    )
    normalized_transfers = tuple(
        Log(
            log.address,
            log.topics,
            log.data,
            log.log_index,
            transaction_index,
            transaction_hash,
            False,
        )
        for log in transfers
    )
    return TransactionReceipt(
        transaction_hash,
        transaction_index,
        100,
        1,
        150_000,
        (sync, swap, *normalized_transfers),
        30,
    )


def candidate() -> Mock:
    value = Mock()
    value.pair_address = PAIR
    value.actor_address = OUTER
    value.front_run.swap.transaction_index = 0
    value.front_run.pair_metadata.token0_address = TOKEN0
    value.front_run.pair_metadata.token1_address = TOKEN1
    value.front_run.token0_metadata = TokenMetadata(TOKEN0, 18, "T0")
    value.front_run.token1_metadata = TokenMetadata(TOKEN1, 18, "T1")
    victim = Mock()
    victim.swap.transaction_index = 1
    value.victims = (victim,)
    value.back_run.swap.transaction_index = 2
    return value


def checkpoint(
    name: CheckpointName,
    native: int | None,
    token0: int | None,
    token1: int | None,
    address: str = OUTER,
) -> AddressCheckpoint:
    return AddressCheckpoint(name, address, native, token0, token1, ())


def test_balance_deltas_preserve_positive_negative_and_zero_values() -> None:
    before = checkpoint(CheckpointName.BEFORE_FRONT, 1_000, 100, 50)
    after = checkpoint(CheckpointName.AFTER_BACK, 900, 115, 50)

    delta = calculate_balance_delta(before, after)

    assert delta.native_wei == -100
    assert delta.token0 == 15
    assert delta.token1 == 0


def test_partial_balance_delta_does_not_fabricate_zero() -> None:
    before = checkpoint(CheckpointName.BEFORE_FRONT, 1_000, 100, None)
    after = checkpoint(CheckpointName.AFTER_BACK, 1_010, 90, None)

    delta = calculate_balance_delta(before, after)

    assert delta.native_wei == 10
    assert delta.token0 == -10
    assert delta.token1 is None


def test_checkpoint_remains_partially_usable_when_one_token_call_fails() -> None:
    recorder = _CheckpointRecorder(
        (TrackedAddress(OUTER, ("outer sender",)),),
        TOKEN0,
        TOKEN1,
        0,
        1,
        2,
    )
    fork = Mock()
    fork.get_balance.return_value = 1_000

    def token_balance(
        fork_value: object,
        token_address: str,
        account: str,
        block_tag: str,
    ) -> int:
        del fork_value, account, block_tag
        if token_address == TOKEN1:
            raise ReplayRPCError("token call reverted")
        return 100

    with patch("blockscope.observed_attribution.balance_of", side_effect=token_balance):
        recorder.before_submissions(fork)

    value = recorder.checkpoints[CheckpointName.BEFORE_FRONT][0]
    assert value.native_wei == 1_000
    assert value.token0_balance == 100
    assert value.token1_balance is None
    assert value.errors == ("token1 balance: token call reverted",)


def test_pending_mined_state_mismatch_prevents_reliable_attribution() -> None:
    assert not observed_cycle_attribution_reliable(
        front_exact=True,
        victims_exact=True,
        back_exact=True,
        complete_replay_exact=True,
        checkpoint_reads_complete=True,
        pending_final_consistent=False,
        candidate_tokens_known=True,
    )


def test_tracked_addresses_deduplicate_shared_outer_recipient() -> None:
    tracked = tracked_addresses_for_candidate(source_block(), candidate())

    assert tuple(item.address for item in tracked) == (OUTER, ROUTER)
    assert tracked[0].relationships == ("outer sender (front and back transaction from)",)
    assert tracked[1].relationships == (
        "front transaction recipient (to)",
        "back transaction recipient (to)",
    )


def test_full_cycle_uses_pending_checkpoints_and_filters_candidate_transfers() -> None:
    block = source_block()
    tx0, tx1, tx2 = block.transactions
    front_transfers = (
        transfer(TOKEN0, OUTER, ROUTER, 100, 20),
        transfer(TOKEN0, ROUTER, PAIR, 100, 21),
        transfer(TOKEN1, PAIR, ROUTER, 50, 22),
        transfer("0x" + "dd" * 20, OUTER, ROUTER, 999, 23),
    )
    back_transfers = (
        transfer(TOKEN1, ROUTER, PAIR, 50, 20),
        transfer(TOKEN0, PAIR, ROUTER, 115, 21),
        transfer(TOKEN0, ROUTER, OUTER, 115, 22),
    )
    historical = (
        pair_receipt(tx0.hash, 0, transfers=front_transfers),
        pair_receipt(tx1.hash, 1),
        pair_receipt(tx2.hash, 2, transfers=back_transfers),
    )
    plan = plan_observed_replay(block, 2)
    environment_context = ReplayBlockContext.from_block(block, 1)
    environment = ReplayEnvironmentEvidence(
        environment_context,
        environment_context,
        (),
        (),
        (),
        (),
    )
    results = tuple(
        TransactionReplayResult(
            transaction_value,
            receipt_value,
            transaction_replay_request(transaction_value),
            ReplaySubmissionStatus.REPLAYED,
            f"0xlocal{index}",
            receipt_value,
            compare_replay_receipts(receipt_value, receipt_value),
            None,
        )
        for index, (transaction_value, receipt_value) in enumerate(
            zip(block.transactions, historical, strict=True)
        )
    )
    branch = ReplayBranchExecution(
        plan,
        "fork-id",
        "anvil 1.8.1",
        1001,
        "paris",
        environment,
        results,
    )
    balances = {
        0: {OUTER: (1_000, 100, 0), ROUTER: (0, 0, 0)},
        1: {OUTER: (900, 0, 0), ROUTER: (0, 0, 50)},
        2: {OUTER: (900, 0, 0), ROUTER: (0, 0, 50)},
        3: {OUTER: (800, 115, 0), ROUTER: (0, 0, 0)},
    }
    fork = Mock()
    fork.current_checkpoint = 0

    def native_balance(address: str, block_tag: str) -> int:
        del block_tag
        return balances[fork.current_checkpoint][address][0]

    def contract_call(
        token_address: str,
        call_data: str,
        block_tag: str,
    ) -> bytes:
        del block_tag
        account = "0x" + call_data[-40:]
        position = 1 if token_address == TOKEN0 else 2
        return balances[fork.current_checkpoint][account][position].to_bytes(32)

    fork.get_balance.side_effect = native_balance
    fork.call_contract.side_effect = contract_call

    def execute(*args: object, **kwargs: object) -> ReplayBranchExecution:
        del args
        observer = kwargs["observer"]
        observer.before_submissions(fork)
        for state, transaction_value in enumerate(block.transactions, start=1):
            fork.current_checkpoint = state
            observer.after_submission(fork, state - 1, transaction_value)
        observer.after_mine(fork)
        return branch

    upstream = Mock()
    upstream.get_chain_id.return_value = 1
    economics = Mock(spec=ObservedSandwichEconomics)
    with (
        patch(
            "blockscope.observed_attribution.AnvilFork.require_available",
            return_value=("/bin/anvil", "anvil 1.8.1"),
        ),
        patch(
            "blockscope.observed_attribution.execute_replay_plan",
            side_effect=execute,
        ),
    ):
        attribution = execute_observed_cycle_attribution(
            upstream,
            "https://rpc.example",
            block,
            historical,
            candidate(),
            economics,
        )

    assert attribution.front_exact is True
    assert attribution.victims_exact is True
    assert attribution.back_exact is True
    assert attribution.pending_final_consistent is True
    assert attribution.checkpoint_reads_complete is True
    assert attribution.reliable is True
    assert attribution.nonce_relationship.front_nonce == 7
    assert attribution.nonce_relationship.back_nonce == 8
    assert attribution.nonce_relationship.consecutive is True
    outer = attribution.addresses[0]
    assert outer.front_delta.native_wei == -100
    assert outer.back_interval_delta.token0 == 115
    assert outer.full_cycle_delta.token0 == 15
    assert outer.full_cycle_delta.token1 == 0
    assert len(attribution.front_transfers) == 3
    assert len(attribution.back_transfers) == 3
    assert all(flow.touches_tracked_address for flow in attribution.front_transfers)
    assert all(flow.touches_tracked_address for flow in attribution.back_transfers)
    assert attribution.front_sender_native.native_delta_beyond_gas_and_value_wei == 4_499_900
    assert attribution.back_sender_native.native_delta_beyond_gas_and_value_wei == 4_499_900
