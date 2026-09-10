"""Front-omitted execution of unchanged victims on independent Anvil forks."""

import hashlib
from dataclasses import dataclass

from blockscope.counterfactual import FixedInputCounterfactual
from blockscope.replay import (
    AnvilFork,
    ComparisonState,
    ObservedReplayReport,
    PairSwapEvidence,
    ReplayBlockContext,
    ReplayBranchExecution,
    ReplayInputError,
    ReplayPlan,
    ReplaySubmissionStatus,
    SemanticLog,
    TransactionReplayResult,
    assemble_observed_replay_report,
    execute_replay_plan,
    infer_ethereum_hardfork,
    pair_swap_evidence,
    plan_observed_replay,
    semantic_receipt_logs,
)
from blockscope.rpc import EthereumRPC
from blockscope.sandwiches import TOKEN0_TO_TOKEN1, TOKEN1_TO_TOKEN0, SandwichCandidate
from blockscope.types import Block, Transaction, TransactionReceipt
from blockscope.uniswap_v2 import ReserveState, scan_receipt_swap_evidence


@dataclass(frozen=True, slots=True)
class FrontOmissionPlan:
    """Observed and single-omission histories ending at the same target."""

    observed: ReplayPlan
    counterfactual: ReplayPlan
    omitted_transaction: Transaction

    @property
    def omitted_transaction_index(self) -> int:
        return self.omitted_transaction.transaction_index


def plan_front_omission(
    block: Block,
    omitted_transaction_index: int,
    target_transaction_index: int,
) -> FrontOmissionPlan:
    """Remove exactly one complete prefix transaction while retaining canonical order."""
    if omitted_transaction_index < 0:
        raise ReplayInputError("omitted transaction index must be non-negative")
    if target_transaction_index < 0:
        raise ReplayInputError("target transaction index must be non-negative")
    if omitted_transaction_index >= target_transaction_index:
        raise ReplayInputError("omitted transaction index must be before the target")
    observed = plan_observed_replay(block, target_transaction_index)
    by_index = {
        transaction.transaction_index: transaction for transaction in observed.prefix
    }
    if omitted_transaction_index not in by_index:
        raise ReplayInputError(
            f"transaction #{omitted_transaction_index} is not present in the target prefix"
        )
    omitted = by_index[omitted_transaction_index]
    counterfactual = ReplayPlan(
        block_number=observed.block_number,
        fork_block_number=observed.fork_block_number,
        target_transaction_index=observed.target_transaction_index,
        prefix=tuple(
            transaction
            for transaction in observed.prefix
            if transaction.transaction_index != omitted_transaction_index
        ),
        target=observed.target,
    )
    return FrontOmissionPlan(observed, counterfactual, omitted)


@dataclass(frozen=True, slots=True)
class ExperimentalIsolationEvidence:
    """Auditable equality claims and the experiment's one deliberate difference."""

    upstream_rpc_fingerprint: str
    observed_fork_instance_id: str
    counterfactual_fork_instance_id: str
    observed_process_id: int | None
    counterfactual_process_id: int | None
    independent_forks: bool
    same_fork_block: bool
    same_anvil_version: bool
    same_hardfork: bool
    same_requested_block_context: bool
    controllable_local_environment_equal: bool
    uncontrolled_local_differences: tuple[str, ...]
    same_target_request: bool
    same_non_omitted_prefix_requests: bool
    omitted_transaction_absent: bool
    intentional_difference: str


@dataclass(frozen=True, slots=True)
class CounterfactualReceiptDifference:
    """Focused observed-versus-altered target receipt differences."""

    observed_status: int | None
    counterfactual_status: int | None
    status_changed: bool | None
    observed_gas_used: int
    counterfactual_gas_used: int | None
    gas_used_delta: int | None
    observed_effective_gas_price: int | None
    counterfactual_effective_gas_price: int | None
    effective_gas_price_delta: int | None
    observed_log_count: int
    counterfactual_log_count: int | None
    log_count_delta: int | None
    log_content: ComparisonState
    observed_logs: tuple[SemanticLog, ...]
    counterfactual_logs: tuple[SemanticLog, ...]


