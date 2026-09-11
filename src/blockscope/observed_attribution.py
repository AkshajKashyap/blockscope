"""Observed full-cycle replay with exact address-level balance checkpoints."""

from dataclasses import dataclass
from enum import StrEnum

from blockscope.economics import ObservedSandwichEconomics, transaction_gas_fee_wei
from blockscope.erc20 import (
    ERC20BalanceReadError,
    ERC20Transfer,
    TransferDecodeError,
    balance_of,
    decode_transfer_log,
)
from blockscope.replay import (
    AnvilFork,
    ReplayBranchExecution,
    ReplayError,
    ReplayInputError,
    TransactionReplayResult,
    execute_replay_plan,
    plan_observed_replay,
    prepare_replay_environment,
    replay_gas_exact,
    replay_pair_execution_exact,
    replay_receipt_available,
    replay_receipt_semantics_exact,
)
from blockscope.rpc import EthereumRPC
from blockscope.sandwiches import SandwichCandidate
from blockscope.types import (
    Block,
    Transaction,
    TransactionReceipt,
    index_transaction_receipts,
    transaction_identity,
)
from blockscope.uniswap_v2 import TokenMetadata


class CheckpointName(StrEnum):
    """Transaction-position state observed from cumulative pending execution."""

    BEFORE_FRONT = "S0 before front"
    AFTER_FRONT = "S1 after front"
    AFTER_VICTIMS = "S2 after victim(s)"
    AFTER_BACK = "S3 after back"


@dataclass(frozen=True, slots=True)
class TrackedAddress:
    """An address and its explicit transaction relationships, without ownership claims."""

    address: str
    relationships: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AddressCheckpoint:
    """Authoritative local-state balances for one address at one checkpoint."""

    checkpoint: CheckpointName
    address: str
    native_wei: int | None
    token0_balance: int | None
    token1_balance: int | None
    errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AddressBalanceDelta:
    """Exact later-minus-earlier balance changes, retaining unavailable values."""

    native_wei: int | None
    token0: int | None
    token1: int | None


@dataclass(frozen=True, slots=True)
class AddressCycleBalanceEvidence:
    """Four checkpoints and exact interval deltas for one tracked address."""

    tracked_address: TrackedAddress
    before_front: AddressCheckpoint
    after_front: AddressCheckpoint
    after_victims: AddressCheckpoint
    after_back: AddressCheckpoint
    front_delta: AddressBalanceDelta
    victim_interval_delta: AddressBalanceDelta
    back_interval_delta: AddressBalanceDelta
    full_cycle_delta: AddressBalanceDelta


@dataclass(frozen=True, slots=True)
class TransferFlowEvidence:
    """A candidate-token Transfer and whether it touches the tracked address set."""

    transfer: ERC20Transfer
    touches_tracked_address: bool


@dataclass(frozen=True, slots=True)
class NonceRelationship:
    """Observed outer-transaction sender and nonce facts."""

    front_sender: str
    front_nonce: int | None
    back_sender: str
    back_nonce: int | None
    same_sender: bool
    consecutive: bool | None


@dataclass(frozen=True, slots=True)
class SenderNativeTransactionEvidence:
    """Sender native delta compared with exact replay gas and transaction value."""

    transaction_index: int
    transaction_value_wei: int
    gas_fee_wei: int | None
    observed_native_delta_wei: int | None
    native_delta_beyond_gas_and_value_wei: int | None
    interval_contains_only_transaction: bool


@dataclass(frozen=True, slots=True)
class ObservedCycleAttribution:
    """Faithful full-cycle replay plus narrow tracked-address state evidence."""

    candidate: SandwichCandidate
    branch: ReplayBranchExecution
    economics: ObservedSandwichEconomics
    candidate_pair_address: str
    token0_address: str | None
    token1_address: str | None
    token0_metadata: TokenMetadata | None
    token1_metadata: TokenMetadata | None
    tracked_addresses: tuple[TrackedAddress, ...]
    addresses: tuple[AddressCycleBalanceEvidence, ...]
    front_exact: bool
    victims_exact: bool
    back_exact: bool
    checkpoint_reads_complete: bool
    pending_final_consistent: bool
    nonce_relationship: NonceRelationship
    front_sender_native: SenderNativeTransactionEvidence
    back_sender_native: SenderNativeTransactionEvidence
    front_transfers: tuple[TransferFlowEvidence, ...]
    back_transfers: tuple[TransferFlowEvidence, ...]
    malformed_transfer_logs: int
    reliable: bool
    limitations: tuple[str, ...]


