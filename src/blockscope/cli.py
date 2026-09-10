"""Command-line interface for BlockScope."""

from datetime import UTC, datetime
from decimal import Decimal, localcontext
from fractions import Fraction
from typing import Annotated

import typer

from blockscope.counterfactual import (
    FixedInputCounterfactual,
    analyze_fixed_input_counterfactuals,
)
from blockscope.economics import (
    ObservedAmount,
    ObservedAsset,
    ObservedSandwichEconomics,
    analyze_observed_sandwich_economics,
)
from blockscope.evm_counterfactual import (
    CounterfactualEVMExecution,
    execute_front_omission_counterfactual,
)
from blockscope.observed_attribution import (
    AddressBalanceDelta,
    AddressCheckpoint,
    ObservedCycleAttribution,
    execute_observed_cycle_attribution,
)
from blockscope.replay import (
    ObservedReplayReport,
    PairSwapEvidence,
    TransactionReplayResult,
    replay_observed_transaction,
)
from blockscope.rpc import BlockScopeError, EthereumRPC, rpc_url_from_env
from blockscope.sandwiches import SandwichCandidate, detect_strict_sandwich_candidates
from blockscope.types import Transaction
from blockscope.uniswap_v2 import (
    EnrichedUniswapV2Swap,
    TokenMetadata,
    analyze_block_swaps,
)

app = typer.Typer(no_args_is_help=True)


@app.callback()
def main() -> None:
    """Inspect Ethereum blocks, MEV evidence, and execution replays."""


def _short(value: str, length: int = 12) -> str:
    return value if len(value) <= length else f"{value[:length]}…"


def _transaction_line(transaction: Transaction) -> str:
    destination = (
        _short(transaction.to_address)
        if transaction.to_address is not None
        else "CONTRACT_CREATION"
    )
    return (
        f"#{transaction.transaction_index:<4} "
        f"{_short(transaction.hash):<13} "
        f"{_short(transaction.from_address)} -> {destination} "
        f"value={transaction.value} wei"
    )


def _token_label(metadata: TokenMetadata | None) -> str:
    if metadata is None:
        return "unavailable"
    symbol = metadata.symbol or "symbol unavailable"
    decimals = "?" if metadata.decimals is None else str(metadata.decimals)
    return f"{symbol} {_short(metadata.address)} decimals={decimals}"


def _swap_lines(enriched: EnrichedUniswapV2Swap) -> tuple[str, ...]:
    swap = enriched.swap
    context = enriched.reserve_context
    if context.pre_reserves is not None and context.post_reserves is not None:
        reserves = (
            f"reserves pre=({context.pre_reserves.reserve0}, {context.pre_reserves.reserve1}) "
            f"post=({context.post_reserves.reserve0}, {context.post_reserves.reserve1})"
        )
    else:
        reserves = "reserves unavailable (no valid immediately preceding same-pair Sync)"
    factory = enriched.pair_metadata.factory_address or "unavailable"
    return (
        f"Tx #{swap.transaction_index} / log {swap.log_index}  Pair: {swap.pair_address}",
        (
            f"  {swap.direction}  in0={swap.amount0_in} in1={swap.amount1_in} "
            f"out0={swap.amount0_out} out1={swap.amount1_out}"
        ),
        f"  {reserves}",
        f"  factory={factory}  token0={_token_label(enriched.token0_metadata)}",
        f"  token1={_token_label(enriched.token1_metadata)}",
    )


def _fraction_decimal(value: Fraction) -> str:
    with localcontext() as context:
        context.prec = 18
        return format(Decimal(value.numerator) / Decimal(value.denominator), ".12g")


def _candidate_lines(index: int, candidate: SandwichCandidate) -> tuple[str, ...]:
    victim_lines = tuple(
        f"  Victim {victim_index}: tx #{victim.swap.transaction_index} "
        f"sender={victim.transaction_sender} {victim.swap.direction}"
        for victim_index, victim in enumerate(candidate.victims, start=1)
    )
    return (
        f"Candidate {index}",
        f"  Pair: {candidate.pair_address}",
        f"  Outer transaction sender: {candidate.actor_address}",
        f"  Front: tx #{candidate.front_run.swap.transaction_index} {candidate.direction}",
        *victim_lines,
        (
            f"  Back: tx #{candidate.back_run.swap.transaction_index} "
            f"{candidate.back_run.swap.direction}"
        ),
        "  Raw directional quote:",
        f"    before front: {_fraction_decimal(candidate.quote_before_front)}",
        f"    after front:  {_fraction_decimal(candidate.quote_after_front)}",
        f"    before back:  {_fraction_decimal(candidate.quote_before_back)}",
        f"    after back:   {_fraction_decimal(candidate.quote_after_back)}",
        (
            "  Evidence: continuous pair state; same outer transaction sender; "
            "distinct victim sender(s); adverse front movement; reversing back movement"
        ),
    )


def _asset_label(asset: ObservedAsset) -> str:
    identity = asset.symbol or asset.pair_position
    address = asset.address or "address unavailable"
    decimals = "?" if asset.decimals is None else str(asset.decimals)
    return f"{identity} ({address}, decimals={decimals})"


def _observed_amount(amount: ObservedAmount, *, signed: bool = False) -> str:
    raw = f"{amount.raw_amount:+d}" if signed else str(amount.raw_amount)
    return f"{raw} raw {_asset_label(amount.asset)}"


