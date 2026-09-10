"""Opt-in live verification for BlockScope's Ethereum block 17,000,000 fixture."""

from blockscope.rpc import EthereumRPC, rpc_url_from_env
from blockscope.sandwich_workflow import SandwichWorkflowOptions, analyze_sandwich_workflow

BLOCK_NUMBER = 17_000_000
OBSERVED_VICTIM_OUTPUT = 348_906_542_210_008_017_802_287
COUNTERFACTUAL_VICTIM_OUTPUT = 401_242_214_846_052_323_951_351
COUNTERFACTUAL_OUTPUT_DELTA = 52_335_672_636_044_306_149_064
RECIPIENT_WETH_DELTA = 21_983_022_503_039_222


def verify() -> None:
    """Run every live subsystem explicitly; never imported by normal offline tests."""
    upstream_url = rpc_url_from_env()
    result = analyze_sandwich_workflow(
        EthereumRPC(upstream_url),
        upstream_url,
        BLOCK_NUMBER,
        SandwichWorkflowOptions(
            limit=20,
            economics=True,
            mathematical_counterfactual=True,
            evm_counterfactual=True,
            flows=True,
        ),
    )
    assert len(result.sandwiches.candidates) == 1
    analysis = result.candidates[0]
    candidate = analysis.candidate
    assert candidate.front_run.swap.transaction_index == 0
    assert tuple(victim.swap.transaction_index for victim in candidate.victims) == (1,)
    assert candidate.back_run.swap.transaction_index == 2

    assert analysis.economics is not None
    assert analysis.economics.victim_executions[0].output_amount.raw_amount == (
        OBSERVED_VICTIM_OUTPUT
    )
    assert analysis.mathematical_counterfactual is not None
    mathematical_output = analysis.mathematical_counterfactual.victims[0].counterfactual_output
    assert mathematical_output == COUNTERFACTUAL_VICTIM_OUTPUT

    assert analysis.evm_counterfactual is not None
    evm = analysis.evm_counterfactual
    assert evm.reliable
    assert evm.observed.prefix_receipt_evidence_exact
    assert evm.observed.pair_execution_exact_match
    assert evm.pair_difference.counterfactual_pair_output == COUNTERFACTUAL_VICTIM_OUTPUT
    assert evm.pair_difference.evm_pair_output_delta == COUNTERFACTUAL_OUTPUT_DELTA
    assert evm.pair_difference.model_vs_evm_output_delta == 0

    assert analysis.attribution is not None
    attribution = analysis.attribution
    assert attribution.reliable
    assert attribution.front_exact and attribution.victims_exact and attribution.back_exact
    recipient = next(
        address
        for address in attribution.addresses
        if "front transaction recipient (to)" in address.tracked_address.relationships
    )
    assert recipient.full_cycle_delta.token0 == RECIPIENT_WETH_DELTA
    assert recipient.full_cycle_delta.token1 == 0
    print("block 17000000 golden fixture: PASS")
    print(f"observed victim output: {OBSERVED_VICTIM_OUTPUT}")
    print(f"front-omitted EVM output: {COUNTERFACTUAL_VICTIM_OUTPUT}")
    print("EVM minus mathematical: 0")
    print(f"observed recipient WETH delta: {RECIPIENT_WETH_DELTA}")


if __name__ == "__main__":
    verify()
