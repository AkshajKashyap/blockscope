from unittest.mock import Mock

import pytest

from blockscope.rpc import (
    ConfigurationError,
    EthereumRPC,
    RPCError,
    redact_rpc_url_in_text,
    rpc_url_from_env,
)


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


def test_rpc_url_redaction_preserves_host_but_removes_credentials_and_path() -> None:
    url = "https://user:password@rpc.example/v2/secret?api-key=value"
    message = redact_rpc_url_in_text(f"failed to connect to {url}", url)

    assert message == "failed to connect to https://rpc.example/<redacted>"
    assert "password" not in message
    assert "secret" not in message
    assert "api-key" not in message


def test_rpc_fetches_and_normalizes_transaction_receipt() -> None:
    client = EthereumRPC("https://rpc.example")
    client._web3 = Mock()
    client._web3.eth.get_transaction_receipt.return_value = {
        "transactionHash": "0x01",
        "transactionIndex": 2,
        "blockNumber": 3,
        "status": 1,
        "gasUsed": 21_000,
        "effectiveGasPrice": 30_000_000_000,
        "logs": [],
    }

    receipt = client.get_transaction_receipt("0x01")

    assert receipt.transaction_hash == "0x01"
    assert receipt.status == 1
    assert receipt.effective_gas_price == 30_000_000_000
    client._web3.eth.get_transaction_receipt.assert_called_once_with("0x01")


def test_receipt_rpc_failure_is_wrapped_with_transaction_context() -> None:
    client = EthereumRPC("https://rpc.example")
    client._web3 = Mock()
    client._web3.eth.get_transaction_receipt.side_effect = OSError("connection refused")

    with pytest.raises(RPCError, match="Could not fetch receipt for transaction 0xabc"):
        client.get_transaction_receipt("0xabc")


def test_eth_call_uses_historical_block_identifier_and_normalizes_bytes() -> None:
    client = EthereumRPC("https://rpc.example")
    client._web3 = Mock()
    client._web3.eth.call.return_value = bytes.fromhex("00" * 31 + "12")

    result = client.eth_call("0x" + "aa" * 20, "0x12345678", 17_000_000)

    assert result == bytes.fromhex("00" * 31 + "12")
    client._web3.eth.call.assert_called_once_with(
        {"to": "0xaAaAaAaaAaAaAaaAaAAAAAAAAaaaAaAaAaaAaaAa", "data": "0x12345678"},
        block_identifier=17_000_000,
    )


def test_eth_call_failure_has_contract_and_block_context() -> None:
    client = EthereumRPC("https://rpc.example")
    client._web3 = Mock()
    client._web3.eth.call.side_effect = OSError("historical state unavailable")

    with pytest.raises(RPCError, match="0xabc at block 123"):
        client.eth_call("0xabc", "0x12345678", 123)


def test_get_chain_id_normalizes_web3_value() -> None:
    client = EthereumRPC("https://rpc.example")
    client._web3 = Mock()
    client._web3.eth.chain_id = 1

    assert client.get_chain_id() == 1
