from hexbytes import HexBytes

from blockscope.types import TransactionReceipt


def log_data(index: int = 4) -> dict[str, object]:
    return {
        "address": HexBytes("0x" + "aa" * 20),
        "topics": [HexBytes("0x" + "11" * 32), HexBytes("0x" + "22" * 32)],
        "data": HexBytes("0x" + "33" * 32),
        "logIndex": index,
        "transactionIndex": 2,
        "transactionHash": HexBytes("0x" + "44" * 32),
        "removed": False,
    }


def receipt_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "transactionHash": HexBytes("0x" + "44" * 32),
        "transactionIndex": 2,
        "blockNumber": 17_000_000,
        "status": 1,
        "gasUsed": 125_000,
        "effectiveGasPrice": 20_000_000_000,
        "logs": [log_data()],
    }
    data.update(overrides)
    return data


def test_normalizes_successful_receipt_and_hexbytes_log_fields() -> None:
    receipt = TransactionReceipt.from_rpc(receipt_data())

    assert receipt.transaction_hash == "0x" + "44" * 32
    assert receipt.transaction_index == 2
    assert receipt.block_number == 17_000_000
    assert receipt.status == 1
    assert receipt.gas_used == 125_000
    assert receipt.effective_gas_price == 20_000_000_000
    assert len(receipt.logs) == 1
    assert receipt.logs[0].address == "0x" + "aa" * 20
    assert receipt.logs[0].topics == (
        "0x" + "11" * 32,
        "0x" + "22" * 32,
    )
    assert receipt.logs[0].data == "0x" + "33" * 32
    assert receipt.logs[0].removed is False
    assert type(receipt.raw) is dict


def test_preserves_failed_transaction_status() -> None:
    receipt = TransactionReceipt.from_rpc(receipt_data(status=0))

    assert receipt.status == 0


def test_normalizes_multiple_logs_in_order() -> None:
    receipt = TransactionReceipt.from_rpc(receipt_data(logs=[log_data(4), log_data(7)]))

    assert tuple(log.log_index for log in receipt.logs) == (4, 7)


def test_normalizes_empty_logs_and_missing_status() -> None:
    data = receipt_data(logs=[])
    del data["status"]

    receipt = TransactionReceipt.from_rpc(data)

    assert receipt.logs == ()
    assert receipt.status is None


def test_allows_missing_effective_gas_price() -> None:
    data = receipt_data()
    del data["effectiveGasPrice"]

    receipt = TransactionReceipt.from_rpc(data)

    assert receipt.effective_gas_price is None