@dataclass(frozen=True, slots=True)
class CounterfactualPairDifference:
    """Candidate-pair outcome from observed and front-omitted EVM execution."""

    candidate_pair_address: str
    candidate_pair_event_ordinal: int
    observed: PairSwapEvidence | None
    counterfactual: PairSwapEvidence | None
    counterfactual_swap_present: bool
    direction: ComparisonState
    pair_input_unchanged: ComparisonState
    observed_pair_input: int | None
    counterfactual_pair_input: int | None
    observed_pair_output: int | None
    counterfactual_pair_output: int | None
    evm_pair_output_delta: int | None
    observed_post_reserves: ReserveState | None
    counterfactual_post_reserves: ReserveState | None
    post_reserves: ComparisonState
    mathematical_counterfactual_output: int | None
    model_vs_evm: ComparisonState
    model_vs_evm_output_delta: int | None


@dataclass(frozen=True, slots=True)
class CounterfactualEVMExecution:
    """Two independently forked worlds and their exact target comparison."""

    candidate: SandwichCandidate
    plan: FrontOmissionPlan
    observed: ObservedReplayReport
    counterfactual_branch: ReplayBranchExecution
    isolation: ExperimentalIsolationEvidence
    receipt_difference: CounterfactualReceiptDifference
    pair_difference: CounterfactualPairDifference
    counterfactual_prefix_executed: bool
    counterfactual_target_executed: bool
    reliable: bool
    limitations: tuple[str, ...]


def _receipts_for_plan(
    plan: ReplayPlan,
    receipts: tuple[TransactionReceipt, ...],
) -> tuple[TransactionReceipt, ...]:
    by_identity = {
        (receipt.transaction_index, receipt.transaction_hash.lower()): receipt
        for receipt in receipts
    }
    selected: list[TransactionReceipt] = []
    for transaction in plan.transactions:
        receipt = by_identity.get((transaction.transaction_index, transaction.hash.lower()))
        if receipt is None:
            raise ReplayInputError(
                f"historical receipt is unavailable for transaction "
                f"#{transaction.transaction_index} {transaction.hash}"
            )
        selected.append(receipt)
    return tuple(selected)


def _candidate_pair_event_ordinal(
    historical_receipt: TransactionReceipt,
    candidate: SandwichCandidate,
) -> int:
    target_swap = candidate.victims[-1].swap
    ordinal = 0
    for context in scan_receipt_swap_evidence(historical_receipt).swaps:
        if context.swap.pair_address.lower() != candidate.pair_address.lower():
            continue
        if context.swap.log_index == target_swap.log_index:
            return ordinal
        ordinal += 1
    raise ReplayInputError(
        "candidate victim Swap could not be located in its historical normalized receipt"
    )


def _select_pair_event(
    receipt: TransactionReceipt | None,
    pair_address: str,
    ordinal: int,
) -> PairSwapEvidence | None:
    if receipt is None:
        return None
    matches = tuple(
        evidence
        for evidence in pair_swap_evidence(receipt)
        if evidence.pair_address.lower() == pair_address.lower()
    )
    return None if ordinal >= len(matches) else matches[ordinal]


def _directional_flow(evidence: PairSwapEvidence) -> tuple[int, int] | None:
    if evidence.direction == TOKEN0_TO_TOKEN1:
        return evidence.amount0_in, evidence.amount1_out
    if evidence.direction == TOKEN1_TO_TOKEN0:
        return evidence.amount1_in, evidence.amount0_out
    return None


def compare_counterfactual_receipts(
    observed: TransactionReceipt,
    counterfactual: TransactionReceipt | None,
) -> CounterfactualReceiptDifference:
    """Describe expected receipt divergence without demanding equality."""
    observed_logs = semantic_receipt_logs(observed)
    if counterfactual is None:
        return CounterfactualReceiptDifference(
            observed.status,
            None,
            None,
            observed.gas_used,
            None,
            None,
            observed.effective_gas_price,
            None,
            None,
            len(observed.logs),
            None,
            None,
            ComparisonState.UNAVAILABLE,
            observed_logs,
            (),
        )
    counterfactual_logs = semantic_receipt_logs(counterfactual)
    price_delta = (
        None
        if observed.effective_gas_price is None
        or counterfactual.effective_gas_price is None
        else counterfactual.effective_gas_price - observed.effective_gas_price
    )
    return CounterfactualReceiptDifference(
        observed_status=observed.status,
        counterfactual_status=counterfactual.status,
        status_changed=observed.status != counterfactual.status,
        observed_gas_used=observed.gas_used,
        counterfactual_gas_used=counterfactual.gas_used,
        gas_used_delta=counterfactual.gas_used - observed.gas_used,
        observed_effective_gas_price=observed.effective_gas_price,
        counterfactual_effective_gas_price=counterfactual.effective_gas_price,
        effective_gas_price_delta=price_delta,
        observed_log_count=len(observed.logs),
        counterfactual_log_count=len(counterfactual.logs),
        log_count_delta=len(counterfactual.logs) - len(observed.logs),
        log_content=(
            ComparisonState.MATCH
            if observed_logs == counterfactual_logs
            else ComparisonState.DIFFER
        ),
        observed_logs=observed_logs,
        counterfactual_logs=counterfactual_logs,
    )