def _gas_fee(value: int | None) -> str:
    return "unavailable" if value is None else f"{value} wei"


def _exact_decimal_amount(raw_amount: int, decimals: int | None) -> str | None:
    if decimals is None:
        return None
    with localcontext() as context:
        context.prec = max(28, len(str(abs(raw_amount))) + decimals + 2)
        return format(Decimal(raw_amount).scaleb(-decimals), "f")


def _economics_lines(economics: ObservedSandwichEconomics) -> tuple[str, ...]:
    victim_lines: list[str] = []
    for index, victim in enumerate(economics.victim_executions, start=1):
        ratio = (
            "unavailable (zero/invalid input)"
            if victim.actual_raw_execution_ratio is None
            else _fraction_decimal(victim.actual_raw_execution_ratio)
        )
        victim_lines.extend(
            (
                f"  Victim {index} actual input:  {_observed_amount(victim.input_amount)}",
                f"  Victim {index} actual output: {_observed_amount(victim.output_amount)}",
                f"  Victim {index} actual raw execution ratio: {ratio}",
                f"  Victim {index} gas fee: {_gas_fee(victim.gas_fee_wei)}",
                (
                    f"  Victim {index} invariant: k_pre={victim.invariant.k_pre} "
                    f"k_post={victim.invariant.k_post} k_delta={victim.invariant.k_delta:+d}"
                ),
            )
        )
    invariant_lines = tuple(
        (
            f"  Outer {label} invariant: k_pre={invariant.k_pre} "
            f"k_post={invariant.k_post} k_delta={invariant.k_delta:+d}"
        )
        for label, invariant in zip(("front", "back"), economics.outer_invariants, strict=True)
    )
    return (
        "Observed outer pair cycle",
        f"  Front spent:    {_observed_amount(economics.front_input)}",
        f"  Front received: {_observed_amount(economics.front_output)}",
        f"  Back spent:     {_observed_amount(economics.back_input)}",
        f"  Back received:  {_observed_amount(economics.back_output)}",
        (
            "  Gross outer-leg cycle delta: "
            f"{_observed_amount(economics.gross_cycle_delta, signed=True)}"
        ),
        (
            "  Intermediate inventory delta: "
            f"{_observed_amount(economics.intermediate_inventory_delta, signed=True)}"
        ),
        "Outer transaction gas expenditure (native ETH, reported in wei)",
        f"  Front: {_gas_fee(economics.front_gas_fee_wei)}",
        f"  Back:  {_gas_fee(economics.back_gas_fee_wei)}",
        f"  Total: {_gas_fee(economics.total_outer_gas_fee_wei)}",
        "Victim actual execution",
        *victim_lines,
        (
            "  Aggregate victim input:  "
            f"{_observed_amount(economics.aggregate_victim_input)}"
        ),
        (
            "  Aggregate victim output: "
            f"{_observed_amount(economics.aggregate_victim_output)}"
        ),
        *invariant_lines,
        "Important: gross pair-level cycle delta is not wallet-level profit.",
        "Important: observed economics alone do not calculate victim counterfactual loss.",
    )


def _counterfactual_lines(result: FixedInputCounterfactual) -> tuple[str, ...]:
    provenance = result.provenance
    lines = [
        "Fixed-input Pair-Level Counterfactual",
        f"  Model state: {result.status.value}",
        f"  Ethereum chain ID: {provenance.chain_id}",
        f"  Canonical Uniswap V2 provenance: {'yes' if provenance.established else 'no'}",
        f"  Pair-reported factory: {provenance.pair_reported_factory_address}",
        f"  Canonical factory getPair result: {provenance.factory_get_pair_address}",
        "  Intervention: remove front reserve effect; hold victim pair input fixed",
    ]
    if not result.victims:
        lines.append(f"  Unavailable reason: {result.unavailable_reason}")
        return tuple(lines)

    direction = result.candidate.direction
    if direction == "token0 -> token1":
        metadata = result.candidate.front_run.token1_metadata
        output_position = "token1"
    else:
        metadata = result.candidate.front_run.token0_metadata
        output_position = "token0"
    output_identity = (
        output_position
        if metadata is None
        else f"{metadata.symbol or output_position} ({metadata.address})"
    )
    decimals = None if metadata is None else metadata.decimals
    for index, execution in enumerate(result.victims, start=1):
        normalized_delta = _exact_decimal_amount(execution.pair_output_delta, decimals)
        delta_display = f"{execution.pair_output_delta:+d} raw {output_identity}"
        if normalized_delta is not None:
            delta_display += f" ({normalized_delta} decimal-normalized)"
        relative = (
            "unavailable"
            if execution.relative_output_improvement is None
            else _fraction_decimal(execution.relative_output_improvement)
        )
        lines.extend(
            (
                f"  Victim {index}: tx #{execution.victim.swap.transaction_index}",
                (
                    "    Observed: input="
                    f"{execution.observed_input} output={execution.observed_output} "
                    f"pre=({execution.observed_pre_reserves.reserve0}, "
                    f"{execution.observed_pre_reserves.reserve1}) "
                    f"post=({execution.observed_post_reserves.reserve0}, "
                    f"{execution.observed_post_reserves.reserve1})"
                ),
                (
                    "    Counterfactual: fixed_input="
                    f"{execution.fixed_input} output={execution.counterfactual_output} "
                    f"pre=({execution.counterfactual_pre_reserves.reserve0}, "
                    f"{execution.counterfactual_pre_reserves.reserve1}) "
                    f"post=({execution.counterfactual_post_reserves.reserve0}, "
                    f"{execution.counterfactual_post_reserves.reserve1})"
                ),
                f"    Counterfactual pair-output delta: {delta_display}",
                f"    Relative output improvement (delta/observed output): {relative}",
                (
                    "    Observed-model validation: canonical maximum="
                    f"{execution.observed_model_quote} observed={execution.observed_output} "
                    f"difference={execution.observed_model_difference:+d} "
                    f"exact={'yes' if execution.observed_model_exact_match else 'no'}"
                ),
            )
        )
    lines.extend(
        (
            f"  Aggregate observed output: {result.aggregate_observed_output}",
            f"  Aggregate counterfactual output: {result.aggregate_counterfactual_output}",
            f"  Aggregate pair-output delta: {result.aggregate_pair_output_delta:+d}",
            "  Replay stops after the final victim; the back leg is not replayed.",
            "  Important: this is fixed-input pair modeling, not full EVM replay.",
            "  Important: this does not prove what the victim wallet would have received.",
        )
    )
    return tuple(lines)


