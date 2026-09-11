"""Observed transaction-wide trace, receipt-flow, and reconciliation evidence."""

from __future__ import annotations

from dataclasses import dataclass

from blockscope.economics import transaction_gas_fee_wei
from blockscope.erc20 import ERC20Transfer, TransferDecodeError, decode_transfer_log
from blockscope.observed_attribution import ObservedCycleAttribution
from blockscope.replay import (
    AnvilFork,
    ReplayBranchExecution,
    ReplayInputError,
    TransactionReplayResult,
    execute_replay_plan,
    plan_observed_replay,
    prepare_replay_environment,
    replay_pair_execution_exact,
    replay_receipt_available,
    replay_receipt_semantics_exact,
)
from blockscope.rpc import EthereumRPC
from blockscope.sandwiches import SandwichCandidate
from blockscope.tracing import (
    CallFrame,
    NativeValueTransfer,
    TraceAcquisition,
    TraceMode,
    TraceNormalizationError,
    acquire_transaction_trace,
    flatten_call_frames,
    maximum_call_depth,
    realized_native_transfers,
)
from blockscope.types import Block, Transaction, TransactionReceipt, index_transaction_receipts
from blockscope.uniswap_v2 import MetadataResolver, TokenMetadata

ZERO_ADDRESS = "0x" + "00" * 20


@dataclass(frozen=True, slots=True)
class TransferTokenEvidence:
    """One Transfer-shaped emitter with best-effort metadata and raw events."""

    address: str
    metadata: TokenMetadata
    candidate_pair_asset: bool
    transfers: tuple[ERC20Transfer, ...]


@dataclass(frozen=True, slots=True)
class NativeFlowReconciliation:
    """Trace value edges and gas reconciled to one checkpoint interval."""

    address: str
    checkpoint_delta_wei: int | None
    trace_inflow_wei: int | None
    trace_outflow_wei: int | None
    gas_paid_wei: int | None
    residual_wei: int | None


@dataclass(frozen=True, slots=True)
class CandidateTokenReconciliation:
    """Candidate-token receipt flow versus pair and endpoint evidence."""

    token_address: str
    metadata: TokenMetadata | None
    recipient_address: str | None
    front_transfer_delta: int
    back_transfer_delta: int
    full_transfer_delta: int
    pair_implied_delta: int | None
    checkpoint_delta: int | None
    transfer_matches_pair: bool | None
    transfer_matches_checkpoint: bool | None


@dataclass(frozen=True, slots=True)
class ObservedTransactionTraceAttribution:
    """Three distinct evidence sources aligned to one observed outer transaction."""

    historical_transaction_hash: str
    replay_transaction_hash: str | None
    transaction_index: int
    observed_reproduction_exact: bool
    trace_attribution_reliable: bool
    trace_mode: TraceMode | None
    root_call: CallFrame | None
    call_count: int
    maximum_call_depth: int | None
    call_types: tuple[str, ...]
    native_transfers: tuple[NativeValueTransfer, ...]
    transfer_events: tuple[ERC20Transfer, ...]
    token_contracts: tuple[TransferTokenEvidence, ...]
    token_flow_participants: tuple[str, ...]
    mint_events: int
    burn_events: int
    malformed_transfer_logs: int
    native_reconciliation: tuple[NativeFlowReconciliation, ...]
    diagnostics: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ObservedCandidateTraceAttribution:
    """Front/back execution-path evidence kept separate from endpoint checkpoints."""

    candidate: SandwichCandidate
    branch: ReplayBranchExecution
    backend_version: str
    preferred_call_tracer_supported: bool
    fallback_used: bool
    trace_mode: TraceMode | None
    front: ObservedTransactionTraceAttribution
    back: ObservedTransactionTraceAttribution
    all_token_contracts: tuple[TransferTokenEvidence, ...]
    all_token_flow_participants: tuple[str, ...]
    candidate_token_reconciliation: tuple[CandidateTokenReconciliation, ...]
    additional_token_contracts: tuple[str, ...]
    reliable: bool
    limitations: tuple[str, ...]


def discover_transfer_shaped_events(
    receipt: TransactionReceipt,
) -> tuple[tuple[ERC20Transfer, ...], int]:
    """Decode every structurally valid Transfer-shaped log in deterministic log order."""
    transfers: list[ERC20Transfer] = []
    malformed = 0
    for log in sorted(receipt.logs, key=lambda item: item.log_index):
        try:
            transfer = decode_transfer_log(log)
        except TransferDecodeError:
            malformed += 1
            continue
        if transfer is not None:
            transfers.append(transfer)
    return tuple(transfers), malformed


