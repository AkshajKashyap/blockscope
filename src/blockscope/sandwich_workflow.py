"""Application orchestration for the existing sandwich-analysis CLI workflow."""

from dataclasses import dataclass

from blockscope.counterfactual import (
    CounterfactualAnalysis,
    FixedInputCounterfactual,
    analyze_fixed_input_counterfactuals,
)
from blockscope.economics import (
    ObservedEconomicsAnalysis,
    ObservedSandwichEconomics,
    analyze_observed_sandwich_economics,
)
from blockscope.evm_counterfactual import (
    CounterfactualEVMExecution,
    execute_front_omission_counterfactual,
)
from blockscope.observed_attribution import (
    ObservedCycleAttribution,
    execute_observed_cycle_attribution,
)
from blockscope.observed_trace import (
    ObservedCandidateTraceAttribution,
    execute_observed_trace_attribution,
)
from blockscope.rpc import EthereumRPC
from blockscope.sandwiches import (
    SandwichCandidate,
    SandwichScanResult,
    detect_strict_sandwich_candidates,
)
from blockscope.uniswap_v2 import BlockSwapAnalysis, analyze_block_swaps


@dataclass(frozen=True, slots=True)
class SandwichWorkflowOptions:
    """Existing optional analyses requested for one sandwich command."""

    limit: int
    economics: bool = False
    mathematical_counterfactual: bool = False
    evm_counterfactual: bool = False
    flows: bool = False
    trace_flows: bool = False


@dataclass(frozen=True, slots=True)
class CandidateAnalysis:
    """Index-aligned evidence for one displayed strict candidate."""

    candidate: SandwichCandidate
    economics: ObservedSandwichEconomics | None
    mathematical_counterfactual: FixedInputCounterfactual | None
    evm_counterfactual: CounterfactualEVMExecution | None
    attribution: ObservedCycleAttribution | None
    trace_attribution: ObservedCandidateTraceAttribution | None = None


@dataclass(frozen=True, slots=True)
class SandwichWorkflowResult:
    """Aggregate diagnostics plus candidate-aligned optional evidence."""

    swaps: BlockSwapAnalysis
    sandwiches: SandwichScanResult
    economics: ObservedEconomicsAnalysis | None
    mathematical_counterfactuals: CounterfactualAnalysis | None
    candidates: tuple[CandidateAnalysis, ...]


def analyze_sandwich_workflow(
    rpc: EthereumRPC,
    upstream_rpc_url: str | None,
    block_number: int,
    options: SandwichWorkflowOptions,
) -> SandwichWorkflowResult:
    """Run the established analyses once and align evidence by candidate position."""
    if options.limit < 0:
        raise ValueError("candidate display limit must be non-negative")
    needs_fork = options.evm_counterfactual or options.flows or options.trace_flows
    if needs_fork and upstream_rpc_url is None:
        raise ValueError("fork-backed sandwich analysis requires an upstream RPC URL")

    swaps = analyze_block_swaps(rpc, block_number)
    sandwiches = detect_strict_sandwich_candidates(swaps.swaps)
    needs_economics = (
        options.economics
        or options.mathematical_counterfactual
        or options.evm_counterfactual
        or options.flows
        or options.trace_flows
    )
    economics = (
        analyze_observed_sandwich_economics(sandwiches.candidates, swaps.receipts)
        if needs_economics
        else None
    )
    mathematical = (
        analyze_fixed_input_counterfactuals(rpc, block_number, sandwiches.candidates)
        if options.mathematical_counterfactual or options.evm_counterfactual
        else None
    )
    block = rpc.get_block(block_number) if needs_fork else None

    aligned: list[CandidateAnalysis] = []
    for index, candidate in enumerate(sandwiches.candidates[: options.limit]):
        candidate_economics = None if economics is None else economics.candidates[index]
        candidate_mathematical = (
            None if mathematical is None else mathematical.candidates[index]
        )
        evm_result = (
            execute_front_omission_counterfactual(
                rpc,
                upstream_rpc_url,
                block,
                swaps.receipts,
                candidate,
                candidate_mathematical,
            )
            if options.evm_counterfactual
            and upstream_rpc_url is not None
            and block is not None
            else None
        )
        attribution = (
            execute_observed_cycle_attribution(
                rpc,
                upstream_rpc_url,
                block,
                swaps.receipts,
                candidate,
                candidate_economics,
            )
            if (options.flows or options.trace_flows)
            and upstream_rpc_url is not None
            and block is not None
            and candidate_economics is not None
            else None
        )
        trace_attribution = (
            execute_observed_trace_attribution(
                rpc,
                upstream_rpc_url,
                block,
                swaps.receipts,
                candidate,
                attribution,
            )
            if options.trace_flows
            and upstream_rpc_url is not None
            and block is not None
            and attribution is not None
            else None
        )
        aligned.append(
            CandidateAnalysis(
                candidate,
                candidate_economics,
                candidate_mathematical,
                evm_result,
                attribution,
                trace_attribution,
            )
        )
    return SandwichWorkflowResult(
        swaps,
        sandwiches,
        economics,
        mathematical,
        tuple(aligned),
    )