def _receipt_status(status: int | None) -> str:
    if status == 1:
        return "success"
    if status == 0:
        return "reverted"
    return "unavailable"


def _pair_replay_lines(label: str, evidence: tuple[PairSwapEvidence, ...]) -> tuple[str, ...]:
    if not evidence:
        return (f"    {label}: no supported V2 Swap/Sync evidence",)
    lines: list[str] = []
    for index, swap in enumerate(evidence, start=1):
        reserves = (
            "unavailable"
            if swap.post_reserves is None
            else f"({swap.post_reserves.reserve0}, {swap.post_reserves.reserve1})"
        )
        lines.append(
            f"    {label} pair event {index}: pair={swap.pair_address} "
            f"direction={swap.direction} in0={swap.amount0_in} in1={swap.amount1_in} "
            f"out0={swap.amount0_out} out1={swap.amount1_out} post={reserves}"
        )
    return tuple(lines)


def _transaction_replay_lines(
    result: TransactionReplayResult,
    *,
    target: bool,
) -> tuple[str, ...]:
    heading = "Target" if target else "Prefix"
    replay_status = (
        "unavailable"
        if result.replay_receipt is None
        else _receipt_status(result.replay_receipt.status)
    )
    replay_gas = (
        "unavailable" if result.replay_receipt is None else str(result.replay_receipt.gas_used)
    )
    lines = [
        f"{heading} #{result.historical_transaction.transaction_index}",
        f"  Historical hash: {result.historical_transaction.hash}",
        f"  Replay hash:     {result.local_transaction_hash or 'unavailable'}",
        f"  Submission:      {result.submission_status.value}",
        f"  Historical status: {_receipt_status(result.historical_receipt.status)}",
        f"  Replay status:     {replay_status}",
        f"  Historical gas used: {result.historical_receipt.gas_used}",
        f"  Replay gas used:     {replay_gas}",
    ]
    if result.error is not None:
        lines.append(f"  Replay error: {result.error}")
    comparison = result.comparison
    if comparison is None:
        lines.append("  Semantic comparison: unavailable")
        return tuple(lines)
    lines.extend(
        (
            f"  Status comparison:       {comparison.status.value}",
            f"  Receipt-log comparison:  {comparison.semantic_logs.value}",
            f"  Pair Swap comparison:    {comparison.pair_swaps.value}",
            f"  Post-Sync comparison:    {comparison.post_sync_reserves.value}",
            f"  Gas comparison:          {comparison.gas_used.value}",
            (
                "  Receipt semantics exact: "
                f"{'yes' if comparison.receipt_semantics_exact_match else 'no'}"
            ),
            (
                "  Pair execution exact:    "
                f"{'yes' if comparison.pair_execution_exact_match else 'no'}"
            ),
            *_pair_replay_lines("Historical", comparison.historical_pair_evidence),
            *_pair_replay_lines("Replay", comparison.replay_pair_evidence),
        )
    )
    lines.extend(f"  Mismatch: {reason}" for reason in comparison.mismatches)
    return tuple(lines)


