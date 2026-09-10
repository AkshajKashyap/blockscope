from unittest.mock import Mock, patch

from blockscope.counterfactual import CounterfactualAnalysis
from blockscope.economics import ObservedEconomicsAnalysis
from blockscope.sandwich_workflow import (
    SandwichWorkflowOptions,
    analyze_sandwich_workflow,
)
from blockscope.sandwiches import SandwichScanResult
from blockscope.uniswap_v2 import BlockSwapAnalysis


def test_workflow_skips_unrequested_optional_analyses() -> None:
    rpc = Mock()
    swaps = Mock(spec=BlockSwapAnalysis)
    swaps.swaps = ()
    sandwiches = Mock(spec=SandwichScanResult)
    sandwiches.candidates = ()

    with (
        patch("blockscope.sandwich_workflow.analyze_block_swaps", return_value=swaps),
        patch(
            "blockscope.sandwich_workflow.detect_strict_sandwich_candidates",
            return_value=sandwiches,
        ),
        patch("blockscope.sandwich_workflow.analyze_observed_sandwich_economics") as economics,
        patch("blockscope.sandwich_workflow.analyze_fixed_input_counterfactuals") as mathematical,
    ):
        result = analyze_sandwich_workflow(
            rpc,
            None,
            17_000_000,
            SandwichWorkflowOptions(limit=20),
        )

    assert result.swaps is swaps
    assert result.sandwiches is sandwiches
    assert result.candidates == ()
    economics.assert_not_called()
    mathematical.assert_not_called()
    rpc.get_block.assert_not_called()


def test_workflow_aligns_all_optional_evidence_with_its_candidate() -> None:
    rpc = Mock()
    block = rpc.get_block.return_value
    candidate = Mock(name="candidate")
    swaps = Mock(spec=BlockSwapAnalysis)
    swaps.swaps = (Mock(),)
    swaps.receipts = (Mock(),)
    sandwiches = Mock(spec=SandwichScanResult)
    sandwiches.candidates = (candidate,)
    candidate_economics = Mock(name="economics")
    economics = Mock(spec=ObservedEconomicsAnalysis)
    economics.candidates = (candidate_economics,)
    candidate_mathematical = Mock(name="mathematical")
    mathematical = Mock(spec=CounterfactualAnalysis)
    mathematical.candidates = (candidate_mathematical,)
    evm = Mock(name="evm")
    attribution = Mock(name="attribution")

    with (
        patch("blockscope.sandwich_workflow.analyze_block_swaps", return_value=swaps),
        patch(
            "blockscope.sandwich_workflow.detect_strict_sandwich_candidates",
            return_value=sandwiches,
        ),
        patch(
            "blockscope.sandwich_workflow.analyze_observed_sandwich_economics",
            return_value=economics,
        ),
        patch(
            "blockscope.sandwich_workflow.analyze_fixed_input_counterfactuals",
            return_value=mathematical,
        ),
        patch(
            "blockscope.sandwich_workflow.execute_front_omission_counterfactual",
            return_value=evm,
        ) as execute_evm,
        patch(
            "blockscope.sandwich_workflow.execute_observed_cycle_attribution",
            return_value=attribution,
        ) as execute_flows,
    ):
        result = analyze_sandwich_workflow(
            rpc,
            "https://rpc.example",
            17_000_000,
            SandwichWorkflowOptions(
                limit=20,
                economics=True,
                mathematical_counterfactual=True,
                evm_counterfactual=True,
                flows=True,
            ),
        )

    assert len(result.candidates) == 1
    aligned = result.candidates[0]
    assert aligned.candidate is candidate
    assert aligned.economics is candidate_economics
    assert aligned.mathematical_counterfactual is candidate_mathematical
    assert aligned.evm_counterfactual is evm
    assert aligned.attribution is attribution
    execute_evm.assert_called_once_with(
        rpc,
        "https://rpc.example",
        block,
        swaps.receipts,
        candidate,
        candidate_mathematical,
    )
    execute_flows.assert_called_once_with(
        rpc,
        "https://rpc.example",
        block,
        swaps.receipts,
        candidate,
        candidate_economics,
    )
