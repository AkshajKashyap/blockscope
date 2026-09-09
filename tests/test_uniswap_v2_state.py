import pytest
from hexbytes import HexBytes
from web3 import Web3

from blockscope.types import Log, TransactionReceipt
from blockscope.uniswap_v2 import (
    MAX_UINT112,
    SWAP_EVENT_TOPIC,
    SYNC_EVENT_SIGNATURE,
    SYNC_EVENT_TOPIC,
    ReserveReconstructionError,
    SyncDecodeError,
    UniswapV2Swap,
    UniswapV2Sync,
    decode_sync_log,
    reconstruct_reserves,
    scan_receipt_swap_evidence,
)

PAIR_A = "0x" + "aa" * 20
PAIR_B = "0x" + "ab" * 20
TX_HASH = "0x" + "44" * 32


def encoded_words(*values: int) -> str:
    return "0x" + b"".join(value.to_bytes(32) for value in values).hex()


def indexed_address(byte: str) -> str:
    return "0x" + "00" * 12 + byte * 20


def event_log(
    topic: str,
    data: str,
    log_index: int,
    *,
    pair: str = PAIR_A,
    topics: tuple[str, ...] | None = None,
) -> Log:
    return Log(
        address=pair,
        topics=(topic,) if topics is None else topics,
        data=data,
        log_index=log_index,
        transaction_index=3,
        transaction_hash=TX_HASH,
        removed=False,
    )


def sync_log(
    reserve0: int,
    reserve1: int,
    log_index: int,
    *,
    pair: str = PAIR_A,
) -> Log:
    return event_log(SYNC_EVENT_TOPIC, encoded_words(reserve0, reserve1), log_index, pair=pair)


def swap_log(
    amounts: tuple[int, int, int, int],
    log_index: int,
    *,
    pair: str = PAIR_A,
) -> Log:
    return event_log(
        SWAP_EVENT_TOPIC,
        encoded_words(*amounts),
        log_index,
        pair=pair,
        topics=(SWAP_EVENT_TOPIC, indexed_address("bb"), indexed_address("cc")),
    )


def unrelated_log(log_index: int) -> Log:
    return event_log("0x" + "12" * 32, "0x", log_index)


def receipt(*logs: Log) -> TransactionReceipt:
    return TransactionReceipt(TX_HASH, 3, 17_000_000, 1, 100_000, logs)


def semantic_swap(amounts: tuple[int, int, int, int]) -> UniswapV2Swap:
    return UniswapV2Swap(
        17_000_000,
        TX_HASH,
        3,
        2,
        PAIR_A,
        "0x" + "bb" * 20,
        "0x" + "cc" * 20,
        *amounts,
    )


def semantic_sync(reserve0: int, reserve1: int) -> UniswapV2Sync:
    return UniswapV2Sync(17_000_000, TX_HASH, 3, 1, PAIR_A, reserve0, reserve1)


def test_sync_signature_topic_is_verified_independently() -> None:
    assert SYNC_EVENT_SIGNATURE == "Sync(uint112,uint112)"
    assert SYNC_EVENT_TOPIC == f"0x{Web3.keccak(text=SYNC_EVENT_SIGNATURE).hex()}"


@pytest.mark.parametrize(
    ("reserve0", "reserve1"),
    [(0, 0), (0, 9), (7, 0), (MAX_UINT112, MAX_UINT112)],
)
def test_decodes_sync_reserves_including_zero_and_large_values(
    reserve0: int,
    reserve1: int,
) -> None:
    decoded = decode_sync_log(sync_log(reserve0, reserve1, 4), block_number=17_000_000)

    assert decoded is not None
    assert decoded.reserve0 == reserve0
    assert decoded.reserve1 == reserve1
    assert decoded.pair_address == PAIR_A


def test_sync_decoder_ignores_wrong_topic() -> None:
    assert decode_sync_log(unrelated_log(1), block_number=17_000_000) is None


def test_sync_decoder_rejects_malformed_data() -> None:
    with pytest.raises(SyncDecodeError, match="exactly two uint112"):
        decode_sync_log(
            event_log(SYNC_EVENT_TOPIC, "0x1234", 1),
            block_number=17_000_000,
        )


def test_sync_decoder_normalizes_hexbytes_through_log_boundary() -> None:
    raw = {
        "address": HexBytes(PAIR_B),
        "topics": [HexBytes(SYNC_EVENT_TOPIC)],
        "data": HexBytes(encoded_words(12, 34)),
        "logIndex": 1,
        "transactionIndex": 3,
        "transactionHash": HexBytes(TX_HASH),
        "removed": False,
    }

    decoded = decode_sync_log(Log.from_rpc(raw), block_number=17_000_000)

    assert decoded is not None
    assert decoded.pair_address == PAIR_B
    assert (decoded.reserve0, decoded.reserve1) == (12, 34)


def test_associates_immediately_preceding_same_pair_sync() -> None:
    scan = scan_receipt_swap_evidence(
        receipt(sync_log(1_100, 1_820, 1), swap_log((100, 0, 0, 180), 2))
    )

    assert len(scan.swaps) == 1
    assert scan.swaps[0].pre_reserves is not None
    assert scan.swaps[0].post_reserves is not None
    assert (scan.swaps[0].pre_reserves.reserve0, scan.swaps[0].pre_reserves.reserve1) == (
        1_000,
        2_000,
    )