def compare_candidate_pair_outcomes(
    pair_address: str,
    event_ordinal: int,
    observed_receipt: TransactionReceipt | None,
    counterfactual_receipt: TransactionReceipt | None,
    mathematical: FixedInputCounterfactual | None,
    target_transaction_index: int,
) -> CounterfactualPairDifference:
    """Compare one deterministic candidate-pair event across both EVM histories."""
    observed = _select_pair_event(observed_receipt, pair_address, event_ordinal)
    counterfactual = _select_pair_event(
        counterfactual_receipt,
        pair_address,
        event_ordinal,
    )
    direction_state = ComparisonState.UNAVAILABLE
    input_state = ComparisonState.UNAVAILABLE
    reserve_state = ComparisonState.UNAVAILABLE
    observed_input: int | None = None
    counterfactual_input: int | None = None
    observed_output: int | None = None
    counterfactual_output: int | None = None
    output_delta: int | None = None
    if observed is not None and counterfactual is not None:
        direction_state = (
            ComparisonState.MATCH
            if observed.direction == counterfactual.direction
            else ComparisonState.DIFFER
        )
        observed_flow = _directional_flow(observed)
        counterfactual_flow = _directional_flow(counterfactual)
        if direction_state is ComparisonState.MATCH and observed_flow and counterfactual_flow:
            observed_input, observed_output = observed_flow
            counterfactual_input, counterfactual_output = counterfactual_flow
            input_state = (
                ComparisonState.MATCH
                if observed_input == counterfactual_input
                else ComparisonState.DIFFER
            )
            output_delta = counterfactual_output - observed_output
        reserve_state = (
            ComparisonState.MATCH
            if observed.post_reserves is not None
            and observed.post_reserves == counterfactual.post_reserves
            else ComparisonState.DIFFER
        )
    elif observed is not None:
        observed_flow = _directional_flow(observed)
        if observed_flow is not None:
            observed_input, observed_output = observed_flow

    mathematical_output: int | None = None
    if mathematical is not None:
        for execution in mathematical.victims:
            if execution.victim.swap.transaction_index == target_transaction_index:
                mathematical_output = execution.counterfactual_output
                break
    if mathematical_output is None or counterfactual_output is None:
        model_state = ComparisonState.UNAVAILABLE
        model_delta = None
    else:
        model_delta = counterfactual_output - mathematical_output
        model_state = (
            ComparisonState.MATCH if model_delta == 0 else ComparisonState.DIFFER
        )
    return CounterfactualPairDifference(
        candidate_pair_address=pair_address.lower(),
        candidate_pair_event_ordinal=event_ordinal,
        observed=observed,
        counterfactual=counterfactual,
        counterfactual_swap_present=counterfactual is not None,
        direction=direction_state,
        pair_input_unchanged=input_state,
        observed_pair_input=observed_input,
        counterfactual_pair_input=counterfactual_input,
        observed_pair_output=observed_output,
        counterfactual_pair_output=counterfactual_output,
        evm_pair_output_delta=output_delta,
        observed_post_reserves=None if observed is None else observed.post_reserves,
        counterfactual_post_reserves=(
            None if counterfactual is None else counterfactual.post_reserves
        ),
        post_reserves=reserve_state,
        mathematical_counterfactual_output=mathematical_output,
        model_vs_evm=model_state,
        model_vs_evm_output_delta=model_delta,
    )


def _result_requests(
    results: tuple[TransactionReplayResult, ...],
) -> dict[int, object]:
    return {
        result.historical_transaction.transaction_index: result.replay_request
        for result in results
    }


