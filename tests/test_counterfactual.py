from fractions import Fraction
from unittest.mock import Mock

import pytest
from web3 import Web3

from blockscope.counterfactual import (
    CANONICAL_UNISWAP_V2_FACTORY,
    GET_PAIR_SELECTOR,
    CanonicalProvenanceResolver,
    CanonicalV2Provenance,
    CounterfactualInputError,
    CounterfactualStatus,
    analyze_fixed_input_counterfactuals,
    canonical_v2_get_amount_out,
    replay_fixed_input_pair_level,
)
from blockscope.rpc import RPCError
from blockscope.sandwiches import TOKEN0_TO_TOKEN1, TOKEN1_TO_TOKEN0, SandwichCandidate
from blockscope.uniswap_v2 import (
    EnrichedUniswapV2Swap,
    ReserveState,
    SwapReserveContext,
    TokenMetadata,
    UniswapV2PairMetadata,
    UniswapV2Swap,
)

PAIR = "0x" + "aa" * 20
TOKEN0 = "0x" + "01" * 20
TOKEN1 = "0x" + "02" * 20
ACTOR = "0x" + "10" * 20
VICTIM = "0x" + "11" * 20
BLOCK_NUMBER = 100


def pair_metadata(*, factory: str | None = CANONICAL_UNISWAP_V2_FACTORY) -> UniswapV2PairMetadata:
    return UniswapV2PairMetadata(PAIR, factory, TOKEN0, TOKEN1)


def address_result(address: str) -> bytes:
    return bytes.fromhex("00" * 12 + address.removeprefix("0x"))


def established_provenance() -> CanonicalV2Provenance:
    return CanonicalV2Provenance(
        True,
        1,
        CANONICAL_UNISWAP_V2_FACTORY,
        CANONICAL_UNISWAP_V2_FACTORY,
        PAIR,
        "canonical mainnet factory and getPair() both match",
    )


def enriched(
    transaction_index: int,
    sender: str,
    direction: str,
    amount_in: int,
    amount_out: int,
    pre: ReserveState,
    post: ReserveState,
) -> EnrichedUniswapV2Swap:
    amounts = (
        (amount_in, 0, 0, amount_out)
        if direction == TOKEN0_TO_TOKEN1
        else (0, amount_in, amount_out, 0)
    )
    swap = UniswapV2Swap(
        BLOCK_NUMBER,
        f"0x{transaction_index:064x}",
        transaction_index,
        transaction_index,
        PAIR,
        "0x" + "20" * 20,
        "0x" + "21" * 20,
        *amounts,
    )
    return EnrichedUniswapV2Swap(
        SwapReserveContext(swap, pre, post),
        pair_metadata(),
        TokenMetadata(TOKEN0, 18, "TK0"),
        TokenMetadata(TOKEN1, 6, "TK1"),
        sender,
    )


def candidate_with_victims(
    victims: tuple[EnrichedUniswapV2Swap, ...],
    *,
    direction: str = TOKEN0_TO_TOKEN1,
    initial: ReserveState | None = None,
) -> SandwichCandidate:
    initial = initial or ReserveState(1_000, 1_000)
    if direction == TOKEN0_TO_TOKEN1:
        front_post = ReserveState(1_100, 910)
        back_direction = TOKEN1_TO_TOKEN0
    else:
        front_post = ReserveState(910, 1_100)
        back_direction = TOKEN0_TO_TOKEN1
    front = enriched(10, ACTOR, direction, 100, 90, initial, front_post)
    last_post = victims[-1].reserve_context.post_reserves or front_post
    back = enriched(20, ACTOR, back_direction, 10, 9, last_post, initial)
    return SandwichCandidate(
        PAIR,
        ACTOR,
        direction,
        front,
        victims,
        back,
        Fraction(1),
        Fraction(9, 10),
        Fraction(9, 10),
        Fraction(8, 10),
        Fraction(9, 10),
    )


def single_victim_candidate(
    *,
    direction: str = TOKEN0_TO_TOKEN1,
    observed_output: int | None = None,
    amount_in: int = 100,
) -> SandwichCandidate:
    if direction == TOKEN0_TO_TOKEN1:
        observed_pre = ReserveState(1_100, 910)
    else:
        observed_pre = ReserveState(910, 1_100)
    reserve_in = 1_100
    reserve_out = 910
    output = (
        canonical_v2_get_amount_out(amount_in, reserve_in, reserve_out)
        if observed_output is None
        else observed_output
    )
    if direction == TOKEN0_TO_TOKEN1:
        observed_post = ReserveState(1_100 + amount_in, 910 - output)
    else:
        observed_post = ReserveState(910 - output, 1_100 + amount_in)
    victim = enriched(11, VICTIM, direction, amount_in, output, observed_pre, observed_post)
    return candidate_with_victims((victim,), direction=direction)