def test_does_not_associate_sync_from_wrong_pair() -> None:
    scan = scan_receipt_swap_evidence(
        receipt(sync_log(1_100, 1_820, 1, pair=PAIR_A), swap_log((100, 0, 0, 180), 2, pair=PAIR_B))
    )

    assert len(scan.swaps) == 1
    assert scan.swaps[0].pre_reserves is None


def test_multiple_sync_swap_sequences_are_independent_and_ordered() -> None:
    scan = scan_receipt_swap_evidence(
        receipt(
            sync_log(2_100, 910, 3),
            swap_log((100, 0, 0, 90), 4),
            sync_log(2_050, 1_010, 5),
            swap_log((0, 100, 50, 0), 6),
        )
    )

    assert tuple(context.swap.log_index for context in scan.swaps) == (4, 6)
    assert scan.swaps[0].pre_reserves is not None
    assert scan.swaps[1].pre_reserves is not None
    assert (scan.swaps[0].pre_reserves.reserve0, scan.swaps[0].pre_reserves.reserve1) == (
        2_000,
        1_000,
    )
    assert (scan.swaps[1].pre_reserves.reserve0, scan.swaps[1].pre_reserves.reserve1) == (
        2_100,
        910,
    )


def test_unrelated_logs_before_sync_do_not_break_association() -> None:
    scan = scan_receipt_swap_evidence(
        receipt(
            unrelated_log(1),
            unrelated_log(2),
            sync_log(1_100, 1_820, 3),
            swap_log((100, 0, 0, 180), 4),
        )
    )

    assert scan.swaps[0].pre_reserves is not None


def test_intervening_log_prevents_conservative_association() -> None:
    scan = scan_receipt_swap_evidence(
        receipt(
            sync_log(1_100, 1_820, 1),
            unrelated_log(2),
            swap_log((100, 0, 0, 180), 3),
        )
    )

    assert scan.swaps[0].pre_reserves is None


def test_missing_sync_retains_swap_without_reserves() -> None:
    scan = scan_receipt_swap_evidence(receipt(swap_log((100, 0, 0, 180), 2)))

    assert len(scan.swaps) == 1
    assert scan.swaps[0].pre_reserves is None
    assert scan.swaps[0].post_reserves is None


def test_standalone_liquidity_sync_does_not_create_swap() -> None:
    scan = scan_receipt_swap_evidence(receipt(sync_log(1_000, 2_000, 1)))

    assert scan.swaps == ()


def test_diagnostics_retain_malformed_matching_evidence() -> None:
    scan = scan_receipt_swap_evidence(
        receipt(
            event_log(SYNC_EVENT_TOPIC, "0x12", 1),
            event_log(
                SWAP_EVENT_TOPIC,
                "0x12",
                2,
                topics=(SWAP_EVENT_TOPIC, indexed_address("bb"), indexed_address("cc")),
            ),
        )
    )

    assert scan.malformed_sync_logs == 1
    assert scan.malformed_swap_logs == 1


def test_reconstructs_forward_swap_with_exact_integer_arithmetic() -> None:
    pre, post = reconstruct_reserves(
        semantic_swap((100, 0, 0, 180)),
        semantic_sync(1_100, 1_820),
    )

    assert (pre.reserve0, pre.reserve1) == (1_000, 2_000)
    assert (post.reserve0, post.reserve1) == (1_100, 1_820)


def test_reconstructs_reverse_swap() -> None:
    pre, post = reconstruct_reserves(
        semantic_swap((0, 100, 45, 0)),
        semantic_sync(955, 2_100),
    )

    assert (pre.reserve0, pre.reserve1) == (1_000, 2_000)
    assert (post.reserve0, post.reserve1) == (955, 2_100)


def test_reconstructs_large_integer_reserves() -> None:
    pre0 = MAX_UINT112 - 1_000
    pre1 = MAX_UINT112 - 2_000
    pre, _ = reconstruct_reserves(
        semantic_swap((500, 0, 0, 700)),
        semantic_sync(pre0 + 500, pre1 - 700),
    )

    assert (pre.reserve0, pre.reserve1) == (pre0, pre1)


def test_reconstruction_rejects_negative_derived_reserve() -> None:
    with pytest.raises(ReserveReconstructionError, match="cannot be negative"):
        reconstruct_reserves(semantic_swap((100, 0, 0, 20)), semantic_sync(50, 1_000))


@pytest.mark.parametrize("amounts", [(0, 0, 0, 1), (1, 0, 0, 0), (0, 0, 0, 0)])
def test_reconstruction_rejects_zero_input_or_output_edges(
    amounts: tuple[int, int, int, int],
) -> None:
    with pytest.raises(ReserveReconstructionError):
        reconstruct_reserves(semantic_swap(amounts), semantic_sync(1_000, 2_000))


def test_reconstruction_rejects_negative_amounts() -> None:
    with pytest.raises(ReserveReconstructionError, match="cannot be negative"):
        reconstruct_reserves(semantic_swap((-1, 0, 0, 1)), semantic_sync(1_000, 2_000))
