from unittest.mock import Mock, patch

from typer.testing import CliRunner

from blockscope.cli import app
from blockscope.types import Block, Transaction
from blockscope.uniswap_v2 import (
    BlockSwapAnalysis,
    EnrichedUniswapV2Swap,
    ReserveState,
    SwapDiagnostics,
    SwapReserveContext,
    TokenMetadata,
    UniswapV2PairMetadata,
    UniswapV2Swap,
)

runner = CliRunner()


def test_block_command_reports_missing_rpc_url() -> None:
    with patch.dict("os.environ", {}, clear=True):
        result = runner.invoke(app, ["block", "17000000"])

    assert result.exit_code == 1
    assert "ETH_RPC_URL is not set" in result.output
    assert "export ETH_RPC_URL=" in result.output


def test_block_command_displays_summary_and_obeys_limit() -> None:
    transactions = tuple(
        Transaction(
            hash=f"0x{index:064x}",
            transaction_index=index,
            from_address="0x" + "11" * 20,
            to_address=None if index == 0 else "0x" + "22" * 20,
            value=index,
            gas=21_000,
            gas_price=1,
            max_fee_per_gas=None,
            max_priority_fee_per_gas=None,
            input_data="0xdeadbeef",
        )
        for index in range(2)
    )
    block = Block(
        number=17_000_000,
        hash="0x" + "aa" * 32,
        parent_hash="0x" + "bb" * 32,
        timestamp=1_681_332_911,
        gas_used=15_000_000,
        gas_limit=30_000_000,
        base_fee_per_gas=None,
        transactions=transactions,
    )
    client = Mock()
    client.get_block.return_value = block

    with patch("blockscope.cli.EthereumRPC.from_env", return_value=client):
        result = runner.invoke(app, ["block", "17000000", "--limit", "1"])

    assert result.exit_code == 0
    assert "Ethereum Block 17000000" in result.output
    assert "Transactions: 2" in result.output
    assert "CONTRACT_CREATION" in result.output
    assert "1 more transaction(s)" in result.output
    assert "deadbeef" not in result.output
    client.get_block.assert_called_once_with(17_000_000)


def test_swaps_command_displays_raw_events_and_obeys_limit() -> None:
    pair = UniswapV2PairMetadata(
        pair_address="0x" + "aa" * 20,
        factory_address="0x" + "dd" * 20,
        token0_address="0x" + "01" * 20,
        token1_address="0x" + "02" * 20,
    )
    swaps = []
    for index in range(2):
        swap = UniswapV2Swap(
            17_000_000,
            f"0x{index:064x}",
            index,
            index + 10,
            pair.pair_address,
            "0x" + "bb" * 20,
            "0x" + "cc" * 20,
            100 + index,
            0,
            0,
            200 + index,
        )
        swaps.append(
            EnrichedUniswapV2Swap(
                SwapReserveContext(swap, ReserveState(1_000, 2_000), ReserveState(1_100, 1_800)),
                pair,
                TokenMetadata(pair.token0_address or "", 18, "WETH"),
                TokenMetadata(pair.token1_address or "", None, None),
                "0x" + "ee" * 20,
            )
        )
    analysis = BlockSwapAnalysis(
        17_000_000,
        tuple(swaps),
        SwapDiagnostics(2, 1, 2, 2, 0, 0, 1),
    )

    with (
        patch("blockscope.cli.EthereumRPC.from_env", return_value=Mock()) as from_env,
        patch("blockscope.cli.analyze_block_swaps", return_value=analysis) as analyze,
    ):
        result = runner.invoke(app, ["swaps", "17000000", "--limit", "1"])

    assert result.exit_code == 0
    assert "Uniswap V2-compatible Pair Swap Events" in result.output
    assert "token0 -> token1" in result.output
    assert "in0=100" in result.output
    assert "in0=101" not in result.output
    assert "reserves pre=(1000, 2000) post=(1100, 1800)" in result.output
    assert "WETH" in result.output
    assert "Valid Swap events: 2" in result.output
    assert "Malformed matching Swap logs: 1" in result.output
    assert "Metadata lookup failures: 1" in result.output
    assert "1 more event(s)" in result.output
    analyze.assert_called_once_with(from_env.return_value, 17_000_000)


def test_sandwiches_command_reports_zero_candidates_without_overclaiming() -> None:
    swap_analysis = BlockSwapAnalysis(
        17_000_000,
        (),
        SwapDiagnostics(0, 0, 0, 0, 0, 0, 0),
    )

    with (
        patch("blockscope.cli.EthereumRPC.from_env", return_value=Mock()),
        patch("blockscope.cli.analyze_block_swaps", return_value=swap_analysis),
    ):
        result = runner.invoke(app, ["sandwiches", "17000000"])

    assert result.exit_code == 0
    assert "Strict Sandwich Candidates" in result.output
    assert "0 strict sandwich candidate(s)" in result.output
    assert "Potential front legs considered: 0" in result.output
    assert "confirmed" not in result.output.lower()