@pytest.mark.parametrize(
    ("amount_in", "reserve_in", "reserve_out", "expected"),
    [
        (1_000, 10_000, 10_000, 906),
        (100, 1_000, 1_000, 90),
        (1, 1_000, 1_000, 0),
        (2**200, 2**220, 2**230, (2**200 * 997 * 2**230) // (2**220 * 1000 + 2**200 * 997)),
    ],
)
def test_canonical_get_amount_out_reference_vectors(
    amount_in: int,
    reserve_in: int,
    reserve_out: int,
    expected: int,
) -> None:
    assert canonical_v2_get_amount_out(amount_in, reserve_in, reserve_out) == expected


@pytest.mark.parametrize(
    ("amount_in", "reserve_in", "reserve_out"),
    [(0, 1, 1), (-1, 1, 1), (1, 0, 1), (1, 1, 0)],
)
def test_canonical_get_amount_out_rejects_invalid_inputs(
    amount_in: int,
    reserve_in: int,
    reserve_out: int,
) -> None:
    with pytest.raises(CounterfactualInputError):
        canonical_v2_get_amount_out(amount_in, reserve_in, reserve_out)


def test_get_pair_selector_is_verified_independently() -> None:
    assert GET_PAIR_SELECTOR == f"0x{Web3.keccak(text='getPair(address,address)')[:4].hex()}"


def test_canonical_provenance_requires_factory_and_historical_get_pair_match() -> None:
    rpc = Mock()
    rpc.get_chain_id.return_value = 1
    rpc.eth_call.return_value = address_result(PAIR)
    resolver = CanonicalProvenanceResolver(rpc, BLOCK_NUMBER)

    provenance = resolver.pair(pair_metadata())

    assert provenance.established is True
    assert provenance.factory_get_pair_address == PAIR
    call_address, call_data, call_block = rpc.eth_call.call_args.args
    assert call_address == CANONICAL_UNISWAP_V2_FACTORY
    assert call_data.startswith(GET_PAIR_SELECTOR)
    assert TOKEN0.removeprefix("0x") in call_data
    assert TOKEN1.removeprefix("0x") in call_data
    assert call_block == BLOCK_NUMBER


def test_wrong_pair_reported_factory_fails_provenance_without_rpc_guessing() -> None:
    rpc = Mock()
    resolver = CanonicalProvenanceResolver(rpc, BLOCK_NUMBER)

    provenance = resolver.pair(pair_metadata(factory="0x" + "ff" * 20))

    assert provenance.established is False
    assert "not the canonical" in provenance.reason
    rpc.eth_call.assert_not_called()


def test_factory_get_pair_mismatch_fails_provenance() -> None:
    rpc = Mock()
    rpc.get_chain_id.return_value = 1
    rpc.eth_call.return_value = address_result("0x" + "bb" * 20)

    provenance = CanonicalProvenanceResolver(rpc, BLOCK_NUMBER).pair(pair_metadata())

    assert provenance.established is False
    assert "different pair" in provenance.reason


def test_failed_historical_get_pair_call_fails_provenance() -> None:
    rpc = Mock()
    rpc.get_chain_id.return_value = 1
    rpc.eth_call.side_effect = RPCError("historical state unavailable")

    provenance = CanonicalProvenanceResolver(rpc, BLOCK_NUMBER).pair(pair_metadata())

    assert provenance.established is False
    assert "historical" in provenance.reason


def test_malformed_token_address_fails_provenance_without_losing_candidate() -> None:
    rpc = Mock()
    rpc.get_chain_id.return_value = 1
    metadata = UniswapV2PairMetadata(
        PAIR,
        CANONICAL_UNISWAP_V2_FACTORY,
        "not-an-address",
        TOKEN1,
    )

    provenance = CanonicalProvenanceResolver(rpc, BLOCK_NUMBER).pair(metadata)

    assert provenance.established is False
    assert "metadata is invalid" in provenance.reason
    rpc.eth_call.assert_not_called()


def test_non_mainnet_chain_fails_provenance() -> None:
    rpc = Mock()
    rpc.get_chain_id.return_value = 10

    provenance = CanonicalProvenanceResolver(rpc, BLOCK_NUMBER).pair(pair_metadata())

    assert provenance.established is False
    assert "not Ethereum mainnet" in provenance.reason
    rpc.eth_call.assert_not_called()


def test_single_victim_replay_starts_before_front_and_uses_fixed_input() -> None:
    replay = replay_fixed_input_pair_level(single_victim_candidate(), established_provenance())
    execution = replay.victims[0]

    assert execution.counterfactual_pre_reserves == ReserveState(1_000, 1_000)
    assert execution.fixed_input == 100
    assert execution.counterfactual_output == 90
    assert execution.counterfactual_post_reserves == ReserveState(1_100, 910)
    assert execution.observed_model_quote == execution.observed_output == 75
    assert execution.pair_output_delta == 15
    assert execution.relative_output_improvement == Fraction(1, 5)
    assert replay.status is CounterfactualStatus.EXACT_OBSERVED_REPRODUCTION


def test_reverse_direction_replay_is_symmetric() -> None:
    replay = replay_fixed_input_pair_level(
        single_victim_candidate(direction=TOKEN1_TO_TOKEN0),
        established_provenance(),
    )
    execution = replay.victims[0]

    assert execution.counterfactual_pre_reserves == ReserveState(1_000, 1_000)
    assert execution.counterfactual_output == 90
    assert execution.counterfactual_post_reserves == ReserveState(910, 1_100)
    assert execution.pair_output_delta == 15


def test_multiple_victims_replay_sequentially_and_preserve_trajectory() -> None:
    observed_states = (
        (ReserveState(1_100, 910), ReserveState(1_200, 835)),
        (ReserveState(1_200, 835), ReserveState(1_300, 771)),
        (ReserveState(1_300, 771), ReserveState(1_400, 716)),
    )
    victims = tuple(
        enriched(
            11 + index,
            f"0x{30 + index:040x}",
            TOKEN0_TO_TOKEN1,
            100,
            canonical_v2_get_amount_out(100, pre.reserve0, pre.reserve1),
            pre,
            post,
        )
        for index, (pre, post) in enumerate(observed_states)
    )

    replay = replay_fixed_input_pair_level(
        candidate_with_victims(victims),
        established_provenance(),
    )

    assert tuple(execution.counterfactual_output for execution in replay.victims) == (90, 75, 64)
    assert replay.victims[1].counterfactual_pre_reserves == replay.victims[0].counterfactual_post_reserves
    assert replay.victims[2].counterfactual_pre_reserves == replay.victims[1].counterfactual_post_reserves
    assert replay.reserve_trajectory == (
        ReserveState(1_000, 1_000),
        ReserveState(1_100, 910),
        ReserveState(1_200, 835),
        ReserveState(1_300, 771),
    )
    assert replay.aggregate_counterfactual_output == 229
    assert len(replay.victims) == 3


@pytest.mark.parametrize(
    ("observed_output", "expected_delta"),
    [(75, 15), (90, 0), (95, -5)],
)
def test_pair_output_delta_preserves_positive_zero_and_negative_results(
    observed_output: int,
    expected_delta: int,
) -> None:
    replay = replay_fixed_input_pair_level(
        single_victim_candidate(observed_output=observed_output),
        established_provenance(),
    )

    assert replay.victims[0].pair_output_delta == expected_delta
    assert replay.aggregate_pair_output_delta == expected_delta


def test_observed_model_mismatch_remains_computed_but_model_limited() -> None:
    replay = replay_fixed_input_pair_level(
        single_victim_candidate(observed_output=70),
        established_provenance(),
    )
    execution = replay.victims[0]

    assert execution.observed_model_quote == 75
    assert execution.observed_model_difference == 5
    assert execution.observed_model_exact_match is False
    assert replay.status is CounterfactualStatus.MODEL_LIMITED
    assert execution.counterfactual_output == 90


def test_unestablished_provenance_retains_candidate_but_suppresses_replay() -> None:
    unavailable = CanonicalV2Provenance(
        False,
        1,
        CANONICAL_UNISWAP_V2_FACTORY,
        "0x" + "ff" * 20,
        None,
        "wrong factory",
    )

    replay = replay_fixed_input_pair_level(single_victim_candidate(), unavailable)

    assert replay.status is CounterfactualStatus.PROVENANCE_UNAVAILABLE
    assert replay.victims == ()
    assert replay.candidate == single_victim_candidate()


def test_invalid_input_is_counted_without_losing_candidate() -> None:
    invalid_candidate = single_victim_candidate(observed_output=1, amount_in=0)
    rpc = Mock()
    rpc.get_chain_id.return_value = 1
    rpc.eth_call.return_value = address_result(PAIR)

    analysis = analyze_fixed_input_counterfactuals(
        rpc,
        BLOCK_NUMBER,
        (invalid_candidate,),
    )

    assert len(analysis.candidates) == 1
    assert analysis.candidates[0].status is CounterfactualStatus.INVALID_INPUT
    assert analysis.diagnostics.invalid_reserve_or_input_cases == 1
    assert analysis.diagnostics.counterfactuals_computed == 0