def _subtract(later: int | None, earlier: int | None) -> int | None:
    return None if later is None or earlier is None else later - earlier


def calculate_balance_delta(
    earlier: AddressCheckpoint,
    later: AddressCheckpoint,
) -> AddressBalanceDelta:
    """Calculate a signed exact balance delta without fabricating unavailable values."""
    if earlier.address.lower() != later.address.lower():
        raise ValueError("balance checkpoints belong to different addresses")
    return AddressBalanceDelta(
        native_wei=_subtract(later.native_wei, earlier.native_wei),
        token0=_subtract(later.token0_balance, earlier.token0_balance),
        token1=_subtract(later.token1_balance, earlier.token1_balance),
    )


def tracked_addresses_for_candidate(
    block: Block,
    candidate: SandwichCandidate,
) -> tuple[TrackedAddress, ...]:
    """Select only the outer sender and distinct outer transaction recipients."""
    by_index = {transaction.transaction_index: transaction for transaction in block.transactions}
    front_index = candidate.front_run.swap.transaction_index
    back_index = candidate.back_run.swap.transaction_index
    try:
        front = by_index[front_index]
        back = by_index[back_index]
    except KeyError as exc:
        raise ReplayInputError(f"candidate transaction #{exc.args[0]} is absent") from exc

    ordered: list[str] = [candidate.actor_address]
    relationships: dict[str, list[str]] = {
        candidate.actor_address.lower(): ["outer sender (front and back transaction from)"]
    }
    for transaction, relationship in (
        (front, "front transaction recipient (to)"),
        (back, "back transaction recipient (to)"),
    ):
        if transaction.to_address is None:
            continue
        key = transaction.to_address.lower()
        if key not in relationships:
            ordered.append(transaction.to_address)
            relationships[key] = []
        relationships[key].append(relationship)
    return tuple(
        TrackedAddress(address.lower(), tuple(relationships[address.lower()]))
        for address in ordered
    )


class _CheckpointRecorder:
    """Mutable read-only observer whose published evidence is immutable."""

    def __init__(
        self,
        tracked_addresses: tuple[TrackedAddress, ...],
        token0_address: str | None,
        token1_address: str | None,
        front_index: int,
        final_victim_index: int,
        back_index: int,
    ) -> None:
        self.tracked_addresses = tracked_addresses
        self.token0_address = token0_address
        self.token1_address = token1_address
        self.front_index = front_index
        self.final_victim_index = final_victim_index
        self.back_index = back_index
        self.checkpoints: dict[CheckpointName, tuple[AddressCheckpoint, ...]] = {}
        self.final_after_mine: tuple[AddressCheckpoint, ...] | None = None

    def _read(
        self,
        fork: AnvilFork,
        checkpoint: CheckpointName,
        block_tag: str,
    ) -> tuple[AddressCheckpoint, ...]:
        values: list[AddressCheckpoint] = []
        for tracked in self.tracked_addresses:
            errors: list[str] = []
            native_wei: int | None = None
            token0_balance: int | None = None
            token1_balance: int | None = None
            try:
                native_wei = fork.get_balance(tracked.address, block_tag)
            except ReplayError as exc:
                errors.append(f"native balance: {exc}")
            if self.token0_address is not None:
                try:
                    token0_balance = balance_of(
                        fork, self.token0_address, tracked.address, block_tag
                    )
                except (ReplayError, ERC20BalanceReadError) as exc:
                    errors.append(f"token0 balance: {exc}")
            if self.token1_address is not None:
                try:
                    token1_balance = balance_of(
                        fork, self.token1_address, tracked.address, block_tag
                    )
                except (ReplayError, ERC20BalanceReadError) as exc:
                    errors.append(f"token1 balance: {exc}")
            values.append(
                AddressCheckpoint(
                    checkpoint,
                    tracked.address,
                    native_wei,
                    token0_balance,
                    token1_balance,
                    tuple(errors),
                )
            )
        return tuple(values)

    def before_submissions(self, fork: AnvilFork) -> None:
        if self.front_index == 0:
            self.checkpoints[CheckpointName.BEFORE_FRONT] = self._read(
                fork, CheckpointName.BEFORE_FRONT, "latest"
            )

    def after_submission(
        self,
        fork: AnvilFork,
        position: int,
        transaction: Transaction,
        local_transaction_hash: str | None = None,
    ) -> None:
        del local_transaction_hash
        del position
        index = transaction.transaction_index
        if index == self.front_index - 1:
            self.checkpoints[CheckpointName.BEFORE_FRONT] = self._read(
                fork, CheckpointName.BEFORE_FRONT, "pending"
            )
        if index == self.front_index:
            self.checkpoints[CheckpointName.AFTER_FRONT] = self._read(
                fork, CheckpointName.AFTER_FRONT, "pending"
            )
        if index == self.final_victim_index:
            self.checkpoints[CheckpointName.AFTER_VICTIMS] = self._read(
                fork, CheckpointName.AFTER_VICTIMS, "pending"
            )
        if index == self.back_index:
            self.checkpoints[CheckpointName.AFTER_BACK] = self._read(
                fork, CheckpointName.AFTER_BACK, "pending"
            )

    def after_mine(self, fork: AnvilFork) -> None:
        self.final_after_mine = self._read(fork, CheckpointName.AFTER_BACK, "latest")