def _observed_replay_lines(report: ObservedReplayReport) -> tuple[str, ...]:
    environment = report.environment
    historical_context = environment.historical
    local_context = environment.local
    lines = [
        "Observed EVM Replay",
        f"Block:       {report.plan.block_number}",
        f"Target tx:   #{report.plan.target_transaction_index}",
        f"Forked from: {report.plan.fork_block_number}",
        f"Backend:     {report.backend} ({report.backend_version})",
        f"Hardfork:    {report.configured_hardfork}",
        "",
        "Replay environment",
        (
            "  Historical: "
            f"number={historical_context.number} timestamp={historical_context.timestamp} "
            f"base_fee={historical_context.base_fee_per_gas} "
            f"coinbase={historical_context.coinbase} gas_limit={historical_context.gas_limit} "
            f"prevrandao={historical_context.prevrandao} "
            f"difficulty={historical_context.difficulty} chain_id={historical_context.chain_id}"
        ),
        (
            "  Local: unavailable"
            if local_context is None
            else (
                "  Local:      "
                f"number={local_context.number} timestamp={local_context.timestamp} "
                f"base_fee={local_context.base_fee_per_gas} "
                f"coinbase={local_context.coinbase} gas_limit={local_context.gas_limit} "
                f"prevrandao={local_context.prevrandao} "
                f"difficulty={local_context.difficulty} chain_id={local_context.chain_id}"
            )
        ),
        f"  Matched fields: {', '.join(environment.matched_fields) or 'none'}",
        f"  Mismatched fields: {', '.join(environment.mismatched_fields) or 'none'}",
        f"  Unavailable fields: {', '.join(environment.unavailable_fields) or 'none'}",
    ]
    lines.extend(f"  Setup warning: {warning}" for warning in environment.setup_warnings)
    lines.append("")
    for result in report.prefix:
        lines.extend(_transaction_replay_lines(result, target=False))
        lines.append("")
    lines.extend(_transaction_replay_lines(report.target, target=True))
    lines.extend(
        (
            "",
            "Overall comparison",
            (
                "  Prefix receipt evidence exact: "
                f"{'yes' if report.prefix_receipt_evidence_exact else 'no'}"
            ),
            f"  Target state reliable: {'yes' if report.target_state_reliable else 'no'}",
            (
                "  Target pair execution exact: "
                f"{'yes' if report.pair_execution_exact_match else 'no'}"
            ),
            "Replay mechanics/deviations",
            *(f"  - {deviation}" for deviation in report.replay_deviations),
            "This is observed historical replay; no transaction was removed or altered.",
            "Exact pair execution means matching status, V2 Swap semantics, and post-Sync reserves.",
            "It does not prove arbitrary hidden state changes match beyond compared receipt evidence.",
        )
    )
    return tuple(lines)


def _evm_counterfactual_lines(result: CounterfactualEVMExecution) -> tuple[str, ...]:
    plan = result.plan
    isolation = result.isolation
    receipt = result.receipt_difference
    pair = result.pair_difference
    observed_target = result.observed.target.replay_receipt
    counterfactual_target = result.counterfactual_branch.target.replay_receipt
    observed_status = None if observed_target is None else observed_target.status
    counterfactual_status = (
        None if counterfactual_target is None else counterfactual_target.status
    )
    observed_indexes = ", ".join(
        f"#{transaction.transaction_index}" for transaction in plan.observed.transactions
    )
    counterfactual_indexes = ", ".join(
        f"#{transaction.transaction_index}" for transaction in plan.counterfactual.transactions
    )
    lines = [
        "Forked-EVM Front-Omission Counterfactual",
        f"  Block: {plan.observed.block_number}",
        f"  Fork base: {plan.observed.fork_block_number}",
        f"  Front transaction omitted: #{plan.omitted_transaction_index}",
        f"  Target victim: #{plan.observed.target_transaction_index}",
        f"  Observed branch executes: {observed_indexes}",
        (
            f"  Counterfactual branch executes unchanged: {counterfactual_indexes}; "
            f"omits #{plan.omitted_transaction_index}"
        ),
        f"  Experiment reliable: {'yes' if result.reliable else 'no'}",
        "Observed baseline",
        (
            "  Complete prefix receipt semantics exact: "
            f"{'yes' if result.observed.prefix_receipt_evidence_exact else 'no'}"
        ),
        (
            "  Target pair execution exact: "
            f"{'yes' if result.observed.pair_execution_exact_match else 'no'}"
        ),
    ]
    for transaction_result in result.observed.transactions:
        comparison = transaction_result.comparison
        lines.append(
            f"  Tx #{transaction_result.historical_transaction.transaction_index}: "
            f"receipt semantics="
            f"{comparison.semantic_logs.value if comparison is not None else 'UNAVAILABLE'}; "
            f"V2 pair execution="
            f"{'exact' if comparison is not None and comparison.pair_execution_exact_match else 'not exact'}; "
            f"gas={comparison.gas_used.value if comparison is not None else 'UNAVAILABLE'}"
        )
    lines.extend(
        (
        "Target execution",
        f"  Observed status: {_receipt_status(observed_status)}",
        f"  Counterfactual status: {_receipt_status(counterfactual_status)}",
        f"  Target request unchanged: {'yes' if isolation.same_target_request else 'no'}",
        f"  Counterfactual candidate-pair Swap present: {'yes' if pair.counterfactual_swap_present else 'no'}",
        f"  Pair direction comparison: {pair.direction.value}",
        f"  Pair input unchanged: {pair.pair_input_unchanged.value}",
        f"  Observed pair input: {pair.observed_pair_input}",
        f"  Counterfactual pair input: {pair.counterfactual_pair_input}",
        f"  Observed pair output: {pair.observed_pair_output}",
        f"  Counterfactual pair output: {pair.counterfactual_pair_output}",
        f"  EVM counterfactual pair-output delta: {pair.evm_pair_output_delta}",
        f"  Observed post-Sync reserves: {pair.observed_post_reserves}",
        f"  Counterfactual post-Sync reserves: {pair.counterfactual_post_reserves}",
        "Receipt and gas difference",
        f"  Status changed: {receipt.status_changed}",
        (
            f"  Gas used: observed={receipt.observed_gas_used} "
            f"counterfactual={receipt.counterfactual_gas_used} "
            f"delta={receipt.gas_used_delta} (counterfactual - observed)"
        ),
        (
            f"  Effective gas price: observed={receipt.observed_effective_gas_price} "
            f"counterfactual={receipt.counterfactual_effective_gas_price} "
            f"delta={receipt.effective_gas_price_delta}"
        ),
        (
            f"  Receipt logs: observed={receipt.observed_log_count} "
            f"counterfactual={receipt.counterfactual_log_count} "
            f"delta={receipt.log_count_delta} content={receipt.log_content.value}"
        ),
        "Mathematical V2 cross-check",
        f"  Milestone 6 output: {pair.mathematical_counterfactual_output}",
        f"  Model vs EVM: {pair.model_vs_evm.value}",
        f"  EVM minus mathematical output: {pair.model_vs_evm_output_delta}",
        "Experimental isolation",
        f"  Upstream RPC fingerprint: {isolation.upstream_rpc_fingerprint}",
        (
            f"  Independent fresh forks: {'yes' if isolation.independent_forks else 'no'} "
            f"(instances {isolation.observed_fork_instance_id}, "
            f"{isolation.counterfactual_fork_instance_id})"
        ),
        (
            f"  Backend PIDs: observed={isolation.observed_process_id} "
            f"counterfactual={isolation.counterfactual_process_id}"
        ),
        f"  Same fork block: {'yes' if isolation.same_fork_block else 'no'}",
        f"  Same Anvil version: {'yes' if isolation.same_anvil_version else 'no'}",
        f"  Same hardfork: {'yes' if isolation.same_hardfork else 'no'}",
        f"  Same requested block context: {'yes' if isolation.same_requested_block_context else 'no'}",
        (
            "  Equivalent controllable local environment: "
            f"{'yes' if isolation.controllable_local_environment_equal else 'no'}"
        ),
        (
            "  Uncontrolled local differences: "
            f"{', '.join(isolation.uncontrolled_local_differences) or 'none'}"
        ),
        (
            "  Same non-omitted prefix requests: "
            f"{'yes' if isolation.same_non_omitted_prefix_requests else 'no'}"
        ),
        f"  Omitted transaction absent: {'yes' if isolation.omitted_transaction_absent else 'no'}",
        f"  Intentional difference: {isolation.intentional_difference}",
        )
    )
    lines.extend(f"  Limitation: {limitation}" for limitation in result.limitations)
    lines.extend(
        (
            "Interpretation: the same victim request ran against two independently forked histories.",
            "The only intended pre-target difference was omission of the complete front transaction.",
            "Pair-output differences are execution consequences, not wallet loss or proof of intent.",
        )
    )
    return tuple(lines)


