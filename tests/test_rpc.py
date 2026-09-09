from unittest.mock import Mock

import pytest

from blockscope.rpc import ConfigurationError, EthereumRPC, RPCError, rpc_url_from_env


def test_missing_rpc_url_has_actionable_error() -> None:
    with pytest.raises(ConfigurationError, match="export ETH_RPC_URL"):
        rpc_url_from_env({})


def test_blank_rpc_url_is_missing() -> None:
    with pytest.raises(ConfigurationError, match="ETH_RPC_URL is not set"):
        rpc_url_from_env({"ETH_RPC_URL": "  "})


def test_rpc_fetches_full_transactions() -> None:
    client = EthereumRPC("https://rpc.example")
    client._web3 = Mock()
    client._web3.eth.get_block.return_value = {
        "number": 1,
        "hash": "0x01",
        "parentHash": "0x00",
        "timestamp": 2,
        "gasUsed": 3,
        "gasLimit": 4,
        "transactions": [],
    }

    block = client.get_block(1)

    assert block.number == 1
    client._web3.eth.get_block.assert_called_once_with(1, full_transactions=True)


def test_rpc_failure_is_wrapped_with_block_context() -> None:
    client = EthereumRPC("https://rpc.example")
    client._web3 = Mock()
    client._web3.eth.get_block.side_effect = OSError("connection refused")

    with pytest.raises(RPCError, match="Could not fetch Ethereum block 123: connection refused"):
        client.get_block(123)