def _environment_isolation(
    upstream_rpc_url: str,
    plan: FrontOmissionPlan,
    observed: ReplayBranchExecution,
    counterfactual: ReplayBranchExecution,
) -> ExperimentalIsolationEvidence:
    observed_requests = _result_requests(observed.transactions)
    counterfactual_requests = _result_requests(counterfactual.transactions)
    non_omitted_indexes = tuple(
        transaction.transaction_index for transaction in plan.counterfactual.prefix
    )
    same_prefix = all(
        observed_requests.get(index) == counterfactual_requests.get(index)
        for index in non_omitted_indexes
    )
    target_index = plan.observed.target_transaction_index
    same_target = observed_requests.get(target_index) == counterfactual_requests.get(target_index)
    omitted_absent = plan.omitted_transaction_index not in counterfactual_requests
    controllable = (
        "number",
        "timestamp",
        "base_fee_per_gas",
        "gas_limit",
        "coinbase",
        "chain_id",
    )
    observed_local = observed.environment.local
    counterfactual_local = counterfactual.environment.local
    controllable_equal = bool(
        observed_local is not None
        and counterfactual_local is not None
        and all(
            getattr(observed_local, name) == getattr(counterfactual_local, name)
            for name in controllable
        )
    )
    uncontrolled_differences: list[str] = []
    if observed_local is None or counterfactual_local is None:
        uncontrolled_differences.extend(("prevrandao", "difficulty"))
    else:
        for name in ("prevrandao", "difficulty"):
            if getattr(observed_local, name) != getattr(counterfactual_local, name):
                uncontrolled_differences.append(name)
    return ExperimentalIsolationEvidence(
        upstream_rpc_fingerprint=hashlib.sha256(upstream_rpc_url.encode()).hexdigest()[:16],
        observed_fork_instance_id=observed.fork_instance_id,
        counterfactual_fork_instance_id=counterfactual.fork_instance_id,
        observed_process_id=observed.backend_process_id,
        counterfactual_process_id=counterfactual.backend_process_id,
        independent_forks=(
            observed.fork_instance_id != counterfactual.fork_instance_id
            and observed.backend_process_id is not None
            and counterfactual.backend_process_id is not None
            and observed.backend_process_id != counterfactual.backend_process_id
        ),
        same_fork_block=(
            observed.plan.fork_block_number == counterfactual.plan.fork_block_number
        ),
        same_anvil_version=observed.backend_version == counterfactual.backend_version,
        same_hardfork=observed.configured_hardfork == counterfactual.configured_hardfork,
        same_requested_block_context=(
            observed.environment.historical == counterfactual.environment.historical
        ),
        controllable_local_environment_equal=controllable_equal,
        uncontrolled_local_differences=tuple(uncontrolled_differences),
        same_target_request=same_target,
        same_non_omitted_prefix_requests=same_prefix,
        omitted_transaction_absent=omitted_absent,
        intentional_difference=(
            f"entire historical transaction #{plan.omitted_transaction_index} omitted before "
            f"unchanged target #{target_index}"
        ),
    )


def _branch_execution_complete(results: tuple[TransactionReplayResult, ...]) -> bool:
    return all(
        result.submission_status is ReplaySubmissionStatus.REPLAYED
        and result.replay_receipt is not None
        for result in results
    )


