"""Ethereum JSON-RPC access."""

import os
from collections.abc import Mapping
from urllib.parse import urlsplit

from web3 import Web3

from blockscope.types import Block, TransactionReceipt


class BlockScopeError(Exception):
    """Base exception for errors safe to present to CLI users."""


class ConfigurationError(BlockScopeError):
    """Raised when required runtime configuration is absent or invalid."""


class RPCError(BlockScopeError):
    """Raised when an Ethereum RPC request cannot be completed or decoded."""


def redact_rpc_url_in_text(text: str, rpc_url: str) -> str:
    """Remove credentials, paths, and query parameters when an error repeats an RPC URL."""
    if not rpc_url or rpc_url not in text:
        return text
    try:
        parsed = urlsplit(rpc_url)
        hostname = parsed.hostname
        if hostname is None:
            replacement = "<redacted RPC URL>"
        else:
            host = f"[{hostname}]" if ":" in hostname else hostname
            port = "" if parsed.port is None else f":{parsed.port}"
            replacement = f"{parsed.scheme}://{host}{port}/<redacted>"
    except ValueError:
        replacement = "<redacted RPC URL>"
    return text.replace(rpc_url, replacement)


def rpc_url_from_env(environ: Mapping[str, str] | None = None) -> str:
    """Return the configured Ethereum endpoint or raise an actionable error."""
    source = os.environ if environ is None else environ
    rpc_url = source.get("ETH_RPC_URL", "").strip()
    if not rpc_url:
        raise ConfigurationError(
            "ETH_RPC_URL is not set. Export it with your Ethereum JSON-RPC endpoint, "
            "for example: export ETH_RPC_URL=https://your-provider.example"
        )
    return rpc_url


class EthereumRPC:
    """Small synchronous boundary around Web3's Ethereum RPC methods."""

    def __init__(self, rpc_url: str) -> None:
        if not rpc_url.strip():
            raise ConfigurationError("Ethereum RPC URL cannot be empty")
        self._rpc_url = rpc_url
        self._web3 = Web3(Web3.HTTPProvider(rpc_url))

    def _error_detail(self, exc: Exception) -> str:
        return redact_rpc_url_in_text(str(exc), self._rpc_url)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "EthereumRPC":
        return cls(rpc_url_from_env(environ))

    def get_block(self, number: int) -> Block:
        """Fetch a block by number, including complete transaction objects."""
        if number < 0:
            raise ValueError("Block number must be non-negative")

        try:
            block_data = self._web3.eth.get_block(number, full_transactions=True)
            return Block.from_rpc(block_data)
        except BlockScopeError:
            raise
        except Exception as exc:
            raise RPCError(
                f"Could not fetch Ethereum block {number}: {self._error_detail(exc)}"
            ) from exc

    def get_transaction_receipt(self, transaction_hash: str) -> TransactionReceipt:
        """Fetch and normalize a transaction receipt and all of its logs."""
        try:
            receipt_data = self._web3.eth.get_transaction_receipt(transaction_hash)
            return TransactionReceipt.from_rpc(receipt_data)
        except BlockScopeError:
            raise
        except Exception as exc:
            raise RPCError(
                f"Could not fetch receipt for transaction {transaction_hash}: "
                f"{self._error_detail(exc)}"
            ) from exc

    def get_chain_id(self) -> int:
        """Return the connected Ethereum chain identifier."""
        try:
            return int(self._web3.eth.chain_id)
        except Exception as exc:
            raise RPCError(
                f"Could not determine Ethereum chain ID: {self._error_detail(exc)}"
            ) from exc

    def eth_call(self, contract_address: str, call_data: str, block_number: int) -> bytes:
        """Execute a read-only contract call against historical block state."""
        if block_number < 0:
            raise ValueError("Block number must be non-negative")
        try:
            checksum_address = Web3.to_checksum_address(contract_address)
            result = self._web3.eth.call(
                {"to": checksum_address, "data": call_data},
                block_identifier=block_number,
            )
            if isinstance(result, str):
                return bytes.fromhex(result.removeprefix("0x"))
            return bytes(result)
        except BlockScopeError:
            raise
        except Exception as exc:
            raise RPCError(
                f"Could not call contract {contract_address} at block {block_number}: "
                f"{self._error_detail(exc)}"
            ) from exc
