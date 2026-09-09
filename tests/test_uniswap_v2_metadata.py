from unittest.mock import Mock

from blockscope.rpc import RPCError
from blockscope.types import Block, Log, Transaction, TransactionReceipt
from blockscope.uniswap_v2 import (
    DECIMALS_SELECTOR,
    FACTORY_SELECTOR,
    SWAP_EVENT_TOPIC,
    SYMBOL_SELECTOR,
    SYNC_EVENT_TOPIC,
    TOKEN0_SELECTOR,
    TOKEN1_SELECTOR,
    MetadataResolver,
    analyze_block_swaps,
)

PAIR = "0x" + "aa" * 20
FACTORY = "0x" + "fa" * 20
TOKEN0 = "0x" + "01" * 20
TOKEN1 = "0x" + "02" * 20
BLOCK_NUMBER = 17_000_000


def address_result(address: str) -> bytes:
    return bytes.fromhex("00" * 12 + address.removeprefix("0x"))


def uint_result(value: int) -> bytes:
    return value.to_bytes(32)


def symbol_result(value: str) -> bytes:
    encoded = value.encode()
    padding = b"\x00" * ((32 - len(encoded) % 32) % 32)
    return uint_result(32) + uint_result(len(encoded)) + encoded + padding


def metadata_results() -> dict[tuple[str, str, int], bytes]:
    return {
        (PAIR, FACTORY_SELECTOR, BLOCK_NUMBER): address_result(FACTORY),
        (PAIR, TOKEN0_SELECTOR, BLOCK_NUMBER): address_result(TOKEN0),
        (PAIR, TOKEN1_SELECTOR, BLOCK_NUMBER): address_result(TOKEN1),
        (TOKEN0, DECIMALS_SELECTOR, BLOCK_NUMBER): uint_result(18),
        (TOKEN0, SYMBOL_SELECTOR, BLOCK_NUMBER): symbol_result("WETH"),
        (TOKEN1, DECIMALS_SELECTOR, BLOCK_NUMBER): uint_result(6),
        (TOKEN1, SYMBOL_SELECTOR, BLOCK_NUMBER): symbol_result("USDC"),
    }


def test_queries_pair_and_token_metadata_at_historical_block() -> None:
    rpc = Mock()
    results = metadata_results()
    rpc.eth_call.side_effect = lambda address, selector, block: results[
        (address, selector, block)
    ]
    resolver = MetadataResolver(rpc, BLOCK_NUMBER)

    pair = resolver.pair(PAIR)
    token0 = resolver.token(pair.token0_address or "")
    token1 = resolver.token(pair.token1_address or "")

    assert pair.factory_address == FACTORY
    assert pair.token0_address == TOKEN0
    assert pair.token1_address == TOKEN1
    assert (token0.symbol, token0.decimals) == ("WETH", 18)
    assert (token1.symbol, token1.decimals) == ("USDC", 6)
    assert resolver.lookup_failures == 0
    assert all(call.args[2] == BLOCK_NUMBER for call in rpc.eth_call.call_args_list)


def test_metadata_is_reused_from_command_local_caches() -> None:
    rpc = Mock()
    results = metadata_results()
    rpc.eth_call.side_effect = lambda address, selector, block: results[
        (address, selector, block)
    ]
    resolver = MetadataResolver(rpc, BLOCK_NUMBER)

    first_pair = resolver.pair(PAIR)
    second_pair = resolver.pair(PAIR.upper())
    first_token = resolver.token(TOKEN0)
    second_token = resolver.token(TOKEN0.upper())

    assert first_pair is second_pair
    assert first_token is second_token
    assert rpc.eth_call.call_count == 5


def test_missing_symbol_does_not_discard_decimals() -> None:
    rpc = Mock()

    def call(_address: str, selector: str, _block: int) -> bytes:
        if selector == SYMBOL_SELECTOR:
            raise RPCError("symbol unavailable")
        return uint_result(8)

    rpc.eth_call.side_effect = call
    resolver = MetadataResolver(rpc, BLOCK_NUMBER)

    token = resolver.token(TOKEN0)

    assert token.decimals == 8
    assert token.symbol is None
    assert resolver.lookup_failures == 1


def test_failed_decimals_does_not_discard_symbol() -> None:
    rpc = Mock()

    def call(_address: str, selector: str, _block: int) -> bytes:
        if selector == DECIMALS_SELECTOR:
            raise RPCError("decimals unavailable")
        return symbol_result("ODD")

    rpc.eth_call.side_effect = call
    resolver = MetadataResolver(rpc, BLOCK_NUMBER)

    token = resolver.token(TOKEN0)

    assert token.decimals is None
    assert token.symbol == "ODD"
    assert resolver.lookup_failures == 1


def encoded_words(*values: int) -> str:
    return "0x" + b"".join(value.to_bytes(32) for value in values).hex()


def analysis_fixture() -> tuple[Block, TransactionReceipt]:
    transaction = Transaction(
        "0x" + "44" * 32,
        3,
        "0x" + "11" * 20,
        PAIR,
        0,
        100_000,
        1,
        None,
        None,
        "0x",
    )
    sync = Log(
        PAIR,
        (SYNC_EVENT_TOPIC,),
        encoded_words(1_100, 1_820),
        1,
        3,
        transaction.hash,
        False,
    )
    swap = Log(
        PAIR,
        (
            SWAP_EVENT_TOPIC,
            "0x" + "00" * 12 + "bb" * 20,
            "0x" + "00" * 12 + "cc" * 20,
        ),
        encoded_words(100, 0, 0, 180),
        2,
        3,
        transaction.hash,
        False,
    )
    block = Block(
        BLOCK_NUMBER,
        "0x" + "99" * 32,
        "0x" + "98" * 32,
        1,
        1,
        2,
        3,
        (transaction,),
    )
    receipt = TransactionReceipt(transaction.hash, 3, BLOCK_NUMBER, 1, 100_000, (sync, swap))
    return block, receipt


def test_pair_metadata_failures_do_not_destroy_reserve_analysis() -> None:
    block, receipt = analysis_fixture()
    rpc = Mock()
    rpc.get_block.return_value = block
    rpc.get_transaction_receipt.return_value = receipt
    rpc.eth_call.side_effect = RPCError("metadata unavailable")

    analysis = analyze_block_swaps(rpc, BLOCK_NUMBER)

    assert len(analysis.swaps) == 1
    assert analysis.swaps[0].reserve_context.pre_reserves is not None
    assert analysis.swaps[0].pair_metadata.token0_address is None
    assert analysis.diagnostics.swaps_with_reconstructed_reserves == 1
    assert analysis.diagnostics.metadata_lookup_failures == 3