def token_flow_participants(
    transfers: tuple[ERC20Transfer, ...],
) -> tuple[str, ...]:
    """Return case-normalized nonzero participants in first-observed order."""
    participants: list[str] = []
    seen: set[str] = set()
    for transfer in transfers:
        for address in (transfer.from_address.lower(), transfer.to_address.lower()):
            if address == ZERO_ADDRESS or address in seen:
                continue
            seen.add(address)
            participants.append(address)
    return tuple(participants)


def _trace_exact(result: TransactionReplayResult | None) -> bool:
    return bool(
        result is not None
        and replay_receipt_available(result)
        and replay_receipt_semantics_exact(result)
        and replay_pair_execution_exact(result)
    )


class _TraceRecorder:
    """Acquire selected transaction traces after mining and before fork teardown."""

    def __init__(self, transaction_indexes: tuple[int, ...]) -> None:
        self.transaction_indexes = frozenset(transaction_indexes)
        self.local_hashes: dict[int, str] = {}
        self.acquisitions: dict[int, TraceAcquisition] = {}

    def before_submissions(self, fork: AnvilFork) -> None:
        del fork

    def after_submission(
        self,
        fork: AnvilFork,
        position: int,
        transaction: Transaction,
        local_transaction_hash: str,
    ) -> None:
        del fork, position
        if transaction.transaction_index in self.transaction_indexes:
            self.local_hashes[transaction.transaction_index] = local_transaction_hash

    def after_mine(self, fork: AnvilFork) -> None:
        preferred: TraceMode | None = None
        for transaction_index in sorted(self.transaction_indexes):
            local_hash = self.local_hashes.get(transaction_index)
            if local_hash is None:
                continue
            try:
                acquisition = acquire_transaction_trace(
                    fork,
                    local_hash,
                    preferred_mode=preferred,
                )
            except TraceNormalizationError as exc:
                acquisition = TraceAcquisition(
                    None,
                    None,
                    preferred is TraceMode.CALL_TRACER,
                    preferred is TraceMode.PARITY,
                    (f"trace normalization failed: {str(exc)[:500]}",),
                )
            self.acquisitions[transaction_index] = acquisition
            if acquisition.root_call is not None and acquisition.mode is not None:
                preferred = acquisition.mode


def _receipt_for_transaction(
    transaction: Transaction,
    receipts: tuple[TransactionReceipt, ...],
) -> TransactionReceipt:
    indexed = index_transaction_receipts(receipts)
    key = (transaction.transaction_index, transaction.hash.lower())
    try:
        return indexed[key]
    except KeyError as exc:
        raise ReplayInputError(
            f"receipt for transaction #{transaction.transaction_index} is unavailable"
        ) from exc


def _transfer_net(
    transfers: tuple[ERC20Transfer, ...],
    token_address: str,
    address: str,
) -> int:
    token = token_address.lower()
    participant = address.lower()
    return sum(
        (transfer.raw_amount if transfer.to_address.lower() == participant else 0)
        - (transfer.raw_amount if transfer.from_address.lower() == participant else 0)
        for transfer in transfers
        if transfer.token_address.lower() == token
    )


def _token_evidence(
    resolver: MetadataResolver,
    transfers: tuple[ERC20Transfer, ...],
    candidate_tokens: frozenset[str],
) -> tuple[TransferTokenEvidence, ...]:
    addresses: list[str] = []
    seen: set[str] = set()
    for transfer in transfers:
        address = transfer.token_address.lower()
        if address not in seen:
            seen.add(address)
            addresses.append(address)
    return tuple(
        TransferTokenEvidence(
            address,
            resolver.token(address),
            address in candidate_tokens,
            tuple(
                transfer
                for transfer in transfers
                if transfer.token_address.lower() == address
            ),
        )
        for address in addresses
    )


def _native_reconciliation(
    transaction: Transaction,
    replay_result: TransactionReplayResult | None,
    root: CallFrame | None,
    endpoint: ObservedCycleAttribution,
    *,
    front: bool,
) -> tuple[NativeFlowReconciliation, ...]:
    transfers = () if root is None else realized_native_transfers(root)
    gas_fee = transaction_gas_fee_wei(
        None if replay_result is None else replay_result.replay_receipt
    )
    reconciliations: list[NativeFlowReconciliation] = []
    for address_evidence in endpoint.addresses:
        address = address_evidence.tracked_address.address.lower()
        checkpoint_delta = (
            address_evidence.front_delta.native_wei
            if front
            else address_evidence.back_interval_delta.native_wei
        )
        if root is None:
            inflow = outflow = residual = None
        else:
            inflow = sum(
                item.value_wei for item in transfers if item.to_address.lower() == address
            )
            outflow = sum(
                item.value_wei for item in transfers if item.from_address.lower() == address
            )
            paid = gas_fee if transaction.from_address.lower() == address else 0
            residual = (
                None
                if checkpoint_delta is None or paid is None
                else checkpoint_delta - (inflow - outflow - paid)
            )
        paid_gas = gas_fee if transaction.from_address.lower() == address else 0
        reconciliations.append(
            NativeFlowReconciliation(
                address,
                checkpoint_delta,
                inflow,
                outflow,
                paid_gas,
                residual,
            )
        )
    return tuple(reconciliations)


