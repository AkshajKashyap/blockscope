import pytest

from blockscope.erc20 import TRANSFER_EVENT_TOPIC, TransferDecodeError, decode_transfer_log
from blockscope.types import Log

TOKEN = "0x" + "aa" * 20
SENDER = "0x" + "11" * 20
RECIPIENT = "0x" + "22" * 20
ZERO = "0x" + "00" * 20


def topic(address: str) -> str:
    return "0x" + address.removeprefix("0x").rjust(64, "0")


def transfer_log(
    amount: int,
    *,
    token: str = TOKEN,
    sender: str = SENDER,
    recipient: str = RECIPIENT,
) -> Log:
    return Log(
        token,
        (TRANSFER_EVENT_TOPIC, topic(sender), topic(recipient)),
        f"0x{amount:064x}",
        7,
        3,
        "0x" + "33" * 32,
        False,
    )


@pytest.mark.parametrize("amount", [0, 1, 2**256 - 1])
def test_decodes_standard_transfer_exactly(amount: int) -> None:
    decoded = decode_transfer_log(transfer_log(amount))

    assert decoded is not None
    assert decoded.token_address == TOKEN
    assert decoded.from_address == SENDER
    assert decoded.to_address == RECIPIENT
    assert decoded.raw_amount == amount
    assert decoded.transaction_index == 3
    assert decoded.log_index == 7


def test_decodes_mint_from_zero_address() -> None:
    decoded = decode_transfer_log(transfer_log(9, sender=ZERO))

    assert decoded is not None
    assert decoded.from_address == ZERO


def test_decodes_burn_to_zero_address() -> None:
    decoded = decode_transfer_log(transfer_log(9, recipient=ZERO))

    assert decoded is not None
    assert decoded.to_address == ZERO


def test_arbitrary_token_address_is_retained() -> None:
    arbitrary = "0x" + "ff" * 20
    decoded = decode_transfer_log(transfer_log(4, token=arbitrary))

    assert decoded is not None
    assert decoded.token_address == arbitrary


def test_wrong_topic_is_unrelated() -> None:
    log = transfer_log(1)
    unrelated = Log(
        log.address,
        ("0x" + "99" * 32, *log.topics[1:]),
        log.data,
        log.log_index,
        log.transaction_index,
        log.transaction_hash,
        False,
    )

    assert decode_transfer_log(unrelated) is None


@pytest.mark.parametrize(
    ("topics", "data"),
    [
        ((TRANSFER_EVENT_TOPIC,), "0x" + "00" * 32),
        ((TRANSFER_EVENT_TOPIC, "0x01", topic(RECIPIENT)), "0x" + "00" * 32),
        ((TRANSFER_EVENT_TOPIC, topic(SENDER), topic(RECIPIENT)), "0x01"),
        ((TRANSFER_EVENT_TOPIC, topic(SENDER), topic(RECIPIENT)), "not-hex"),
    ],
)
def test_matching_malformed_transfer_is_rejected(
    topics: tuple[str, ...],
    data: str,
) -> None:
    log = Log(TOKEN, topics, data, 0, 0, "0x01", False)

    with pytest.raises(TransferDecodeError):
        decode_transfer_log(log)
