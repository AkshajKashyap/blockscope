"""Focused decoding and local-state reads for standard ERC-20 balance evidence."""

from dataclasses import dataclass

from blockscope.replay import AnvilFork, ReplayRPCError
from blockscope.types import Log

TRANSFER_EVENT_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)
BALANCE_OF_SELECTOR = "0x70a08231"


class TransferDecodeError(ValueError):
    """Raised when a log claims to be a Transfer event but is malformed."""


@dataclass(frozen=True, slots=True)
class ERC20Transfer:
    """Exact standard Transfer event evidence from one token contract."""

    token_address: str
    from_address: str
    to_address: str
    raw_amount: int
    transaction_hash: str
    transaction_index: int
    log_index: int


def _decode_hex(value: str, field_name: str) -> bytes:
    if not value.startswith("0x"):
        raise TransferDecodeError(f"{field_name} must be 0x-prefixed")
    try:
        return bytes.fromhex(value[2:])
    except ValueError as exc:
        raise TransferDecodeError(f"{field_name} is not valid hex") from exc


def _decode_indexed_address(topic: str, field_name: str) -> str:
    encoded = _decode_hex(topic, field_name)
    if len(encoded) != 32 or any(encoded[:12]):
        raise TransferDecodeError(f"{field_name} is not an ABI-encoded address")
    return f"0x{encoded[12:].hex()}"


def decode_transfer_log(log: Log) -> ERC20Transfer | None:
    """Decode a standard Transfer log, ignoring unrelated and removed logs."""
    if not log.topics or log.topics[0].lower() != TRANSFER_EVENT_TOPIC:
        return None
    if log.removed:
        return None
    if len(log.topics) != 3:
        raise TransferDecodeError(
            "Transfer event must contain signature, from, and to topics"
        )
    encoded_amount = _decode_hex(log.data, "Transfer event data")
    if len(encoded_amount) != 32:
        raise TransferDecodeError("Transfer event data must contain exactly one uint256")
    return ERC20Transfer(
        token_address=log.address.lower(),
        from_address=_decode_indexed_address(log.topics[1], "Transfer from topic"),
        to_address=_decode_indexed_address(log.topics[2], "Transfer to topic"),
        raw_amount=int.from_bytes(encoded_amount),
        transaction_hash=log.transaction_hash,
        transaction_index=log.transaction_index,
        log_index=log.log_index,
    )


def balance_of(fork: AnvilFork, token_address: str, account: str, block_tag: str) -> int:
    """Read one exact ERC-20 balance from local fork state."""
    normalized = account.removeprefix("0x")
    try:
        encoded_account = bytes.fromhex(normalized)
    except ValueError as exc:
        raise ReplayRPCError(f"invalid balanceOf account address: {account}") from exc
    if len(encoded_account) != 20:
        raise ReplayRPCError(f"invalid balanceOf account address: {account}")
    result = fork.call_contract(
        token_address,
        BALANCE_OF_SELECTOR + encoded_account.hex().rjust(64, "0"),
        block_tag,
    )
    if len(result) != 32:
        raise ReplayRPCError(
            f"ERC-20 balanceOf returned {len(result)} bytes instead of 32"
        )
    return int.from_bytes(result)
