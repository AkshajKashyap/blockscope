"""Decode the canonical Uniswap V2 Pair Swap event shape."""

from dataclasses import dataclass

from blockscope.rpc import EthereumRPC
from blockscope.types import Log, TransactionReceipt

SWAP_EVENT_SIGNATURE = "Swap(address,uint256,uint256,uint256,uint256,address)"
SWAP_EVENT_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"


class SwapDecodeError(ValueError):
    """Raised when a log claims to be a Swap event but is malformed."""


@dataclass(frozen=True, slots=True)
class UniswapV2Swap:
    """Raw-unit semantics of one Uniswap V2-compatible Pair Swap log."""

    block_number: int
    transaction_hash: str
    transaction_index: int
    log_index: int
    pair_address: str
    sender: str
    recipient: str
    amount0_in: int
    amount1_in: int
    amount0_out: int
    amount1_out: int

    @property
    def direction(self) -> str:
        """Return an ordinary raw token direction, or unknown for unusual values."""
        token0_to_token1 = (
            self.amount0_in > 0
            and self.amount1_out > 0
            and self.amount1_in == 0
            and self.amount0_out == 0
        )
        token1_to_token0 = (
            self.amount1_in > 0
            and self.amount0_out > 0
            and self.amount0_in == 0
            and self.amount1_out == 0
        )
        if token0_to_token1:
            return "token0 -> token1"
        if token1_to_token0:
            return "token1 -> token0"
        return "unknown"


def _hex_bytes(value: str, *, field_name: str) -> bytes:
    if not value.startswith("0x"):
        raise SwapDecodeError(f"{field_name} must be 0x-prefixed")
    try:
        return bytes.fromhex(value[2:])
    except ValueError as exc:
        raise SwapDecodeError(f"{field_name} is not valid hex") from exc


def _indexed_address(topic: str, *, field_name: str) -> str:
    word = _hex_bytes(topic, field_name=field_name)
    if len(word) != 32 or any(word[:12]):
        raise SwapDecodeError(f"{field_name} is not an ABI-encoded address")
    return f"0x{word[12:].hex()}"


def decode_swap_log(log: Log, *, block_number: int) -> UniswapV2Swap | None:
    """Decode one matching Swap log; return None for unrelated or removed logs."""
    if not log.topics or log.topics[0].lower() != SWAP_EVENT_TOPIC:
        return None
    if log.removed:
        return None
    if len(log.topics) != 3:
        raise SwapDecodeError("Swap event must contain signature, sender, and recipient topics")

    encoded_amounts = _hex_bytes(log.data, field_name="Swap event data")
    if len(encoded_amounts) != 4 * 32:
        raise SwapDecodeError("Swap event data must contain exactly four uint256 values")
    amounts = tuple(
        int.from_bytes(encoded_amounts[offset : offset + 32])
        for offset in range(0, len(encoded_amounts), 32)
    )

    return UniswapV2Swap(
        block_number=block_number,
        transaction_hash=log.transaction_hash,
        transaction_index=log.transaction_index,
        log_index=log.log_index,
        pair_address=log.address,
        sender=_indexed_address(log.topics[1], field_name="Swap sender topic"),
        recipient=_indexed_address(log.topics[2], field_name="Swap recipient topic"),
        amount0_in=amounts[0],
        amount1_in=amounts[1],
        amount0_out=amounts[2],
        amount1_out=amounts[3],
    )


def decode_receipt_swaps(receipt: TransactionReceipt) -> tuple[UniswapV2Swap, ...]:
    """Decode every valid supported Swap log from a receipt in log order."""
    swaps: list[UniswapV2Swap] = []
    for log in receipt.logs:
        try:
            swap = decode_swap_log(log, block_number=receipt.block_number)
        except SwapDecodeError:
            continue
        if swap is not None:
            swaps.append(swap)
    return tuple(sorted(swaps, key=lambda swap: (swap.transaction_index, swap.log_index)))


def collect_block_swaps(rpc: EthereumRPC, block_number: int) -> tuple[UniswapV2Swap, ...]:
    """Fetch a block's receipts once each and return all supported Swap events."""
    block = rpc.get_block(block_number)
    swaps: list[UniswapV2Swap] = []
    for transaction in block.transactions:
        receipt = rpc.get_transaction_receipt(transaction.hash)
        swaps.extend(decode_receipt_swaps(receipt))
    return tuple(sorted(swaps, key=lambda swap: (swap.transaction_index, swap.log_index)))