def _transaction_attribution(
    transaction: Transaction,
    replay_result: TransactionReplayResult | None,
    acquisition: TraceAcquisition | None,
    receipt: TransactionReceipt,
    resolver: MetadataResolver,
    candidate_tokens: frozenset[str],
    endpoint: ObservedCycleAttribution,
    *,
    front: bool,
) -> ObservedTransactionTraceAttribution:
    root = None if acquisition is None else acquisition.root_call
    exact = _trace_exact(replay_result)
    transfers, malformed = discover_transfer_shaped_events(receipt)
    frames = () if root is None else flatten_call_frames(root)
    call_types = tuple(dict.fromkeys(frame.call_type for frame in frames))
    diagnostics = (
        ("local replay did not meet the observed-reproduction trace gate",)
        if not exact
        else ()
    ) + (() if acquisition is None else acquisition.diagnostics)
    return ObservedTransactionTraceAttribution(
        transaction.hash.lower(),
        None if replay_result is None else replay_result.local_transaction_hash,
        transaction.transaction_index,
        exact,
        exact and root is not None,
        None if acquisition is None else acquisition.mode,
        root,
        len(frames),
        None if root is None else maximum_call_depth(root),
        call_types,
        () if root is None else realized_native_transfers(root),
        transfers,
        _token_evidence(resolver, transfers, candidate_tokens),
        token_flow_participants(transfers),
        sum(transfer.from_address.lower() == ZERO_ADDRESS for transfer in transfers),
        sum(transfer.to_address.lower() == ZERO_ADDRESS for transfer in transfers),
        malformed,
        _native_reconciliation(
            transaction,
            replay_result,
            root,
            endpoint,
            front=front,
        ),
        diagnostics,
    )


def _candidate_token_reconciliation(
    candidate: SandwichCandidate,
    endpoint: ObservedCycleAttribution,
    front: ObservedTransactionTraceAttribution,
    back: ObservedTransactionTraceAttribution,
    front_transaction: Transaction,
    back_transaction: Transaction,
) -> tuple[CandidateTokenReconciliation, ...]:
    pair = candidate.front_run.pair_metadata
    tokens = (
        (pair.token0_address, candidate.front_run.token0_metadata, "token0"),
        (pair.token1_address, candidate.front_run.token1_metadata, "token1"),
    )
    shared_recipient = (
        front_transaction.to_address.lower()
        if front_transaction.to_address is not None
        and back_transaction.to_address is not None
        and front_transaction.to_address.lower() == back_transaction.to_address.lower()
        else None
    )
    address_evidence = next(
        (
            item
            for item in endpoint.addresses
            if shared_recipient is not None
            and item.tracked_address.address.lower() == shared_recipient
        ),
        None,
    )
    front_swap = candidate.front_run.swap
    back_swap = candidate.back_run.swap
    rows: list[CandidateTokenReconciliation] = []
    for token_address, metadata, position in tokens:
        if token_address is None:
            continue
        front_delta = (
            0
            if shared_recipient is None
            else _transfer_net(front.transfer_events, token_address, shared_recipient)
        )
        back_delta = (
            0
            if shared_recipient is None
            else _transfer_net(back.transfer_events, token_address, shared_recipient)
        )
        pair_delta = None
        if (
            shared_recipient is not None
            and front_swap.recipient.lower() == shared_recipient
            and back_swap.recipient.lower() == shared_recipient
        ):
            if position == "token0":
                pair_delta = (
                    front_swap.amount0_out
                    - front_swap.amount0_in
                    + back_swap.amount0_out
                    - back_swap.amount0_in
                )
            else:
                pair_delta = (
                    front_swap.amount1_out
                    - front_swap.amount1_in
                    + back_swap.amount1_out
                    - back_swap.amount1_in
                )
        checkpoint_delta = None
        if address_evidence is not None:
            checkpoint_delta = (
                address_evidence.full_cycle_delta.token0
                if position == "token0"
                else address_evidence.full_cycle_delta.token1
            )
        full_delta = front_delta + back_delta
        rows.append(
            CandidateTokenReconciliation(
                token_address.lower(),
                metadata,
                shared_recipient,
                front_delta,
                back_delta,
                full_delta,
                pair_delta,
                checkpoint_delta,
                None if pair_delta is None else full_delta == pair_delta,
                None if checkpoint_delta is None else full_delta == checkpoint_delta,
            )
        )
    return tuple(rows)


