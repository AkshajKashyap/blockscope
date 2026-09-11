"""Reproducible staged corpus evaluation over existing BlockScope analyses."""

from __future__ import annotations

import json
import statistics
import subprocess
import time
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from blockscope.counterfactual import (
    CounterfactualStatus,
    FixedInputCounterfactual,
    analyze_fixed_input_counterfactuals,
)
from blockscope.evm_counterfactual import (
    CounterfactualEVMExecution,
    execute_front_omission_counterfactual,
)
from blockscope.replay import (
    AnvilFork,
    AnvilUnavailableError,
    ComparisonState,
    ReplayInputError,
    ReplayRPCError,
    ReplaySubmissionStatus,
    UnsupportedTransactionType,
    infer_ethereum_hardfork,
    replay_pair_execution_exact,
    replay_receipt_semantics_exact,
)
from blockscope.rpc import BlockScopeError, EthereumRPC, RPCError, redact_rpc_url_in_text
from blockscope.sandwiches import SandwichCandidate, detect_strict_sandwich_candidates
from blockscope.types import Block, TransactionReceipt
from blockscope.uniswap_v2 import BlockSwapAnalysis, analyze_block_swaps


class BlockEvaluationState(StrEnum):
    COMPLETE = "complete"
    PROVIDER_FAILURE = "provider_failure"


class ProvenanceState(StrEnum):
    CANONICAL = "canonical"
    NON_CANONICAL = "noncanonical"
    UNAVAILABLE = "unavailable"
    NOT_EVALUATED = "not_evaluated"