def execute_front_omission_counterfactual(
    upstream_rpc: EthereumRPC,
    upstream_rpc_url: str,
    block: Block,
    historical_receipts: tuple[TransactionReceipt, ...],
    candidate: SandwichCandidate,
    mathematical: FixedInputCounterfactual | None = None,
    *,
    anvil_executable: str = "anvil",
) -> CounterfactualEVMExecution:
    """Execute observed and front-omitted histories on two fresh Anvil processes."""
    if not candidate.victims:
        raise ReplayInputError("candidate has no victim transaction")
    front_index = candidate.front_run.swap.transaction_index
    target_index = candidate.victims[-1].swap.transaction_index
    plan = plan_front_omission(block, front_index, target_index)
    executable_path, _ = AnvilFork.require_available(anvil_executable)
    chain_id = upstream_rpc.get_chain_id()
    if chain_id != 1:
        raise ReplayInputError(
            f"front-omitted Ethereum replay requires mainnet chain ID 1; got {chain_id}"
        )
    hardfork = infer_ethereum_hardfork(block)
    historical_context = ReplayBlockContext.from_block(block, chain_id)
    observed_receipts = _receipts_for_plan(plan.observed, historical_receipts)
    counterfactual_receipts = _receipts_for_plan(
        plan.counterfactual,
        historical_receipts,
    )

    observed_branch = execute_replay_plan(
        upstream_rpc_url,
        plan.observed,
        observed_receipts,
        historical_context,
        hardfork,
        anvil_executable=executable_path,
    )
    counterfactual_branch = execute_replay_plan(
        upstream_rpc_url,
        plan.counterfactual,
        counterfactual_receipts,
        historical_context,
        hardfork,
        anvil_executable=executable_path,
    )
    observed_report = assemble_observed_replay_report(
        plan.observed,
        observed_branch.backend_version,
        hardfork,
        observed_branch.environment,
        observed_branch.transactions,
    )
    isolation = _environment_isolation(
        upstream_rpc_url,
        plan,
        observed_branch,
        counterfactual_branch,
    )
    historical_target_receipt = observed_receipts[-1]
    event_ordinal = _candidate_pair_event_ordinal(historical_target_receipt, candidate)
    observed_target_receipt = observed_branch.target.replay_receipt
    counterfactual_target_receipt = counterfactual_branch.target.replay_receipt
    receipt_difference = compare_counterfactual_receipts(
        historical_target_receipt if observed_target_receipt is None else observed_target_receipt,
        counterfactual_target_receipt,
    )
    pair_difference = compare_candidate_pair_outcomes(
        candidate.pair_address,
        event_ordinal,
        observed_target_receipt,
        counterfactual_target_receipt,
        mathematical,
        target_index,
    )
    counterfactual_prefix_executed = _branch_execution_complete(
        counterfactual_branch.prefix
    )
    counterfactual_target_executed = _branch_execution_complete(
        (counterfactual_branch.target,)
    )
    observed_target_semantics_exact = bool(
        observed_report.target.comparison is not None
        and observed_report.target.comparison.receipt_semantics_exact_match
    )
    reliable = all(
        (
            observed_report.pair_execution_exact_match,
            observed_target_semantics_exact,
            isolation.independent_forks,
            isolation.same_fork_block,
            isolation.same_anvil_version,
            isolation.same_hardfork,
            isolation.same_requested_block_context,
            isolation.controllable_local_environment_equal,
            isolation.same_target_request,
            isolation.same_non_omitted_prefix_requests,
            isolation.omitted_transaction_absent,
            counterfactual_prefix_executed,
            counterfactual_target_executed,
        )
    )
    limitations: list[str] = []
    for branch_name, branch in (
        ("observed", observed_branch),
        ("counterfactual", counterfactual_branch),
    ):
        for warning in branch.environment.setup_warnings:
            limitations.append(f"{branch_name} environment: {warning}")
        if branch.environment.mismatched_fields:
            limitations.append(
                f"{branch_name} historical context mismatches: "
                + ", ".join(branch.environment.mismatched_fields)
            )
    if not observed_report.pair_execution_exact_match:
        limitations.append("observed target did not meet exact V2 reproduction criteria")
    if not observed_target_semantics_exact:
        limitations.append("observed target receipt semantics did not reproduce exactly")
    if not counterfactual_prefix_executed:
        limitations.append("a required non-omitted counterfactual prefix transaction failed")
    if not counterfactual_target_executed:
        limitations.append("counterfactual target was not submitted and mined with a receipt")
    limitations.extend(
        (
            "prevrandao/difficulty differences remain relevant to contracts that inspect them",
            "pair Swap output is not necessarily the victim wallet's received token amount",
            "this experiment establishes execution consequences, not intent or mempool causation",
        )
    )
    return CounterfactualEVMExecution(
        candidate=candidate,
        plan=plan,
        observed=observed_report,
        counterfactual_branch=counterfactual_branch,
        isolation=isolation,
        receipt_difference=receipt_difference,
        pair_difference=pair_difference,
        counterfactual_prefix_executed=counterfactual_prefix_executed,
        counterfactual_target_executed=counterfactual_target_executed,
        reliable=reliable,
        limitations=tuple(limitations),
    )
