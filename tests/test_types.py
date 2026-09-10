from blockscope.types import (
    Block,
    TransactionReceipt,
    index_transaction_receipts,
    transaction_identity,
)


def test_transaction_identity_normalizes_hash_case_without_losing_index() -> None:
    assert transaction_identity(7, "0xAbCd") == (7, "0xabcd")


def test_receipt_index_uses_canonical_historical_identity() -> None:
    receipt = TransactionReceipt("0xAbCd", 7, 100, 1, 21_000, ())

    assert index_transaction_receipts((receipt,))[(7, "0xabcd")] is receipt


def transaction_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "hash": bytes.fromhex("11" * 32),
        "transactionIndex": 0,
        "from": "0x" + "22" * 20,
        "to": "0x" + "33" * 20,
        "nonce": 7,
        "value": 123,
        "gas": 21_000,
        "gasPrice": 30_000_000_000,
        "input": "0x",
        "type": 0,
    }
    data.update(overrides)
    return data


def block_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "number": 17_000_000,
        "hash": bytes.fromhex("aa" * 32),
        "parentHash": bytes.fromhex("bb" * 32),
        "timestamp": 1_681_332_911,
        "gasUsed": 15_000_000,
        "gasLimit": 30_000_000,
        "baseFeePerGas": 25_000_000_000,
        "miner": "0x" + "44" * 20,
        "difficulty": 0,
        "mixHash": bytes.fromhex("cc" * 32),
        "transactions": [transaction_data()],
    }
    data.update(overrides)
    return data


def test_converts_representative_rpc_block_to_models() -> None:
    block = Block.from_rpc(block_data())

    assert block.number == 17_000_000
    assert block.hash == "0x" + "aa" * 32
    assert block.parent_hash == "0x" + "bb" * 32
    assert block.gas_used == 15_000_000
    assert block.base_fee_per_gas == 25_000_000_000
    assert block.miner_address == "0x" + "44" * 20
    assert block.difficulty == 0
    assert block.mix_hash == "0x" + "cc" * 32
    assert len(block.transactions) == 1
    assert block.transactions[0].input_data == "0x"
    assert block.transactions[0].nonce == 7
    assert type(block.raw) is dict


def test_converts_eip_1559_fee_fields() -> None:
    transaction = transaction_data(
        type=2,
        gasPrice=28_000_000_000,
        maxFeePerGas=40_000_000_000,
        maxPriorityFeePerGas=2_000_000_000,
        chainId=1,
        accessList=[
            {
                "address": "0x" + "55" * 20,
                "storageKeys": ["0x" + "66" * 32],
            }
        ],
    )

    converted = Block.from_rpc(block_data(transactions=[transaction])).transactions[0]

    assert converted.transaction_type == 2
    assert converted.gas_price == 28_000_000_000
    assert converted.max_fee_per_gas == 40_000_000_000
    assert converted.max_priority_fee_per_gas == 2_000_000_000
    assert converted.chain_id == 1
    assert converted.access_list[0].address == "0x" + "55" * 20
    assert converted.access_list[0].storage_keys == ("0x" + "66" * 32,)


def test_converts_legacy_fee_fields() -> None:
    converted = Block.from_rpc(block_data()).transactions[0]

    assert converted.transaction_type == 0
    assert converted.gas_price == 30_000_000_000
    assert converted.max_fee_per_gas is None
    assert converted.max_priority_fee_per_gas is None


def test_allows_contract_creation_and_missing_base_fee() -> None:
    data = block_data(transactions=[transaction_data(to=None)])
    del data["baseFeePerGas"]

    converted = Block.from_rpc(data)

    assert converted.base_fee_per_gas is None
    assert converted.transactions[0].to_address is None


def test_accepts_hex_encoded_rpc_quantities() -> None:
    converted = Block.from_rpc(
        block_data(number="0x10", transactions=[transaction_data(value="0x2a")])
    )

    assert converted.number == 16
    assert converted.transactions[0].value == 42


def test_rejects_hash_only_transactions() -> None:
    try:
        Block.from_rpc(block_data(transactions=["0x" + "11" * 32]))
    except ValueError as exc:
        assert "full transaction objects" in str(exc)
    else:
        raise AssertionError("Expected hash-only transaction data to be rejected")
