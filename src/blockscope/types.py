"""Small, provider-independent representations of Ethereum RPC data."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


def _integer(value: Any, *, field_name: str) -> int:
    """Convert an RPC quantity to an integer with a useful error on bad data."""
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be an integer, got a boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError as exc:
            raise ValueError(f"{field_name} is not a valid RPC quantity: {value!r}") from exc
    raise ValueError(f"{field_name} is not a valid RPC quantity: {value!r}")


def _optional_integer(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    return _integer(value, field_name=field_name)


def _hex(value: Any, *, field_name: str) -> str:
    """Normalize string and bytes-like RPC values to 0x-prefixed hex strings."""
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        return f"0x{bytes(value).hex()}"
    hex_method = getattr(value, "hex", None)
    if callable(hex_method):
        result = hex_method()
        if isinstance(result, str):
            return result if result.startswith("0x") else f"0x{result}"
    raise ValueError(f"{field_name} is not a valid hex value: {value!r}")


def _plain(value: Any) -> Any:
    """Copy nested Web3 mappings/lists into ordinary Python containers."""
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_plain(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class Transaction:
    """Transaction fields needed for block inspection and future decoding."""

    hash: str
    transaction_index: int
    from_address: str
    to_address: str | None
    value: int
    gas: int
    gas_price: int | None
    max_fee_per_gas: int | None
    max_priority_fee_per_gas: int | None
    input_data: str
    transaction_type: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_rpc(cls, data: Mapping[str, Any]) -> "Transaction":
        """Build a transaction from a full JSON-RPC transaction object."""
        to_address = data.get("to")
        return cls(
            hash=_hex(data["hash"], field_name="transaction hash"),
            transaction_index=_integer(
                data["transactionIndex"], field_name="transaction index"
            ),
            from_address=_hex(data["from"], field_name="from address"),
            to_address=None if to_address is None else _hex(to_address, field_name="to address"),
            value=_integer(data["value"], field_name="transaction value"),
            gas=_integer(data["gas"], field_name="transaction gas"),
            gas_price=_optional_integer(data.get("gasPrice"), field_name="gas price"),
            max_fee_per_gas=_optional_integer(
                data.get("maxFeePerGas"), field_name="max fee per gas"
            ),
            max_priority_fee_per_gas=_optional_integer(
                data.get("maxPriorityFeePerGas"), field_name="max priority fee per gas"
            ),
            input_data=_hex(data.get("input", "0x"), field_name="transaction input"),
            transaction_type=_optional_integer(data.get("type"), field_name="transaction type"),
            raw=_plain(data),
        )


@dataclass(frozen=True, slots=True)
class Block:
    """The subset of an Ethereum block used by BlockScope today."""

    number: int
    hash: str
    parent_hash: str
    timestamp: int
    gas_used: int
    gas_limit: int
    base_fee_per_gas: int | None
    transactions: tuple[Transaction, ...]
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_rpc(cls, data: Mapping[str, Any]) -> "Block":
        """Build a block from a JSON-RPC block containing full transactions."""
        transactions = data.get("transactions", ())
        if not all(isinstance(transaction, Mapping) for transaction in transactions):
            raise ValueError("RPC block did not include full transaction objects")

        return cls(
            number=_integer(data["number"], field_name="block number"),
            hash=_hex(data["hash"], field_name="block hash"),
            parent_hash=_hex(data["parentHash"], field_name="parent hash"),
            timestamp=_integer(data["timestamp"], field_name="block timestamp"),
            gas_used=_integer(data["gasUsed"], field_name="block gas used"),
            gas_limit=_integer(data["gasLimit"], field_name="block gas limit"),
            base_fee_per_gas=_optional_integer(
                data.get("baseFeePerGas"), field_name="block base fee per gas"
            ),
            transactions=tuple(Transaction.from_rpc(transaction) for transaction in transactions),
            raw=_plain(data),
        )


@dataclass(frozen=True, slots=True)
class Log:
    """An Ethereum event log normalized from a transaction receipt."""

    address: str
    topics: tuple[str, ...]
    data: str
    log_index: int
    transaction_index: int
    transaction_hash: str
    removed: bool | None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_rpc(cls, data: Mapping[str, Any]) -> "Log":
        """Build a log from its JSON-RPC representation."""
        removed = data.get("removed")
        if removed is not None and not isinstance(removed, bool):
            raise TypeError(f"log removed must be a boolean or null, got {removed!r}")
        return cls(
            address=_hex(data["address"], field_name="log address"),
            topics=tuple(
                _hex(topic, field_name="log topic") for topic in data.get("topics", ())
            ),
            data=_hex(data.get("data", "0x"), field_name="log data"),
            log_index=_integer(data["logIndex"], field_name="log index"),
            transaction_index=_integer(
                data["transactionIndex"], field_name="log transaction index"
            ),
            transaction_hash=_hex(data["transactionHash"], field_name="log transaction hash"),
            removed=removed,
            raw=_plain(data),
        )


@dataclass(frozen=True, slots=True)
class TransactionReceipt:
    """Receipt fields needed for event-oriented transaction inspection."""

    transaction_hash: str
    transaction_index: int
    block_number: int
    status: int | None
    gas_used: int
    logs: tuple[Log, ...]
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_rpc(cls, data: Mapping[str, Any]) -> "TransactionReceipt":
        """Build a receipt and all of its logs from JSON-RPC data."""
        logs = data.get("logs", ())
        if not all(isinstance(log, Mapping) for log in logs):
            raise ValueError("RPC receipt logs must be full log objects")
        return cls(
            transaction_hash=_hex(
                data["transactionHash"], field_name="receipt transaction hash"
            ),
            transaction_index=_integer(
                data["transactionIndex"], field_name="receipt transaction index"
            ),
            block_number=_integer(data["blockNumber"], field_name="receipt block number"),
            status=_optional_integer(data.get("status"), field_name="receipt status"),
            gas_used=_integer(data["gasUsed"], field_name="receipt gas used"),
            logs=tuple(Log.from_rpc(log) for log in logs),
            raw=_plain(data),
        )