def _checkpoint_balance_lines(
    checkpoint: AddressCheckpoint,
    token0_label: str,
    token1_label: str,
) -> tuple[str, ...]:
    native = "unavailable" if checkpoint.native_wei is None else str(checkpoint.native_wei)
    token0 = (
        "unavailable" if checkpoint.token0_balance is None else str(checkpoint.token0_balance)
    )
    token1 = (
        "unavailable" if checkpoint.token1_balance is None else str(checkpoint.token1_balance)
    )
    return (
        (
            f"    {checkpoint.checkpoint.value}: ETH={native} wei; "
            f"{token0_label}={token0}; {token1_label}={token1}"
        ),
        *(f"      Read limitation: {error}" for error in checkpoint.errors),
    )


def _balance_delta_line(
    label: str,
    delta: AddressBalanceDelta,
    token0_label: str,
    token1_label: str,
) -> str:
    def shown(value: int | None, suffix: str = "") -> str:
        return "unavailable" if value is None else f"{value:+d}{suffix}"

    return (
        f"    {label}: ETH={shown(delta.native_wei, ' wei')}; "
        f"{token0_label}={shown(delta.token0)}; {token1_label}={shown(delta.token1)}"
    )


def _sum_available(values: tuple[int | None, ...]) -> int | None:
    return None if any(value is None for value in values) else sum(
        value for value in values if value is not None
    )