def execute_observed_trace_attribution(
    upstream_rpc: EthereumRPC,
    upstream_rpc_url: str,
    block: Block,
    historical_receipts: tuple[TransactionReceipt, ...],
    candidate: SandwichCandidate,
    endpoint: ObservedCycleAttribution,
    *,
    anvil_executable: str = "anvil",
) -> ObservedCandidateTraceAttribution:
    """Replay the observed cycle, trace front/back locally, and align all evidence."""
    front_index = candidate.front_run.swap.transaction_index
    back_index = candidate.back_run.swap.transaction_index
    plan = plan_observed_replay(block, back_index)
    receipts = tuple(
        _receipt_for_transaction(transaction, historical_receipts)
        for transaction in plan.transactions
    )
    executable_path, _ = AnvilFork.require_available(anvil_executable)
    setup = prepare_replay_environment(upstream_rpc, block)
    if setup.chain_id != 1:
        raise ReplayInputError(
            f"observed Ethereum tracing requires mainnet chain ID 1; got {setup.chain_id}"
        )
    recorder = _TraceRecorder((front_index, back_index))
    branch = execute_replay_plan(
        upstream_rpc_url,
        plan,
        receipts,
        setup.historical_context,
        setup.hardfork,
        anvil_executable=executable_path,
        observer=recorder,
    )
    by_index = {
        result.historical_transaction.transaction_index: result
        for result in branch.transactions
    }
    transactions = {
        transaction.transaction_index: transaction for transaction in block.transactions
    }
    front_transaction = transactions[front_index]
    back_transaction = transactions[back_index]
    resolver = MetadataResolver(upstream_rpc, block.number)
    candidate_tokens = frozenset(
        address.lower()
        for address in (
            candidate.front_run.pair_metadata.token0_address,
            candidate.front_run.pair_metadata.token1_address,
        )
        if address is not None
    )
    front = _transaction_attribution(
        front_transaction,
        by_index.get(front_index),
        recorder.acquisitions.get(front_index),
        _receipt_for_transaction(front_transaction, historical_receipts),
        resolver,
        candidate_tokens,
        endpoint,
        front=True,
    )
    back = _transaction_attribution(
        back_transaction,
        by_index.get(back_index),
        recorder.acquisitions.get(back_index),
        _receipt_for_transaction(back_transaction, historical_receipts),
        resolver,
        candidate_tokens,
        endpoint,
        front=False,
    )
    all_transfers = (*front.transfer_events, *back.transfer_events)
    all_tokens = _token_evidence(resolver, all_transfers, candidate_tokens)
    acquisitions = tuple(recorder.acquisitions.values())
    modes = tuple(
        dict.fromkeys(
            acquisition.mode
            for acquisition in acquisitions
            if acquisition.mode is not None
        )
    )
    limitations = [
        "checkpoint balances, receipt logs, and execution traces are distinct evidence sources",
        (
            "trace-native-flow coverage does not necessarily include every "
            "SELFDESTRUCT-style balance transfer"
        ),
        (
            "DELEGATECALL targets identify code execution, not ownership or the account "
            "whose storage/balance context is used"
        ),
        "Transfer-shaped events do not prove well-behaved ERC-20 semantics",
        "call paths do not establish common ownership or beneficial-owner profit",
        "counterfactual execution is intentionally not traced",
    ]
    if len(modes) > 1:
        limitations.append("front and back traces required different backend trace modes")
    return ObservedCandidateTraceAttribution(
        candidate,
        branch,
        branch.backend_version,
        bool(acquisitions) and all(
            acquisition.preferred_call_tracer_supported for acquisition in acquisitions
        ),
        any(acquisition.fallback_used for acquisition in acquisitions),
        modes[0] if len(modes) == 1 else None,
        front,
        back,
        all_tokens,
        token_flow_participants(tuple(all_transfers)),
        _candidate_token_reconciliation(
            candidate,
            endpoint,
            front,
            back,
            front_transaction,
            back_transaction,
        ),
        tuple(
            evidence.address
            for evidence in all_tokens
            if not evidence.candidate_pair_asset
        ),
        endpoint.reliable and front.trace_attribution_reliable and back.trace_attribution_reliable,
        tuple(limitations),
    )