def _historical_receipts_for_plan(
    plan_transactions: tuple[Transaction, ...],
    receipts: tuple[TransactionReceipt, ...],
) -> tuple[TransactionReceipt, ...]:
    by_identity = index_transaction_receipts(receipts)
    selected: list[TransactionReceipt] = []
    for transaction in plan_transactions:
        receipt = by_identity.get(
            transaction_identity(transaction.transaction_index, transaction.hash)
        )
        if receipt is None:
            raise ReplayInputError(
                f"historical receipt unavailable for transaction #{transaction.transaction_index}"
            )
        selected.append(receipt)
    return tuple(selected)


def _result_by_index(
    branch: ReplayBranchExecution,
) -> dict[int, TransactionReplayResult]:
    return {
        result.historical_transaction.transaction_index: result
        for result in branch.transactions
    }


def _candidate_leg_exact(result: TransactionReplayResult | None) -> bool:
    return bool(
        result is not None
        and replay_receipt_semantics_exact(result)
        and replay_pair_execution_exact(result)
        and replay_gas_exact(result)
    )


def _all_replay_exact(branch: ReplayBranchExecution) -> bool:
    return all(
        replay_receipt_available(result)
        and replay_receipt_semantics_exact(result)
        and replay_gas_exact(result)
        for result in branch.transactions
    )


def _checkpoints_complete(
    checkpoints: dict[CheckpointName, tuple[AddressCheckpoint, ...]],
    tracked_count: int,
) -> bool:
    return all(
        len(checkpoints.get(name, ())) == tracked_count
        and all(
            item.native_wei is not None
            and item.token0_balance is not None
            and item.token1_balance is not None
            for item in checkpoints.get(name, ())
        )
        for name in CheckpointName
    )


def _balances_equal(
    pending: tuple[AddressCheckpoint, ...] | None,
    mined: tuple[AddressCheckpoint, ...] | None,
) -> bool:
    if pending is None or mined is None or len(pending) != len(mined):
        return False
    return all(
        left.address.lower() == right.address.lower()
        and left.native_wei == right.native_wei
        and left.token0_balance == right.token0_balance
        and left.token1_balance == right.token1_balance
        for left, right in zip(pending, mined, strict=True)
    )


def _address_evidence(
    tracked: tuple[TrackedAddress, ...],
    checkpoints: dict[CheckpointName, tuple[AddressCheckpoint, ...]],
) -> tuple[AddressCycleBalanceEvidence, ...]:
    by_checkpoint = {
        name: {item.address.lower(): item for item in checkpoints.get(name, ())}
        for name in CheckpointName
    }
    evidence: list[AddressCycleBalanceEvidence] = []
    for item in tracked:
        try:
            before = by_checkpoint[CheckpointName.BEFORE_FRONT][item.address]
            after_front = by_checkpoint[CheckpointName.AFTER_FRONT][item.address]
            after_victims = by_checkpoint[CheckpointName.AFTER_VICTIMS][item.address]
            after_back = by_checkpoint[CheckpointName.AFTER_BACK][item.address]
        except KeyError:
            continue
        evidence.append(
            AddressCycleBalanceEvidence(
                item,
                before,
                after_front,
                after_victims,
                after_back,
                calculate_balance_delta(before, after_front),
                calculate_balance_delta(after_front, after_victims),
                calculate_balance_delta(after_victims, after_back),
                calculate_balance_delta(before, after_back),
            )
        )
    return tuple(evidence)