def _observed_attribution_lines(result: ObservedCycleAttribution) -> tuple[str, ...]:
    token0_label = (
        result.token0_metadata.symbol
        if result.token0_metadata is not None and result.token0_metadata.symbol
        else "token0"
    )
    token1_label = (
        result.token1_metadata.symbol
        if result.token1_metadata is not None and result.token1_metadata.symbol
        else "token1"
    )
    nonce = result.nonce_relationship
    lines = [
        "Observed Full-Cycle Attribution",
        f"  Fork base: {result.branch.plan.fork_block_number}",
        (
            "  Replay order: "
            + ", ".join(
                f"#{transaction.transaction_index}"
                for transaction in result.branch.plan.transactions
            )
        ),
        "  Checkpoints: latest S0 and cumulative pending S1/S2/S3; one final mine",
        f"  Attribution reliable: {'yes' if result.reliable else 'no'}",
        "Candidate identities",
        f"  Outer sender: {result.candidate.actor_address}",
        f"  Candidate pair: {result.candidate_pair_address}",
        f"  token0: {result.token0_address} ({token0_label})",
        f"  token1: {result.token1_address} ({token1_label})",
        "Replay reproduction gate",
        f"  Front exact: {'yes' if result.front_exact else 'no'}",
        f"  All victims exact: {'yes' if result.victims_exact else 'no'}",
        f"  Back exact: {'yes' if result.back_exact else 'no'}",
        (
            "  Checkpoint reads complete: "
            f"{'yes' if result.checkpoint_reads_complete else 'no'}"
        ),
        (
            "  Pending S3 equals mined S3: "
            f"{'yes' if result.pending_final_consistent else 'no'}"
        ),
        "Back nonce relationship",
        f"  Front sender: {nonce.front_sender}",
        f"  Front nonce: {nonce.front_nonce}",
        f"  Back sender: {nonce.back_sender}",
        f"  Back nonce: {nonce.back_nonce}",
        f"  Same sender: {'yes' if nonce.same_sender else 'no'}",
        (
            "  Back nonce equals front nonce + 1: "
            + ("unavailable" if nonce.consecutive is None else "yes" if nonce.consecutive else "no")
        ),
        "Tracked-address balances (raw exact units)",
    ]
    for address in result.addresses:
        lines.extend(
            (
                f"  Address: {address.tracked_address.address}",
                f"    Relationships: {', '.join(address.tracked_address.relationships)}",
                *_checkpoint_balance_lines(address.before_front, token0_label, token1_label),
                *_checkpoint_balance_lines(address.after_front, token0_label, token1_label),
                *_checkpoint_balance_lines(address.after_victims, token0_label, token1_label),
                *_checkpoint_balance_lines(address.after_back, token0_label, token1_label),
                _balance_delta_line(
                    "Front delta (S1-S0)",
                    address.front_delta,
                    token0_label,
                    token1_label,
                ),
                _balance_delta_line(
                    "Victim interval delta (S2-S1)",
                    address.victim_interval_delta,
                    token0_label,
                    token1_label,
                ),
                _balance_delta_line(
                    "Back interval delta (S3-S2)",
                    address.back_interval_delta,
                    token0_label,
                    token1_label,
                ),
                _balance_delta_line(
                    "Full-cycle delta (S3-S0)",
                    address.full_cycle_delta,
                    token0_label,
                    token1_label,
                ),
            )
        )
    front_native = result.front_sender_native
    back_native = result.back_sender_native
    lines.extend(
        (
            "Outer-sender native accounting",
            (
                f"  Front #{front_native.transaction_index}: value={front_native.transaction_value_wei} "
                f"gas={front_native.gas_fee_wei} balance_delta="
                f"{front_native.observed_native_delta_wei} delta_beyond_gas_and_value="
                f"{front_native.native_delta_beyond_gas_and_value_wei}"
            ),
            (
                f"  Back interval ending #{back_native.transaction_index}: "
                f"value={back_native.transaction_value_wei} gas={back_native.gas_fee_wei} "
                f"balance_delta={back_native.observed_native_delta_wei} "
                f"delta_beyond_gas_and_value="
                f"{back_native.native_delta_beyond_gas_and_value_wei} "
                f"single-transaction interval="
                f"{'yes' if back_native.interval_contains_only_transaction else 'no'}"
            ),
            "Candidate-token Transfer evidence — front",
        )
    )
    for evidence in result.front_transfers:
        transfer = evidence.transfer
        lines.append(
            f"  log {transfer.log_index}: token={transfer.token_address} "
            f"{transfer.from_address} -> {transfer.to_address} amount={transfer.raw_amount} "
            f"touches_tracked={'yes' if evidence.touches_tracked_address else 'no'}"
        )
    if not result.front_transfers:
        lines.append("  none")
    lines.append("Candidate-token Transfer evidence — back")
    for evidence in result.back_transfers:
        transfer = evidence.transfer
        lines.append(
            f"  log {transfer.log_index}: token={transfer.token_address} "
            f"{transfer.from_address} -> {transfer.to_address} amount={transfer.raw_amount} "
            f"touches_tracked={'yes' if evidence.touches_tracked_address else 'no'}"
        )
    if not result.back_transfers:
        lines.append("  none")
    total_native = _sum_available(
        tuple(item.full_cycle_delta.native_wei for item in result.addresses)
    )
    total_token0 = _sum_available(
        tuple(item.full_cycle_delta.token0 for item in result.addresses)
    )
    total_token1 = _sum_available(
        tuple(item.full_cycle_delta.token1 for item in result.addresses)
    )
    initial_position = result.economics.initial_asset.pair_position
    intermediate_position = result.economics.intermediate_asset.pair_position
    tracked_initial = total_token0 if initial_position == "token0" else total_token1
    tracked_intermediate = total_token0 if intermediate_position == "token0" else total_token1
    lines.extend(
        (
            "Pair-level versus tracked-address endpoint state",
            (
                "  Pair-level gross outer-cycle delta: "
                f"{result.economics.gross_cycle_delta.raw_amount:+d} raw "
                f"{result.economics.initial_asset.symbol or initial_position}"
            ),
            (
                "  Tracked-address total for that token: "
                f"{tracked_initial if tracked_initial is not None else 'unavailable'}"
            ),
            (
                "  Pair-level intermediate inventory delta: "
                f"{result.economics.intermediate_inventory_delta.raw_amount:+d} raw "
                f"{result.economics.intermediate_asset.symbol or intermediate_position}"
            ),
            (
                "  Tracked-address total for that token: "
                f"{tracked_intermediate if tracked_intermediate is not None else 'unavailable'}"
            ),
            f"  Tracked-address native ETH total delta: {total_native}",
        )
    )
    lines.extend(f"  Limitation: {limitation}" for limitation in result.limitations)
    lines.extend(
        (
            "Interpretation: these are observed EVM balance changes for explicitly tracked addresses.",
            "They do not establish common beneficial ownership or a universal profit figure.",
        )
    )
    return tuple(lines)


