from unittest.mock import Mock, patch

from typer.testing import CliRunner

from blockscope.cli import app
from blockscope.types import Block, Transaction

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