def _transfer_evidence(
    receipt: TransactionReceipt | None,
    candidate_tokens: frozenset[str],
    tracked_addresses: frozenset[str],
) -> tuple[tuple[TransferFlowEvidence, ...], int]:
    if receipt is None:
        return (), 0
    transfers: list[TransferFlowEvidence] = []
    malformed = 0
    for log in sorted(receipt.logs, key=lambda item: item.log_index):
        try:
            transfer = decode_transfer_log(log)
        except TransferDecodeError:
            malformed += 1
            continue
        if transfer is None or transfer.token_address.lower() not in candidate_tokens:
            continue
        transfers.append(
            TransferFlowEvidence(
                transfer,
                transfer.from_address.lower() in tracked_addresses
                or transfer.to_address.lower() in tracked_addresses,
            )
        )
    return tuple(transfers), malformed


def _sender_native_evidence(
    transaction: Transaction,
    result: TransactionReplayResult | None,
    native_delta: int | None,
    *,
    interval_contains_only_transaction: bool,
) -> SenderNativeTransactionEvidence:
    receipt = None if result is None else result.replay_receipt
    gas_fee = transaction_gas_fee_wei(receipt)
    residual = (
        None
        if native_delta is None or gas_fee is None
        else native_delta + gas_fee + transaction.value
    )
    return SenderNativeTransactionEvidence(
        transaction.transaction_index,
        transaction.value,
        gas_fee,
        native_delta,
        residual,
        interval_contains_only_transaction,
    )


def observed_cycle_attribution_reliable(
    *,
    front_exact: bool,
    victims_exact: bool,
    back_exact: bool,
    complete_replay_exact: bool,
    checkpoint_reads_complete: bool,
    pending_final_consistent: bool,
    candidate_tokens_known: bool,
) -> bool:
    """State the complete domain-level reliability gate without hiding its inputs."""
    return all(
        (
            front_exact,
            victims_exact,
            back_exact,
            complete_replay_exact,
            checkpoint_reads_complete,
            pending_final_consistent,
            candidate_tokens_known,
        )
    )