@app.command("block")
def show_block(
    number: Annotated[int, typer.Argument(min=0, help="Ethereum block number")],
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", min=0, help="Maximum transactions to display"),
    ] = 20,
) -> None:
    """Fetch and display a historical Ethereum block."""
    try:
        block = EthereumRPC.from_env().get_block(number)
    except (BlockScopeError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    timestamp = datetime.fromtimestamp(block.timestamp, tz=UTC).isoformat()
    typer.echo(f"Ethereum Block {block.number}")
    typer.echo(f"Hash: {block.hash}")
    typer.echo(f"Timestamp: {timestamp}")
    typer.echo(f"Transactions: {len(block.transactions)}")
    typer.echo(f"Gas used: {block.gas_used:,} / {block.gas_limit:,}")
    if block.base_fee_per_gas is not None:
        typer.echo(f"Base fee: {block.base_fee_per_gas:,} wei")

    typer.echo("\nTransactions")
    for transaction in block.transactions[:limit]:
        typer.echo(_transaction_line(transaction))
    remaining = len(block.transactions) - limit
    if remaining > 0:
        typer.echo(f"… {remaining} more transaction(s); use --limit to display more")


@app.command("swaps")
def show_swaps(
    number: Annotated[int, typer.Argument(min=0, help="Ethereum block number")],
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", min=0, help="Maximum swap events to display"),
    ] = 50,
) -> None:
    """Display Uniswap V2-compatible Pair Swap events in a block."""
    try:
        analysis = analyze_block_swaps(EthereumRPC.from_env(), number)
    except (BlockScopeError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Uniswap V2-compatible Pair Swap Events — Ethereum Block {number}\n")
    for enriched in analysis.swaps[:limit]:
        for line in _swap_lines(enriched):
            typer.echo(line)
        typer.echo()
    remaining = len(analysis.swaps) - limit
    if remaining > 0:
        typer.echo(f"… {remaining} more event(s); use --limit to display more")
    diagnostics = analysis.diagnostics
    typer.echo("Diagnostics")
    typer.echo(f"Valid Swap events: {diagnostics.valid_swaps}")
    typer.echo(
        "Reserve state reconstructed: "
        f"{diagnostics.swaps_with_reconstructed_reserves}"
    )
    typer.echo(
        "Reserve state unavailable: "
        f"{diagnostics.swaps_without_reconstructed_reserves}"
    )
    typer.echo(f"Malformed matching Swap logs: {diagnostics.malformed_swap_logs}")
    typer.echo(f"Malformed matching Sync logs: {diagnostics.malformed_sync_logs}")
    typer.echo(
        "Invalid reserve reconstructions: "
        f"{diagnostics.invalid_reserve_reconstructions}"
    )
    typer.echo(f"Metadata lookup failures: {diagnostics.metadata_lookup_failures}")


@app.command("replay")
def show_replay(
    number: Annotated[int, typer.Argument(min=1, help="Historical Ethereum block number")],
    transaction_index: Annotated[
        int,
        typer.Argument(min=0, help="Target transaction index within the block"),
    ],
) -> None:
    """Replay a target and its complete block prefix on an observed Anvil fork."""
    try:
        upstream_url = rpc_url_from_env()
        report = replay_observed_transaction(
            EthereumRPC(upstream_url),
            upstream_url,
            number,
            transaction_index,
        )
    except (BlockScopeError, ValueError, TypeError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    for line in _observed_replay_lines(report):
        typer.echo(line)


@app.command("sandwiches")
def show_sandwiches(
    number: Annotated[int, typer.Argument(min=0, help="Ethereum block number")],
    limit: Annotated[
        int,
        typer.Option("--limit", "-l", min=0, help="Maximum candidates to display"),
    ] = 20,
    economics: Annotated[
        bool,
        typer.Option("--economics", help="Show observed pair-level economics"),
    ] = False,
    counterfactual: Annotated[
        bool,
        typer.Option(
            "--counterfactual",
            help="Show canonical fixed-input pair-level victim replay",
        ),
    ] = False,
    evm_counterfactual: Annotated[
        bool,
        typer.Option(
            "--evm-counterfactual",
            help="Execute unchanged victims on independent observed/front-omitted Anvil forks",
        ),
    ] = False,
    flows: Annotated[
        bool,
        typer.Option(
            "--flows",
            help="Replay the observed full cycle and show tracked-address asset balances",
        ),
    ] = False,
) -> None:
    """Display conservative strict sandwich candidates in a block."""
    try:
        if evm_counterfactual or flows:
            upstream_url = rpc_url_from_env()
            rpc = EthereumRPC(upstream_url)
        else:
            upstream_url = None
            rpc = EthereumRPC.from_env()
        swap_analysis = analyze_block_swaps(rpc, number)
        result = detect_strict_sandwich_candidates(swap_analysis.swaps)
        economics_analysis = (
            analyze_observed_sandwich_economics(result.candidates, swap_analysis.receipts)
            if economics or counterfactual or evm_counterfactual or flows
            else None
        )
        counterfactual_analysis = (
            analyze_fixed_input_counterfactuals(rpc, number, result.candidates)
            if counterfactual or evm_counterfactual
            else None
        )
        if evm_counterfactual or flows:
            assert upstream_url is not None
            block = rpc.get_block(number)
        if evm_counterfactual:
            evm_results = tuple(
                execute_front_omission_counterfactual(
                    rpc,
                    upstream_url,
                    block,
                    swap_analysis.receipts,
                    candidate,
                    counterfactual_analysis.candidates[index],
                )
                for index, candidate in enumerate(result.candidates[:limit])
            )
        else:
            evm_results = ()
        if flows:
            assert upstream_url is not None
            assert economics_analysis is not None
            flow_results = tuple(
                execute_observed_cycle_attribution(
                    rpc,
                    upstream_url,
                    block,
                    swap_analysis.receipts,
                    candidate,
                    economics_analysis.candidates[index],
                )
                for index, candidate in enumerate(result.candidates[:limit])
            )
        else:
            flow_results = ()
    except (BlockScopeError, ValueError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(f"Strict Sandwich Candidates — Ethereum Block {number}\n")
    for index, candidate in enumerate(result.candidates[:limit], start=1):
        for line in _candidate_lines(index, candidate):
            typer.echo(line)
        if economics_analysis is not None:
            for line in _economics_lines(economics_analysis.candidates[index - 1]):
                typer.echo(line)
        if counterfactual and counterfactual_analysis is not None:
            for line in _counterfactual_lines(counterfactual_analysis.candidates[index - 1]):
                typer.echo(line)
        if evm_counterfactual:
            for line in _evm_counterfactual_lines(evm_results[index - 1]):
                typer.echo(line)
        if flows:
            for line in _observed_attribution_lines(flow_results[index - 1]):
                typer.echo(line)
        typer.echo()
    remaining = len(result.candidates) - limit
    if remaining > 0:
        typer.echo(f"… {remaining} more candidate(s); use --limit to display more")
    typer.echo(f"{len(result.candidates)} strict sandwich candidate(s)\n")

    diagnostics = result.diagnostics
    typer.echo("Detector diagnostics")
    typer.echo(f"Continuous pair segments: {diagnostics.continuous_pair_segments}")
    typer.echo(f"Potential front legs considered: {diagnostics.potential_front_legs_considered}")
    typer.echo(f"Rejected — no victims: {diagnostics.rejected_no_victims}")
    typer.echo(
        "Rejected — no closing back-run: "
        f"{diagnostics.rejected_no_closing_back_run}"
    )
    typer.echo(
        "Rejected — invalid transaction-level leg sequence: "
        f"{diagnostics.rejected_invalid_leg_sequence}"
    )
    typer.echo(
        "Rejected — closing sender mismatch: "
        f"{diagnostics.rejected_closing_sender_mismatch}"
    )
    typer.echo(f"Rejected — invalid raw quote: {diagnostics.rejected_invalid_quote}")
    typer.echo(f"Rejected — front not adverse: {diagnostics.rejected_adverse_movement}")
    typer.echo(
        "Rejected — back did not improve quote: "
        f"{diagnostics.rejected_back_run_reversal}"
    )
    typer.echo(f"Duplicate candidates suppressed: {diagnostics.duplicate_candidates_suppressed}")
    if economics_analysis is not None:
        economics_diagnostics = economics_analysis.diagnostics
        typer.echo("\nObserved-economics diagnostics")
        typer.echo(f"Economics calculated: {economics_diagnostics.economics_calculated}")
        typer.echo(
            "Missing receipt gas data: "
            f"{economics_diagnostics.missing_receipt_gas_data}"
        )
        typer.echo(
            "Invalid or zero victim inputs: "
            f"{economics_diagnostics.invalid_or_zero_victim_inputs}"
        )
        typer.echo(
            "Unusual invariant decreases: "
            f"{economics_diagnostics.unusual_invariant_decreases}"
        )
        typer.echo(
            f"Missing token metadata: {economics_diagnostics.missing_token_metadata}"
        )
    if counterfactual_analysis is not None:
        counterfactual_diagnostics = counterfactual_analysis.diagnostics
        typer.echo("\nCounterfactual diagnostics")
        typer.echo(
            "Canonical provenance established: "
            f"{counterfactual_diagnostics.canonical_provenance_established}"
        )
        typer.echo(
            "Canonical provenance unavailable: "
            f"{counterfactual_diagnostics.canonical_provenance_unavailable}"
        )
        typer.echo(
            f"Counterfactuals computed: {counterfactual_diagnostics.counterfactuals_computed}"
        )
        typer.echo(
            "Invalid reserve/input cases: "
            f"{counterfactual_diagnostics.invalid_reserve_or_input_cases}"
        )
        typer.echo(
            "Observed-model exact matches: "
            f"{counterfactual_diagnostics.observed_model_exact_matches}"
        )
        typer.echo(
            "Observed-model mismatches: "
            f"{counterfactual_diagnostics.observed_model_mismatches}"
        )
    if evm_counterfactual:
        reliable_count = sum(item.reliable for item in evm_results)
        typer.echo("\nForked-EVM counterfactual diagnostics")
        typer.echo(f"Experiments executed: {len(evm_results)}")
        typer.echo(f"Reliable experiments: {reliable_count}")
        typer.echo(f"Unreliable experiments: {len(evm_results) - reliable_count}")
    if flows:
        reliable_count = sum(item.reliable for item in flow_results)
        partial_count = sum(not item.checkpoint_reads_complete for item in flow_results)
        typer.echo("\nObserved full-cycle attribution diagnostics")
        typer.echo(f"Attributions executed: {len(flow_results)}")
        typer.echo(f"Reliable attributions: {reliable_count}")
        typer.echo(f"Unreliable attributions: {len(flow_results) - reliable_count}")
        typer.echo(f"Partial checkpoint sets: {partial_count}")


if __name__ == "__main__":
    app()
