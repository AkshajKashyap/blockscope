from unittest.mock import Mock

import pytest

from blockscope.types import Block, Log, Transaction, TransactionReceipt
from blockscope.uniswap_v2 import (
    SWAP_EVENT_SIGNATURE,
    SWAP_EVENT_TOPIC,
    SwapDecodeError,
    collect_block_swaps,
    decode_receipt_swaps,
    decode_swap_log,
)


def indexed_address(address: str) -> str:
    return "0x" + "00" * 12 + address.removeprefix("0x")


def encoded_amounts(*amounts: int) -> str:
    return "0x" + b"".join(amount.to_bytes(32) for amount in amounts).hex()


def swap_log(
    *,
    transaction_index: int = 3,
    log_index: int = 8,
    amounts: tuple[int, int, int, int] = (10, 0, 0, 20),
    topic0: str = SWAP_EVENT_TOPIC,
    data: str | None = None,
    pair_address: str = "0x" + "aa" * 20,
) -> Log:
    return Log(
        address=pair_address,
        topics=(
            topic0,
            indexed_address("0x" + "bb" * 20),
            indexed_address("0x" + "cc" * 20),
        ),
        data=encoded_amounts(*amounts) if data is None else data,
        log_index=log_index,
        transaction_index=transaction_index,
        transaction_hash=f"0x{transaction_index:064x}",
        removed=False,
    )


def receipt(*logs: Log, transaction_index: int = 3) -> TransactionReceipt:
    return TransactionReceipt(
        transaction_hash=f"0x{transaction_index:064x}",
        transaction_index=transaction_index,
        block_number=17_000_000,
        status=1,
        gas_used=100_000,
        logs=logs,
    )


def transaction(index: int) -> Transaction:
    return Transaction(
        hash=f"0x{index:064x}",
        transaction_index=index,
        from_address="0x" + "11" * 20,
        to_address="0x" + "22" * 20,
        value=0,
        gas=100_000,
        gas_price=1,
        max_fee_per_gas=None,
        max_priority_fee_per_gas=None,
        input_data="0x",
    )


def test_uses_exact_canonical_swap_signature_and_topic() -> None:
    assert SWAP_EVENT_SIGNATURE == "Swap(address,uint256,uint256,uint256,uint256,address)"
    assert SWAP_EVENT_TOPIC == (
        "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
    )


def test_decodes_token0_to_token1_swap_and_indexed_addresses() -> None:
    decoded = decode_swap_log(swap_log(), block_number=17_000_000)

    assert decoded is not None
    assert decoded.direction == "token0 -> token1"
    assert decoded.sender == "0x" + "bb" * 20
    assert decoded.recipient == "0x" + "cc" * 20
    assert decoded.amount0_in == 10
    assert decoded.amount1_in == 0
    assert decoded.amount0_out == 0
    assert decoded.amount1_out == 20


def test_decodes_token1_to_token0_swap_from_arbitrary_pair() -> None:
    pair = "0x" + "de" * 20
    decoded = decode_swap_log(
        swap_log(amounts=(0, 30, 40, 0), pair_address=pair), block_number=17_000_000
    )

    assert decoded is not None
    assert decoded.direction == "token1 -> token0"
    assert decoded.pair_address == pair


def test_decodes_large_uint256_values_without_float_conversion() -> None:
    maximum = 2**256 - 1
    decoded = decode_swap_log(
        swap_log(amounts=(maximum, 0, 0, maximum)), block_number=17_000_000
    )

    assert decoded is not None
    assert decoded.amount0_in == maximum
    assert isinstance(decoded.amount0_in, int)


def test_ignores_unrelated_event_topic() -> None:
    decoded = decode_swap_log(
        swap_log(topic0="0x" + "12" * 32), block_number=17_000_000
    )

    assert decoded is None


def test_rejects_malformed_matching_event_data() -> None:
    with pytest.raises(SwapDecodeError, match="exactly four uint256"):
        decode_swap_log(swap_log(data="0x1234"), block_number=17_000_000)


def test_unusual_amounts_have_unknown_direction() -> None:
    decoded = decode_swap_log(
        swap_log(amounts=(1, 1, 1, 1)), block_number=17_000_000
    )

    assert decoded is not None
    assert decoded.direction == "unknown"


def test_receipt_preserves_multiple_swaps_and_safely_skips_other_logs() -> None:
    decoded = decode_receipt_swaps(
        receipt(
            swap_log(log_index=9, amounts=(0, 3, 4, 0)),
            swap_log(log_index=7, topic0="0x" + "12" * 32),
            swap_log(log_index=8, amounts=(5, 0, 0, 6)),
            swap_log(log_index=10, data="0x1234"),
        )
    )

    assert tuple(swap.log_index for swap in decoded) == (8, 9)


def test_collect_block_swaps_fetches_each_receipt_once_and_orders_all_logs() -> None:
    block = Block(
        number=17_000_000,
        hash="0x" + "aa" * 32,
        parent_hash="0x" + "bb" * 32,
        timestamp=1,
        gas_used=1,
        gas_limit=2,
        base_fee_per_gas=3,
        transactions=(transaction(5), transaction(2)),
    )
    receipts = {
        transaction(5).hash: receipt(
            swap_log(transaction_index=5, log_index=12),
            transaction_index=5,
        ),
        transaction(2).hash: receipt(
            swap_log(transaction_index=2, log_index=6),
            swap_log(transaction_index=2, log_index=4),
            transaction_index=2,
        ),
    }
    rpc = Mock()
    rpc.get_block.return_value = block
    rpc.get_transaction_receipt.side_effect = receipts.__getitem__

    swaps = collect_block_swaps(rpc, 17_000_000)

    assert tuple((swap.transaction_index, swap.log_index) for swap in swaps) == (
        (2, 4),
        (2, 6),
        (5, 12),
    )
    assert rpc.get_transaction_receipt.call_count == 2
    rpc.get_block.assert_called_once_with(17_000_000)