class MathematicalState(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    NOT_EVALUATED = "not_evaluated"


class ObservedReplayState(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    EXACT = "exact"
    SEMANTIC_MISMATCH = "semantic_mismatch"
    SUBMISSION_FAILURE = "submission_failure"
    ENVIRONMENT_FAILURE = "environment_failure"
    PROVIDER_FAILURE = "provider_failure"
    UNSUPPORTED_TRANSACTION_TYPE = "unsupported_transaction_type"
    OTHER_OPERATIONAL_FAILURE = "other_operational_failure"


class EVMCounterfactualState(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    RELIABLE = "reliable"
    UNRELIABLE = "unreliable"
    FAILED = "failed"


class TraceEvaluationState(StrEnum):
    NOT_ATTEMPTED = "not_attempted"
    RELIABLE = "reliable"
    UNAVAILABLE = "unavailable"


class FailureKind(StrEnum):
    PROVIDER_ERROR = "provider_error"
    ANVIL_STARTUP_ERROR = "anvil_startup_error"
    ANVIL_RPC_ERROR = "anvil_rpc_error"
    OBSERVED_REPLAY_MISMATCH = "observed_replay_mismatch"
    PREFIX_REPLAY_FAILURE = "prefix_replay_failure"
    COUNTERFACTUAL_TARGET_REVERT = "counterfactual_target_revert"
    COUNTERFACTUAL_SWAP_ABSENT = "counterfactual_swap_absent"
    NONCANONICAL_PAIR = "noncanonical_pair"
    MODEL_MISMATCH = "model_mismatch"
    UNSUPPORTED_TRANSACTION_TYPE = "unsupported_transaction_type"
    UNSUPPORTED_CASE = "unsupported_case"
    METADATA_UNAVAILABLE = "metadata_unavailable"
    OTHER_OPERATIONAL_FAILURE = "other_operational_failure"


@dataclass(frozen=True, slots=True)
class EvaluationConfiguration:
    start_block: int
    end_block: int
    candidate_limit: int
    evm_experiment_limit: int
    evm_cooldown_seconds: int = 0
    selection_rule: str = (
        "ascending block number, then front transaction index/log index; first limits only"
    )

    def __post_init__(self) -> None:
        if self.start_block < 0 or self.end_block < 0:
            raise ValueError("evaluation block numbers must be non-negative")
        if self.end_block < self.start_block:
            raise ValueError("end block must be greater than or equal to start block")
        if self.candidate_limit < 0:
            raise ValueError("candidate limit must be non-negative")
        if self.evm_experiment_limit < 0:
            raise ValueError("EVM experiment limit must be non-negative")
        if self.evm_cooldown_seconds < 0:
            raise ValueError("EVM cooldown must be non-negative")

    @property
    def block_count(self) -> int:
        return self.end_block - self.start_block + 1


@dataclass(frozen=True, slots=True)
class SoftwareMetadata:
    blockscope_version: str
    git_commit: str | None
    git_worktree_dirty: bool | None
    anvil_version: str | None


@dataclass(frozen=True, slots=True)
class RPCRequestCounts:
    scope: str
    blocks: int
    receipts: int
    chain_ids: int
    historical_calls: int


@dataclass(frozen=True, slots=True)
class StageTiming:
    stage: str
    count: int
    total_seconds: float
    mean_seconds: float | None
    median_seconds: float | None


@dataclass(frozen=True, slots=True)
class HardforkSelection:
    block_number: int
    hardfork: str


@dataclass(frozen=True, slots=True)
class BlockEvaluationRecord:
    block_number: int
    state: BlockEvaluationState
    transaction_count: int
    receipts_fetched: int
    swap_count: int
    unique_pair_count: int
    swap_pair_addresses: tuple[str, ...]
    malformed_swap_logs: int
    malformed_sync_logs: int
    swaps_without_reconstructed_reserves: int
    invalid_reserve_reconstructions: int
    metadata_lookup_failures: int
    continuous_pair_segments: int
    potential_front_legs_considered: int
    rejected_no_victims: int
    rejected_no_closing_back_run: int
    rejected_invalid_leg_sequence: int
    rejected_closing_sender_mismatch: int
    rejected_invalid_quote: int
    rejected_adverse_movement: int
    rejected_back_run_reversal: int
    duplicate_candidates_suppressed: int
    strict_candidates_found: int
    block_ingestion_seconds: float
    receipt_v2_analysis_seconds: float
    candidate_detection_seconds: float
    failure_kind: FailureKind | None = None
    failure_detail: str | None = None


@dataclass(frozen=True, slots=True)
class TransactionReference:
    transaction_index: int
    transaction_hash: str


@dataclass(frozen=True, slots=True)
class VictimMathematicalRecord:
    transaction_index: int
    observed_pair_input: int
    observed_pair_output: int
    mathematical_output: int


@dataclass(frozen=True, slots=True)
class CandidateEvaluationRecord:
    ordinal: int
    selected_for_candidate_analysis: bool
    selected_for_evm: bool
    block_number: int
    pair_address: str
    front_transaction: TransactionReference
    victim_transactions: tuple[TransactionReference, ...]
    back_transaction: TransactionReference
    provenance: ProvenanceState
    provenance_reason: str | None
    mathematical_state: MathematicalState
    mathematical_victims: tuple[VictimMathematicalRecord, ...]
    observed_replay: ObservedReplayState
    evm_counterfactual: EVMCounterfactualState
    observed_victim_status: int | None
    counterfactual_victim_status: int | None
    observed_pair_input: int | None
    counterfactual_pair_input: int | None
    observed_pair_output: int | None
    counterfactual_pair_output: int | None
    mathematical_output: int | None
    math_vs_evm_difference: int | None
    pair_input_comparison: ComparisonState
    counterfactual_pair_swap_present: bool | None
    trace_attribution: TraceEvaluationState
    failure_kinds: tuple[FailureKind, ...]
    failure_details: tuple[str, ...]
    mathematical_seconds: float
    evm_seconds: float

    @property
    def victim_count(self) -> int:
        return len(self.victim_transactions)


@dataclass(frozen=True, slots=True)
class FailureCount:
    kind: FailureKind
    count: int


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    blocks_inspected: int
    blocks_completed: int
    transactions_inspected: int
    receipts_fetched: int
    v2_compatible_swaps: int
    unique_pairs: int
    malformed_swap_logs: int
    malformed_sync_logs: int
    swaps_without_reconstructed_reserves: int
    invalid_reserve_reconstructions: int
    metadata_lookup_failures: int
    strict_candidates: int
    detector_potential_front_legs: int
    detector_rejected_no_victims: int
    detector_rejected_no_closing_back_run: int
    detector_rejected_invalid_leg_sequence: int
    detector_rejected_closing_sender_mismatch: int
    detector_rejected_invalid_quote: int
    detector_rejected_adverse_movement: int
    detector_rejected_back_run_reversal: int
    single_victim_candidates: int
    multi_victim_candidates: int
    canonical_candidates: int
    noncanonical_candidates: int
    provenance_unavailable_candidates: int
    candidate_analysis_not_evaluated: int
    observed_replay_attempted: int
    observed_replay_exact: int
    observed_replay_semantic_mismatches: int
    observed_replay_submission_failures: int
    observed_replay_environment_failures: int
    observed_replay_provider_failures: int
    observed_replay_unsupported_transaction_types: int
    observed_replay_other_failures: int
    evm_counterfactual_attempted: int
    evm_counterfactual_reliable: int
    target_success_to_success: int
    target_success_to_revert: int
    counterfactual_pair_swap_present: int
    counterfactual_pair_swap_absent: int
    pair_input_unchanged: int
    pair_input_changed: int
    mathematical_model_eligible: int
    math_evm_comparisons: int
    math_evm_exact_matches: int
    math_evm_nonzero_differences: int
    math_model_unavailable: int
    evm_comparison_unavailable: int


@dataclass(frozen=True, slots=True)
class EvaluationArtifact:
    schema_version: int
    created_at_utc: str
    configuration: EvaluationConfiguration
    software: SoftwareMetadata
    hardfork_selections: tuple[HardforkSelection, ...]
    rpc_requests: RPCRequestCounts
    metrics: EvaluationMetrics
    failure_taxonomy: tuple[FailureCount, ...]
    timings: tuple[StageTiming, ...]
    blocks: tuple[BlockEvaluationRecord, ...]
    candidates: tuple[CandidateEvaluationRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a deterministic JSON-compatible structure."""
        return asdict(self)


@dataclass(slots=True)
class _CandidateWork:
    record: CandidateEvaluationRecord
    candidate: SandwichCandidate
    mathematical: FixedInputCounterfactual | None
    block: Block
    receipts: tuple[TransactionReceipt, ...]


class _CountingCachingRPC:
    """Evaluation-only request counter with a one-block cache for public APIs."""

    def __init__(self, rpc: EthereumRPC) -> None:
        self._rpc = rpc
        self._blocks: dict[int, Block] = {}
        self.blocks = 0
        self.receipts = 0
        self.chain_ids = 0
        self.historical_calls = 0
        self._chain_id: int | None = None

    def get_block(self, number: int) -> Block:
        if number not in self._blocks:
            self._blocks[number] = self._rpc.get_block(number)
            self.blocks += 1
        return self._blocks[number]

    def get_transaction_receipt(self, transaction_hash: str) -> TransactionReceipt:
        self.receipts += 1
        return self._rpc.get_transaction_receipt(transaction_hash)

    def get_chain_id(self) -> int:
        if self._chain_id is None:
            self._chain_id = self._rpc.get_chain_id()
            self.chain_ids += 1
        return self._chain_id

    def eth_call(self, contract_address: str, call_data: str, block_number: int) -> bytes:
        self.historical_calls += 1
        return self._rpc.eth_call(contract_address, call_data, block_number)

    def counts(self) -> RPCRequestCounts:
        return RPCRequestCounts(
            "direct BlockScope upstream calls; excludes Anvil's internal fork-provider calls",
            self.blocks,
            self.receipts,
            self.chain_ids,
            self.historical_calls,
        )


def _bounded_detail(exc: BaseException, rpc_url: str, limit: int = 500) -> str:
    text = redact_rpc_url_in_text(" ".join(str(exc).split()), rpc_url)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _package_version() -> str:
    try:
        return version("blockscope")
    except PackageNotFoundError:
        return "unknown"


def _git_metadata() -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    commit_value = commit.stdout.strip() if commit.returncode == 0 else None
    dirty = bool(status.stdout.strip()) if status.returncode == 0 else None
    return commit_value, dirty


def software_metadata(anvil_executable: str = "anvil") -> SoftwareMetadata:
    """Collect reproducibility metadata without making Anvil mandatory for scanning."""
    commit, dirty = _git_metadata()
    try:
        _, anvil_version = AnvilFork.require_available(anvil_executable)
    except AnvilUnavailableError:
        anvil_version = None
    return SoftwareMetadata(_package_version(), commit, dirty, anvil_version)


def deterministic_candidate_order(
    candidates: tuple[SandwichCandidate, ...],
) -> tuple[SandwichCandidate, ...]:
    """Order candidates without consulting provenance, model output, or replay outcome."""
    return tuple(
        sorted(
            candidates,
            key=lambda candidate: (
                candidate.front_run.swap.block_number,
                candidate.front_run.swap.transaction_index,
                candidate.front_run.swap.log_index,
                candidate.back_run.swap.transaction_index,
                candidate.back_run.swap.log_index,
            ),
        )
    )


def candidate_stage_selection(
    ordinal: int,
    configuration: EvaluationConfiguration,
) -> tuple[bool, bool]:
    """Apply candidate and EVM caps using only global deterministic ordinal."""
    candidate_selected = ordinal <= configuration.candidate_limit
    evm_selected = candidate_selected and ordinal <= configuration.evm_experiment_limit
    return candidate_selected, evm_selected


def _transaction_reference(swap: Any) -> TransactionReference:
    return TransactionReference(swap.transaction_index, swap.transaction_hash.lower())


def _provenance_state(mathematical: FixedInputCounterfactual) -> ProvenanceState:
    if mathematical.provenance.established:
        return ProvenanceState.CANONICAL
    reason = mathematical.provenance.reason.lower()
    if any(word in reason for word in ("unavailable", "failed", "could not")):
        return ProvenanceState.UNAVAILABLE
    return ProvenanceState.NON_CANONICAL


def _mathematical_state(mathematical: FixedInputCounterfactual) -> MathematicalState:
    if mathematical.victims:
        return MathematicalState.AVAILABLE
    if mathematical.status is CounterfactualStatus.INVALID_INPUT:
        return MathematicalState.INVALID
    return MathematicalState.UNAVAILABLE


def _candidate_record(
    ordinal: int,
    candidate: SandwichCandidate,
    mathematical: FixedInputCounterfactual | None,
    mathematical_seconds: float,
    *,
    selected_for_candidate_analysis: bool,
    selected_for_evm: bool,
) -> CandidateEvaluationRecord:
    provenance = (
        ProvenanceState.NOT_EVALUATED
        if mathematical is None
        else _provenance_state(mathematical)
    )
    math_state = (
        MathematicalState.NOT_EVALUATED
        if mathematical is None
        else _mathematical_state(mathematical)
    )
    failures: list[FailureKind] = []
    details: list[str] = []
    if provenance is ProvenanceState.NON_CANONICAL:
        failures.append(FailureKind.NONCANONICAL_PAIR)
    elif provenance is ProvenanceState.UNAVAILABLE:
        failures.append(FailureKind.METADATA_UNAVAILABLE)
    if mathematical is not None and mathematical.unavailable_reason:
        details.append(mathematical.unavailable_reason[:500])
    math_victims = () if mathematical is None else tuple(
        VictimMathematicalRecord(
            execution.victim.swap.transaction_index,
            execution.observed_input,
            execution.observed_output,
            execution.counterfactual_output,
        )
        for execution in mathematical.victims
    )
    return CandidateEvaluationRecord(
        ordinal,
        selected_for_candidate_analysis,
        selected_for_evm,
        candidate.front_run.swap.block_number,
        candidate.pair_address.lower(),
        _transaction_reference(candidate.front_run.swap),
        tuple(_transaction_reference(victim.swap) for victim in candidate.victims),
        _transaction_reference(candidate.back_run.swap),
        provenance,
        None if mathematical is None else mathematical.provenance.reason,
        math_state,
        math_victims,
        ObservedReplayState.NOT_ATTEMPTED,
        EVMCounterfactualState.NOT_ATTEMPTED,
        None,
        None,
        None,
        None,
        None,
        None,
        None if not math_victims else math_victims[-1].mathematical_output,
        None,
        ComparisonState.UNAVAILABLE,
        None,
        TraceEvaluationState.NOT_ATTEMPTED,
        tuple(failures),
        tuple(details),
        mathematical_seconds,
        0.0,
    )


def _observed_state(execution: CounterfactualEVMExecution) -> ObservedReplayState:
    results = execution.observed.transactions
    if any(
        result.submission_status is not ReplaySubmissionStatus.REPLAYED
        for result in results
    ):
        return ObservedReplayState.SUBMISSION_FAILURE
    if execution.observed.environment.local is None:
        return ObservedReplayState.ENVIRONMENT_FAILURE
    exact = bool(
        execution.observed.prefix_receipt_evidence_exact
        and replay_receipt_semantics_exact(execution.observed.target)
        and replay_pair_execution_exact(execution.observed.target)
    )
    return ObservedReplayState.EXACT if exact else ObservedReplayState.SEMANTIC_MISMATCH


def _record_evm_result(
    record: CandidateEvaluationRecord,
    execution: CounterfactualEVMExecution,
    elapsed: float,
    rpc_url: str,
) -> CandidateEvaluationRecord:
    observed_state = _observed_state(execution)
    pair = execution.pair_difference
    receipt = execution.receipt_difference
    failures = list(record.failure_kinds)
    details = list(record.failure_details)
    if observed_state is not ObservedReplayState.EXACT:
        failures.append(FailureKind.OBSERVED_REPLAY_MISMATCH)
    if not execution.counterfactual_prefix_executed:
        failures.append(FailureKind.PREFIX_REPLAY_FAILURE)
    if receipt.observed_status == 1 and receipt.counterfactual_status == 0:
        failures.append(FailureKind.COUNTERFACTUAL_TARGET_REVERT)
    if not pair.counterfactual_swap_present:
        failures.append(FailureKind.COUNTERFACTUAL_SWAP_ABSENT)
    if pair.model_vs_evm_output_delta not in (None, 0):
        failures.append(FailureKind.MODEL_MISMATCH)
    if failures:
        details.extend(
            _bounded_detail(Exception(detail), rpc_url) for detail in execution.limitations[:3]
        )
    return replace(
        record,
        observed_replay=observed_state,
        evm_counterfactual=(
            EVMCounterfactualState.RELIABLE
            if execution.reliable
            else EVMCounterfactualState.UNRELIABLE
        ),
        observed_victim_status=receipt.observed_status,
        counterfactual_victim_status=receipt.counterfactual_status,
        observed_pair_input=pair.observed_pair_input,
        counterfactual_pair_input=pair.counterfactual_pair_input,
        observed_pair_output=pair.observed_pair_output,
        counterfactual_pair_output=pair.counterfactual_pair_output,
        mathematical_output=pair.mathematical_counterfactual_output,
        math_vs_evm_difference=pair.model_vs_evm_output_delta,
        pair_input_comparison=pair.pair_input_unchanged,
        counterfactual_pair_swap_present=pair.counterfactual_swap_present,
        failure_kinds=tuple(dict.fromkeys(failures)),
        failure_details=tuple(dict.fromkeys(details)),
        evm_seconds=elapsed,
    )


def _record_evm_failure(
    record: CandidateEvaluationRecord,
    exc: BlockScopeError,
    elapsed: float,
    rpc_url: str,
) -> CandidateEvaluationRecord:
    if isinstance(exc, UnsupportedTransactionType):
        kind = FailureKind.UNSUPPORTED_TRANSACTION_TYPE
        observed = ObservedReplayState.UNSUPPORTED_TRANSACTION_TYPE
    elif isinstance(exc, AnvilUnavailableError):
        kind = FailureKind.ANVIL_STARTUP_ERROR
        observed = ObservedReplayState.OTHER_OPERATIONAL_FAILURE
    elif isinstance(exc, RPCError):
        kind = FailureKind.PROVIDER_ERROR
        observed = ObservedReplayState.PROVIDER_FAILURE
    elif isinstance(exc, ReplayRPCError):
        kind = FailureKind.ANVIL_RPC_ERROR
        observed = ObservedReplayState.OTHER_OPERATIONAL_FAILURE
    elif isinstance(exc, ReplayInputError):
        kind = FailureKind.UNSUPPORTED_CASE
        observed = ObservedReplayState.OTHER_OPERATIONAL_FAILURE
    else:
        kind = FailureKind.OTHER_OPERATIONAL_FAILURE
        observed = ObservedReplayState.OTHER_OPERATIONAL_FAILURE
    return replace(
        record,
        observed_replay=observed,
        evm_counterfactual=EVMCounterfactualState.FAILED,
        failure_kinds=tuple(dict.fromkeys((*record.failure_kinds, kind))),
        failure_details=(*record.failure_details, _bounded_detail(exc, rpc_url)),
        evm_seconds=elapsed,
    )


def _empty_block_failure(
    number: int,
    ingestion_seconds: float,
    exc: BlockScopeError,
    rpc_url: str,
) -> BlockEvaluationRecord:
    return BlockEvaluationRecord(
        block_number=number,
        state=BlockEvaluationState.PROVIDER_FAILURE,
        transaction_count=0,
        receipts_fetched=0,
        swap_count=0,
        unique_pair_count=0,
        swap_pair_addresses=(),
        malformed_swap_logs=0,
        malformed_sync_logs=0,
        swaps_without_reconstructed_reserves=0,
        invalid_reserve_reconstructions=0,
        metadata_lookup_failures=0,
        continuous_pair_segments=0,
        potential_front_legs_considered=0,
        rejected_no_victims=0,
        rejected_no_closing_back_run=0,
        rejected_invalid_leg_sequence=0,
        rejected_closing_sender_mismatch=0,
        rejected_invalid_quote=0,
        rejected_adverse_movement=0,
        rejected_back_run_reversal=0,
        duplicate_candidates_suppressed=0,
        strict_candidates_found=0,
        block_ingestion_seconds=ingestion_seconds,
        receipt_v2_analysis_seconds=0.0,
        candidate_detection_seconds=0.0,
        failure_kind=FailureKind.PROVIDER_ERROR,
        failure_detail=_bounded_detail(exc, rpc_url),
    )


def _complete_block_record(
    block: Block,
    swaps: BlockSwapAnalysis,
    detection: Any,
    receipts_fetched: int,
    ingestion_seconds: float,
    analysis_seconds: float,
    detection_seconds: float,
) -> BlockEvaluationRecord:
    swap_diagnostics = swaps.diagnostics
    diagnostics = detection.diagnostics
    return BlockEvaluationRecord(
        block.number,
        BlockEvaluationState.COMPLETE,
        len(block.transactions),
        receipts_fetched,
        len(swaps.swaps),
        len({item.swap.pair_address.lower() for item in swaps.swaps}),
        tuple(sorted({item.swap.pair_address.lower() for item in swaps.swaps})),
        swap_diagnostics.malformed_swap_logs,
        swap_diagnostics.malformed_sync_logs,
        swap_diagnostics.swaps_without_reconstructed_reserves,
        swap_diagnostics.invalid_reserve_reconstructions,
        swap_diagnostics.metadata_lookup_failures,
        diagnostics.continuous_pair_segments,
        diagnostics.potential_front_legs_considered,
        diagnostics.rejected_no_victims,
        diagnostics.rejected_no_closing_back_run,
        diagnostics.rejected_invalid_leg_sequence,
        diagnostics.rejected_closing_sender_mismatch,
        diagnostics.rejected_invalid_quote,
        diagnostics.rejected_adverse_movement,
        diagnostics.rejected_back_run_reversal,
        diagnostics.duplicate_candidates_suppressed,
        diagnostics.strict_candidates_found,
        ingestion_seconds,
        analysis_seconds,
        detection_seconds,
    )


def _timing(stage: str, values: list[float]) -> StageTiming:
    return StageTiming(
        stage,
        len(values),
        sum(values),
        None if not values else statistics.fmean(values),
        None if not values else statistics.median(values),
    )


def aggregate_metrics(
    blocks: tuple[BlockEvaluationRecord, ...],
    candidates: tuple[CandidateEvaluationRecord, ...],
) -> tuple[EvaluationMetrics, tuple[FailureCount, ...], tuple[StageTiming, ...]]:
    """Aggregate exact counts and denominators from serialized record semantics."""
    attempted = tuple(
        candidate
        for candidate in candidates
        if candidate.observed_replay is not ObservedReplayState.NOT_ATTEMPTED
    )
    evm_attempted = tuple(candidate for candidate in candidates if candidate.selected_for_evm)
    comparisons = tuple(
        candidate
        for candidate in candidates
        if candidate.math_vs_evm_difference is not None
    )
    unique_pairs = {
        pair_address for block in blocks for pair_address in block.swap_pair_addresses
    }
    metrics = EvaluationMetrics(
        blocks_inspected=len(blocks),
        blocks_completed=sum(block.state is BlockEvaluationState.COMPLETE for block in blocks),
        transactions_inspected=sum(block.transaction_count for block in blocks),
        receipts_fetched=sum(block.receipts_fetched for block in blocks),
        v2_compatible_swaps=sum(block.swap_count for block in blocks),
        unique_pairs=len(unique_pairs),
        malformed_swap_logs=sum(block.malformed_swap_logs for block in blocks),
        malformed_sync_logs=sum(block.malformed_sync_logs for block in blocks),
        swaps_without_reconstructed_reserves=sum(
            block.swaps_without_reconstructed_reserves for block in blocks
        ),
        invalid_reserve_reconstructions=sum(
            block.invalid_reserve_reconstructions for block in blocks
        ),
        metadata_lookup_failures=sum(block.metadata_lookup_failures for block in blocks),
        strict_candidates=len(candidates),
        detector_potential_front_legs=sum(
            block.potential_front_legs_considered for block in blocks
        ),
        detector_rejected_no_victims=sum(block.rejected_no_victims for block in blocks),
        detector_rejected_no_closing_back_run=sum(
            block.rejected_no_closing_back_run for block in blocks
        ),
        detector_rejected_invalid_leg_sequence=sum(
            block.rejected_invalid_leg_sequence for block in blocks
        ),
        detector_rejected_closing_sender_mismatch=sum(
            block.rejected_closing_sender_mismatch for block in blocks
        ),
        detector_rejected_invalid_quote=sum(
            block.rejected_invalid_quote for block in blocks
        ),
        detector_rejected_adverse_movement=sum(
            block.rejected_adverse_movement for block in blocks
        ),
        detector_rejected_back_run_reversal=sum(
            block.rejected_back_run_reversal for block in blocks
        ),
        single_victim_candidates=sum(candidate.victim_count == 1 for candidate in candidates),
        multi_victim_candidates=sum(candidate.victim_count > 1 for candidate in candidates),
        canonical_candidates=sum(
            candidate.provenance is ProvenanceState.CANONICAL for candidate in candidates
        ),
        noncanonical_candidates=sum(
            candidate.provenance is ProvenanceState.NON_CANONICAL for candidate in candidates
        ),
        provenance_unavailable_candidates=sum(
            candidate.provenance is ProvenanceState.UNAVAILABLE for candidate in candidates
        ),
        candidate_analysis_not_evaluated=sum(
            candidate.provenance is ProvenanceState.NOT_EVALUATED for candidate in candidates
        ),
        observed_replay_attempted=len(attempted),
        observed_replay_exact=sum(
            candidate.observed_replay is ObservedReplayState.EXACT for candidate in attempted
        ),
        observed_replay_semantic_mismatches=sum(
            candidate.observed_replay is ObservedReplayState.SEMANTIC_MISMATCH
            for candidate in attempted
        ),
        observed_replay_submission_failures=sum(
            candidate.observed_replay is ObservedReplayState.SUBMISSION_FAILURE
            for candidate in attempted
        ),
        observed_replay_environment_failures=sum(
            candidate.observed_replay is ObservedReplayState.ENVIRONMENT_FAILURE
            for candidate in attempted
        ),
        observed_replay_provider_failures=sum(
            candidate.observed_replay is ObservedReplayState.PROVIDER_FAILURE
            for candidate in attempted
        ),
        observed_replay_unsupported_transaction_types=sum(
            candidate.observed_replay is ObservedReplayState.UNSUPPORTED_TRANSACTION_TYPE
            for candidate in attempted
        ),
        observed_replay_other_failures=sum(
            candidate.observed_replay is ObservedReplayState.OTHER_OPERATIONAL_FAILURE
            for candidate in attempted
        ),
        evm_counterfactual_attempted=len(evm_attempted),
        evm_counterfactual_reliable=sum(
            candidate.evm_counterfactual is EVMCounterfactualState.RELIABLE
            for candidate in evm_attempted
        ),
        target_success_to_success=sum(
            candidate.observed_victim_status == 1
            and candidate.counterfactual_victim_status == 1
            for candidate in evm_attempted
        ),
        target_success_to_revert=sum(
            candidate.observed_victim_status == 1
            and candidate.counterfactual_victim_status == 0
            for candidate in evm_attempted
        ),
        counterfactual_pair_swap_present=sum(
            candidate.counterfactual_pair_swap_present is True
            for candidate in evm_attempted
        ),
        counterfactual_pair_swap_absent=sum(
            candidate.counterfactual_pair_swap_present is False
            for candidate in evm_attempted
        ),
        pair_input_unchanged=sum(
            candidate.pair_input_comparison is ComparisonState.MATCH
            for candidate in evm_attempted
        ),
        pair_input_changed=sum(
            candidate.pair_input_comparison is ComparisonState.DIFFER
            for candidate in evm_attempted
        ),
        mathematical_model_eligible=sum(
            candidate.mathematical_state is MathematicalState.AVAILABLE
            for candidate in candidates
        ),
        math_evm_comparisons=len(comparisons),
        math_evm_exact_matches=sum(
            candidate.math_vs_evm_difference == 0 for candidate in comparisons
        ),
        math_evm_nonzero_differences=sum(
            candidate.math_vs_evm_difference != 0 for candidate in comparisons
        ),
        math_model_unavailable=sum(
            candidate.mathematical_state
            in {
                MathematicalState.UNAVAILABLE,
                MathematicalState.INVALID,
                MathematicalState.NOT_EVALUATED,
            }
            for candidate in candidates
        ),
        evm_comparison_unavailable=sum(
            candidate.math_vs_evm_difference is None for candidate in evm_attempted
        ),
    )
    failure_counts: dict[FailureKind, int] = {}
    for block in blocks:
        if block.failure_kind is not None:
            failure_counts[block.failure_kind] = failure_counts.get(block.failure_kind, 0) + 1
    for candidate in candidates:
        for kind in candidate.failure_kinds:
            failure_counts[kind] = failure_counts.get(kind, 0) + 1
    failures = tuple(
        FailureCount(kind, count)
        for kind, count in sorted(
            failure_counts.items(),
            key=lambda item: (-item[1], item[0].value),
        )
    )
    timings = (
        _timing("block_ingestion", [block.block_ingestion_seconds for block in blocks]),
        _timing(
            "receipt_v2_analysis",
            [block.receipt_v2_analysis_seconds for block in blocks],
        ),
        _timing(
            "candidate_detection",
            [block.candidate_detection_seconds for block in blocks],
        ),
        _timing(
            "provenance_and_mathematical_counterfactual",
            [candidate.mathematical_seconds for candidate in candidates if candidate.selected_for_candidate_analysis],
        ),
        _timing(
            "observed_and_counterfactual_evm_combined",
            [candidate.evm_seconds for candidate in candidates if candidate.selected_for_evm],
        ),
        _timing("optional_trace_attribution", []),
    )
    return metrics, failures, timings


def run_evaluation(
    rpc: EthereumRPC,
    upstream_rpc_url: str,
    configuration: EvaluationConfiguration,
    *,
    anvil_executable: str = "anvil",
    progress: Any | None = print,
) -> EvaluationArtifact:
    """Run staged evaluation; record operational failures and preserve invariants."""
    counted = _CountingCachingRPC(rpc)
    blocks: list[BlockEvaluationRecord] = []
    works: list[_CandidateWork] = []
    hardforks: list[HardforkSelection] = []
    ordinal = 0
    for offset, block_number in enumerate(
        range(configuration.start_block, configuration.end_block + 1),
        start=1,
    ):
        started = time.perf_counter()
        try:
            block = counted.get_block(block_number)
        except RPCError as exc:
            blocks.append(
                _empty_block_failure(
                    block_number,
                    time.perf_counter() - started,
                    exc,
                    upstream_rpc_url,
                )
            )
            if progress is not None:
                progress(
                    f"blocks: {offset}/{configuration.block_count} "
                    f"swaps: {sum(item.swap_count for item in blocks)} "
                    f"strict candidates: {len(works)} EVM validations: 0"
                )
            continue
        ingestion_seconds = time.perf_counter() - started
        try:
            hardforks.append(HardforkSelection(block_number, infer_ethereum_hardfork(block)))
        except ReplayInputError:
            hardforks.append(HardforkSelection(block_number, "unavailable"))
        receipts_before = counted.receipts
        started = time.perf_counter()
        try:
            swaps = analyze_block_swaps(counted, block_number)  # type: ignore[arg-type]
        except RPCError as exc:
            blocks.append(
                replace(
                    _empty_block_failure(
                        block_number,
                        ingestion_seconds,
                        exc,
                        upstream_rpc_url,
                    ),
                    transaction_count=len(block.transactions),
                    receipts_fetched=counted.receipts - receipts_before,
                    receipt_v2_analysis_seconds=time.perf_counter() - started,
                )
            )
            continue
        analysis_seconds = time.perf_counter() - started
        receipts_fetched = counted.receipts - receipts_before
        started = time.perf_counter()
        detection = detect_strict_sandwich_candidates(swaps.swaps)
        detection_seconds = time.perf_counter() - started
        ordered = deterministic_candidate_order(detection.candidates)
        block_math_limit = max(configuration.candidate_limit - ordinal, 0)
        selected = ordered[:block_math_limit]
        started = time.perf_counter()
        mathematical_results = analyze_fixed_input_counterfactuals(
            counted,  # type: ignore[arg-type]
            block_number,
            selected,
        ).candidates
        math_elapsed = time.perf_counter() - started
        per_candidate_math = 0.0 if not selected else math_elapsed / len(selected)
        math_by_identity = {
            (
                item.candidate.front_run.swap.transaction_index,
                item.candidate.front_run.swap.log_index,
            ): item
            for item in mathematical_results
        }
        for candidate in ordered:
            ordinal += 1
            selected_for_analysis, selected_for_evm = candidate_stage_selection(
                ordinal,
                configuration,
            )
            mathematical = math_by_identity.get(
                (
                    candidate.front_run.swap.transaction_index,
                    candidate.front_run.swap.log_index,
                )
            )
            record = _candidate_record(
                ordinal,
                candidate,
                mathematical,
                per_candidate_math if mathematical is not None else 0.0,
                selected_for_candidate_analysis=selected_for_analysis,
                selected_for_evm=selected_for_evm,
            )
            works.append(_CandidateWork(record, candidate, mathematical, block, swaps.receipts))
        blocks.append(
            _complete_block_record(
                block,
                swaps,
                detection,
                receipts_fetched,
                ingestion_seconds,
                analysis_seconds,
                detection_seconds,
            )
        )
        if progress is not None and (
            offset == configuration.block_count or offset % 10 == 0 or ordered
        ):
            progress(
                f"blocks: {offset}/{configuration.block_count} "
                f"swaps: {sum(item.swap_count for item in blocks)} "
                f"strict candidates: {len(works)} EVM validations: 0"
            )

    selected_evm_count = sum(work.record.selected_for_evm for work in works)
    remaining_cooldown = (
        configuration.evm_cooldown_seconds if selected_evm_count else 0
    )
    while remaining_cooldown > 0:
        if progress is not None:
            progress(
                "EVM-stage provider cooldown: "
                f"{remaining_cooldown} second(s) remaining"
            )
        interval = min(30, remaining_cooldown)
        time.sleep(interval)
        remaining_cooldown -= interval

    evm_completed = 0
    for work in works:
        if not work.record.selected_for_evm:
            continue
        started = time.perf_counter()
        try:
            execution = execute_front_omission_counterfactual(
                counted,  # type: ignore[arg-type]
                upstream_rpc_url,
                work.block,
                work.receipts,
                work.candidate,
                work.mathematical,
                anvil_executable=anvil_executable,
            )
        except (
            UnsupportedTransactionType,
            AnvilUnavailableError,
            RPCError,
            ReplayRPCError,
            ReplayInputError,
        ) as exc:
            work.record = _record_evm_failure(
                work.record,
                exc,
                time.perf_counter() - started,
                upstream_rpc_url,
            )
        else:
            work.record = _record_evm_result(
                work.record,
                execution,
                time.perf_counter() - started,
                upstream_rpc_url,
            )
        evm_completed += 1
        if progress is not None:
            progress(
                f"blocks: {len(blocks)}/{configuration.block_count} "
                f"swaps: {sum(item.swap_count for item in blocks)} "
                f"strict candidates: {len(works)} "
                f"EVM validations: {evm_completed}/{selected_evm_count}"
            )

    candidate_records = tuple(work.record for work in works)
    metrics, failures, timings = aggregate_metrics(tuple(blocks), candidate_records)
    return EvaluationArtifact(
        1,
        datetime.now(UTC).isoformat(),
        configuration,
        software_metadata(anvil_executable),
        tuple(hardforks),
        counted.counts(),
        metrics,
        failures,
        timings,
        tuple(blocks),
        candidate_records,
    )


def write_evaluation_artifact(artifact: EvaluationArtifact, path: Path) -> None:
    """Atomically write deterministic, sorted, credential-free JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(artifact.to_dict(), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _count_line(numerator: int, denominator: int) -> str:
    return f"{numerator}/{denominator}"


def render_evaluation_report(artifact: EvaluationArtifact) -> str:
    """Render exact corpus counts and only interesting outcomes that exist."""
    config = artifact.configuration
    metrics = artifact.metrics
    timings = {timing.stage: timing for timing in artifact.timings}
    evm_timing = timings["observed_and_counterfactual_evm_combined"]
    lines = [
        "BlockScope Corpus Evaluation",
        "",
        f"Range: {config.start_block}–{config.end_block} ({config.block_count} blocks)",
        f"Blocks inspected/completed: {metrics.blocks_inspected}/{metrics.blocks_completed}",
        f"Transactions inspected: {metrics.transactions_inspected}",
        f"Receipts fetched: {metrics.receipts_fetched}",
        f"V2-compatible swaps: {metrics.v2_compatible_swaps}",
        (
            "Malformed Swap/Sync logs: "
            f"{metrics.malformed_swap_logs}/{metrics.malformed_sync_logs}"
        ),
        f"Invalid reserve reconstructions: {metrics.invalid_reserve_reconstructions}",
        f"Metadata lookup failures: {metrics.metadata_lookup_failures}",
        f"Strict candidates: {metrics.strict_candidates}",
        (
            "Single-victim / multi-victim: "
            f"{metrics.single_victim_candidates}/{metrics.multi_victim_candidates}"
        ),
        (
            "Canonical / noncanonical / provenance unavailable: "
            f"{metrics.canonical_candidates}/{metrics.noncanonical_candidates}/"
            f"{metrics.provenance_unavailable_candidates}"
        ),
        f"Candidate analyses not evaluated due to limit: {metrics.candidate_analysis_not_evaluated}",
        "",
        "Observed EVM validation",
        (
            "  exact: "
            + _count_line(metrics.observed_replay_exact, metrics.observed_replay_attempted)
        ),
        f"  semantic mismatch: {metrics.observed_replay_semantic_mismatches}",
        f"  submission failure: {metrics.observed_replay_submission_failures}",
        f"  operational failures: {metrics.observed_replay_provider_failures + metrics.observed_replay_environment_failures + metrics.observed_replay_unsupported_transaction_types + metrics.observed_replay_other_failures}",
        "",
        "Front-omitted EVM",
        (
            "  reliable: "
            + _count_line(
                metrics.evm_counterfactual_reliable,
                metrics.evm_counterfactual_attempted,
            )
        ),
        f"  victim success -> revert: {metrics.target_success_to_revert}",
        f"  pair input unchanged / changed: {metrics.pair_input_unchanged}/{metrics.pair_input_changed}",
        f"  pair Swap present / absent: {metrics.counterfactual_pair_swap_present}/{metrics.counterfactual_pair_swap_absent}",
        "",
        "Math vs EVM",
        f"  mathematical eligibility: {metrics.mathematical_model_eligible}",
        (
            "  exact integer agreement: "
            + _count_line(metrics.math_evm_exact_matches, metrics.math_evm_comparisons)
        ),
        f"  nonzero differences: {metrics.math_evm_nonzero_differences}",
        "",
        (
            "Combined observed/counterfactual EVM runtime: "
            f"mean={evm_timing.mean_seconds} s median={evm_timing.median_seconds} s"
        ),
    ]
    if artifact.failure_taxonomy:
        lines.extend(("", "Failure taxonomy"))
        lines.extend(
            f"  {failure.kind.value}: {failure.count}"
            for failure in artifact.failure_taxonomy
        )
    interesting: list[tuple[str, CandidateEvaluationRecord]] = []
    predicates = (
        ("exact successful validation", lambda item: item.evm_counterfactual is EVMCounterfactualState.RELIABLE),
        ("model unavailable", lambda item: item.mathematical_state is MathematicalState.UNAVAILABLE),
        ("observed replay mismatch", lambda item: item.observed_replay is ObservedReplayState.SEMANTIC_MISMATCH),
        ("counterfactual revert", lambda item: item.counterfactual_victim_status == 0),
        ("math/EVM mismatch", lambda item: item.math_vs_evm_difference not in (None, 0)),
        ("multi-victim candidate", lambda item: item.victim_count > 1),
    )
    used: set[int] = set()
    for label, predicate in predicates:
        match = next((item for item in artifact.candidates if predicate(item)), None)
        if match is not None and match.ordinal not in used:
            interesting.append((label, match))
            used.add(match.ordinal)
    if interesting:
        lines.extend(("", "Interesting cases", "  outcome | candidate | block | pair"))
        lines.extend(
            f"  {label} | {item.ordinal} | {item.block_number} | {item.pair_address}"
            for label, item in interesting
        )
    return "\n".join(lines)