def execute_observed_cycle_attribution(
    upstream_rpc: EthereumRPC,
    upstream_rpc_url: str,
    block: Block,
    historical_receipts: tuple[TransactionReceipt, ...],
    candidate: SandwichCandidate,
    economics: ObservedSandwichEconomics,
    *,
    anvil_executable: str = "anvil",
) -> ObservedCycleAttribution:
    """Replay through the back leg once and read cumulative pending balance state."""
    front_index = candidate.front_run.swap.transaction_index
    victim_indexes = tuple(victim.swap.transaction_index for victim in candidate.victims)
    if not victim_indexes:
        raise ReplayInputError("candidate has no victims")
    back_index = candidate.back_run.swap.transaction_index
    plan = plan_observed_replay(block, back_index)
    receipts = _historical_receipts_for_plan(plan.transactions, historical_receipts)
    executable_path, _ = AnvilFork.require_available(anvil_executable)
    setup = prepare_replay_environment(upstream_rpc, block)
    if setup.chain_id != 1:
        raise ReplayInputError(
            f"observed Ethereum attribution requires mainnet chain ID 1; got {setup.chain_id}"
        )
    pair_metadata = candidate.front_run.pair_metadata
    token0_address = pair_metadata.token0_address
    token1_address = pair_metadata.token1_address
    tracked = tracked_addresses_for_candidate(block, candidate)
    recorder = _CheckpointRecorder(
        tracked,
        token0_address,
        token1_address,
        front_index,
        victim_indexes[-1],
        back_index,
    )
    branch = execute_replay_plan(
        upstream_rpc_url,
        plan,
        receipts,
        setup.historical_context,
        setup.hardfork,
        anvil_executable=executable_path,
        observer=recorder,
    )
    results = _result_by_index(branch)
    front_exact = _candidate_leg_exact(results.get(front_index))
    victims_exact = all(_candidate_leg_exact(results.get(index)) for index in victim_indexes)
    back_exact = _candidate_leg_exact(results.get(back_index))
    tokens_known = token0_address is not None and token1_address is not None
    checkpoint_reads_complete = bool(
        tokens_known and _checkpoints_complete(recorder.checkpoints, len(tracked))
    )
    pending_final_consistent = _balances_equal(
        recorder.checkpoints.get(CheckpointName.AFTER_BACK),
        recorder.final_after_mine,
    )
    addresses = _address_evidence(tracked, recorder.checkpoints)
    tracked_set = frozenset(item.address.lower() for item in tracked)
    candidate_tokens = frozenset(
        address.lower()
        for address in (token0_address, token1_address)
        if address is not None
    )
    front_result = results.get(front_index)
    back_result = results.get(back_index)
    front_transfers, front_malformed = _transfer_evidence(
        None if front_result is None else front_result.replay_receipt,
        candidate_tokens,
        tracked_set,
    )
    back_transfers, back_malformed = _transfer_evidence(
        None if back_result is None else back_result.replay_receipt,
        candidate_tokens,
        tracked_set,
    )
    by_transaction = {
        transaction.transaction_index: transaction for transaction in block.transactions
    }
    front_transaction = by_transaction[front_index]
    back_transaction = by_transaction[back_index]
    same_sender = (
        front_transaction.from_address.lower() == back_transaction.from_address.lower()
    )
    consecutive = (
        None
        if not same_sender
        or front_transaction.nonce is None
        or back_transaction.nonce is None
        else back_transaction.nonce == front_transaction.nonce + 1
    )
    nonce_relationship = NonceRelationship(
        front_transaction.from_address.lower(),
        front_transaction.nonce,
        back_transaction.from_address.lower(),
        back_transaction.nonce,
        same_sender,
        consecutive,
    )
    outer_address = candidate.actor_address.lower()
    outer = next(
        (item for item in addresses if item.tracked_address.address == outer_address),
        None,
    )
    front_native_delta = None if outer is None else outer.front_delta.native_wei
    back_native_delta = None if outer is None else outer.back_interval_delta.native_wei
    front_sender_native = _sender_native_evidence(
        front_transaction,
        front_result,
        front_native_delta,
        interval_contains_only_transaction=True,
    )
    back_sender_native = _sender_native_evidence(
        back_transaction,
        back_result,
        back_native_delta,
        interval_contains_only_transaction=back_index == victim_indexes[-1] + 1,
    )
    reliable = observed_cycle_attribution_reliable(
        front_exact=front_exact,
        victims_exact=victims_exact,
        back_exact=back_exact,
        complete_replay_exact=_all_replay_exact(branch),
        checkpoint_reads_complete=checkpoint_reads_complete,
        pending_final_consistent=pending_final_consistent,
        candidate_tokens_known=tokens_known,
    )
    limitations: list[str] = []
    for warning in branch.environment.setup_warnings:
        limitations.append(f"environment: {warning}")
    if branch.environment.mismatched_fields:
        limitations.append(
            "historical context mismatches: "
            + ", ".join(branch.environment.mismatched_fields)
        )
    if not tokens_known:
        limitations.append("candidate token identities are unavailable")
    if not checkpoint_reads_complete:
        limitations.append("one or more checkpoint balance reads are unavailable")
    if not pending_final_consistent:
        limitations.append("pending S3 balances did not match post-mine latest balances")
    if front_malformed + back_malformed:
        limitations.append("one or more matching Transfer logs were malformed")
    limitations.extend(
        (
            "Transfer logs may be incomplete for non-standard tokens and do not show native calls",
            "balance endpoints prove address state changes but not the causal transfer path",
            "tracked transaction recipients are not assumed to share beneficial ownership",
            "native ETH, token0, and token1 remain separate assets and are not universally netted",
            (
                "the historical back is not replayed in the front-omitted branch because "
                "omitting the front can break its sender nonce sequence"
            ),
        )
    )
    return ObservedCycleAttribution(
        candidate,
        branch,
        economics,
        candidate.pair_address.lower(),
        token0_address,
        token1_address,
        candidate.front_run.token0_metadata,
        candidate.front_run.token1_metadata,
        tracked,
        addresses,
        front_exact,
        victims_exact,
        back_exact,
        checkpoint_reads_complete,
        pending_final_consistent,
        nonce_relationship,
        front_sender_native,
        back_sender_native,
        front_transfers,
        back_transfers,
        front_malformed + back_malformed,
        reliable,
        tuple(limitations),
    )
