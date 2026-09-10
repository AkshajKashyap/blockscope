"""Decode and enrich canonical Uniswap V2-compatible Pair event evidence."""

from dataclasses import dataclass

from blockscope.rpc import BlockScopeError, EthereumRPC
from blockscope.types import Log, TransactionReceipt, transaction_identity

SWAP_EVENT_SIGNATURE = "Swap(address,uint256,uint256,uint256,uint256,address)"
SWAP_EVENT_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
SYNC_EVENT_SIGNATURE = "Sync(uint112,uint112)"
SYNC_EVENT_TOPIC = "0x1c411e9a96e071241c2f21f7726b17ae89e3cab4c78be50e062b03a9fffbbad1"

TOKEN0_SELECTOR = "0x0dfe1681"
TOKEN1_SELECTOR = "0xd21220a7"
FACTORY_SELECTOR = "0xc45a0155"
DECIMALS_SELECTOR = "0x313ce567"
SYMBOL_SELECTOR = "0x95d89b41"
MAX_UINT112 = 2**112 - 1


class SwapDecodeError(ValueError):
    """Raised when a log claims to be a Swap event but is malformed."""


class SyncDecodeError(ValueError):
    """Raised when a log claims to be a Sync event but is malformed."""


class ReserveReconstructionError(ValueError):
    """Raised when event evidence cannot describe valid reserve states."""


class MetadataDecodeError(ValueError):
    """Raised when a contract metadata return value is malformed."""


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


@dataclass(frozen=True, slots=True)
class UniswapV2Sync:
    """Post-update reserves emitted by a V2-compatible Pair Sync log."""

    block_number: int
    transaction_hash: str
    transaction_index: int
    log_index: int
    pair_address: str
    reserve0: int
    reserve1: int


@dataclass(frozen=True, slots=True)
class ReserveState:
    """Exact raw reserves for token0 and token1 at one execution point."""

    reserve0: int
    reserve1: int


@dataclass(frozen=True, slots=True)
class SwapReserveContext:
    """A decoded swap with optional transaction-local reserve evidence."""

    swap: UniswapV2Swap
    pre_reserves: ReserveState | None
    post_reserves: ReserveState | None


@dataclass(frozen=True, slots=True)
class UniswapV2PairMetadata:
    """Historically queried structural metadata, with explicit missing fields."""

    pair_address: str
    factory_address: str | None
    token0_address: str | None
    token1_address: str | None


@dataclass(frozen=True, slots=True)
class TokenMetadata:
    """Best-effort ERC-20 display metadata."""

    address: str
    decimals: int | None
    symbol: str | None


@dataclass(frozen=True, slots=True)
class EnrichedUniswapV2Swap:
    """Event-derived reserve context plus independent best-effort metadata."""

    reserve_context: SwapReserveContext
    pair_metadata: UniswapV2PairMetadata
    token0_metadata: TokenMetadata | None
    token1_metadata: TokenMetadata | None
    transaction_sender: str | None

    @property
    def swap(self) -> UniswapV2Swap:
        return self.reserve_context.swap


@dataclass(frozen=True, slots=True)
class SwapDiagnostics:
    """Counts that distinguish absent evidence from malformed evidence."""

    valid_swaps: int
    malformed_swap_logs: int
    malformed_sync_logs: int
    swaps_with_reconstructed_reserves: int
    swaps_without_reconstructed_reserves: int
    invalid_reserve_reconstructions: int
    metadata_lookup_failures: int


@dataclass(frozen=True, slots=True)
class BlockSwapAnalysis:
    """Deterministically ordered enriched swaps and block-scan diagnostics."""

    block_number: int
    swaps: tuple[EnrichedUniswapV2Swap, ...]
    diagnostics: SwapDiagnostics
    receipts: tuple[TransactionReceipt, ...] = ()


@dataclass(frozen=True, slots=True)
class ReceiptSwapScan:
    """Transaction-local event evidence before metadata enrichment."""

    swaps: tuple[SwapReserveContext, ...]
    malformed_swap_logs: int
    malformed_sync_logs: int
    invalid_reserve_reconstructions: int


def _has_topic(log: Log, topic: str) -> bool:
    return bool(log.topics) and log.topics[0].lower() == topic


def _hex_bytes(
    value: str,
    *,
    field_name: str,
    error_type: type[ValueError],
) -> bytes:
    if not value.startswith("0x"):
        raise error_type(f"{field_name} must be 0x-prefixed")
    try:
        return bytes.fromhex(value[2:])
    except ValueError as exc:
        raise error_type(f"{field_name} is not valid hex") from exc


def _indexed_address(topic: str, *, field_name: str) -> str:
    word = _hex_bytes(topic, field_name=field_name, error_type=SwapDecodeError)
    if len(word) != 32 or any(word[:12]):
        raise SwapDecodeError(f"{field_name} is not an ABI-encoded address")
    return f"0x{word[12:].hex()}"


def decode_swap_log(log: Log, *, block_number: int) -> UniswapV2Swap | None:
    """Decode one matching Swap log; return None for unrelated or removed logs."""
    if not _has_topic(log, SWAP_EVENT_TOPIC):
        return None
    if log.removed:
        return None
    if len(log.topics) != 3:
        raise SwapDecodeError("Swap event must contain signature, sender, and recipient topics")

    encoded_amounts = _hex_bytes(
        log.data,
        field_name="Swap event data",
        error_type=SwapDecodeError,
    )
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


def decode_sync_log(log: Log, *, block_number: int) -> UniswapV2Sync | None:
    """Decode one matching canonical Sync log."""
    if not _has_topic(log, SYNC_EVENT_TOPIC):
        return None
    if log.removed:
        return None
    if len(log.topics) != 1:
        raise SyncDecodeError("Sync event must contain only its signature topic")
    encoded_reserves = _hex_bytes(
        log.data,
        field_name="Sync event data",
        error_type=SyncDecodeError,
    )
    if len(encoded_reserves) != 2 * 32:
        raise SyncDecodeError("Sync event data must contain exactly two uint112 values")
    reserve0 = int.from_bytes(encoded_reserves[:32])
    reserve1 = int.from_bytes(encoded_reserves[32:])
    if reserve0 > MAX_UINT112 or reserve1 > MAX_UINT112:
        raise SyncDecodeError("Sync reserves exceed uint112")
    return UniswapV2Sync(
        block_number=block_number,
        transaction_hash=log.transaction_hash,
        transaction_index=log.transaction_index,
        log_index=log.log_index,
        pair_address=log.address,
        reserve0=reserve0,
        reserve1=reserve1,
    )


def reconstruct_reserves(
    swap: UniswapV2Swap,
    sync: UniswapV2Sync,
) -> tuple[ReserveState, ReserveState]:
    """Reconstruct pre/post reserves from a swap and its associated post-Swap Sync."""
    amounts = (swap.amount0_in, swap.amount1_in, swap.amount0_out, swap.amount1_out)
    if any(amount < 0 for amount in amounts):
        raise ReserveReconstructionError("Swap amounts cannot be negative")
    if swap.amount0_in == 0 and swap.amount1_in == 0:
        raise ReserveReconstructionError("Swap must have at least one positive input amount")
    if swap.amount0_out == 0 and swap.amount1_out == 0:
        raise ReserveReconstructionError("Swap must have at least one positive output amount")
    if not 0 <= sync.reserve0 <= MAX_UINT112 or not 0 <= sync.reserve1 <= MAX_UINT112:
        raise ReserveReconstructionError("Post-Swap reserves must fit uint112")

    pre_reserve0 = sync.reserve0 - swap.amount0_in + swap.amount0_out
    pre_reserve1 = sync.reserve1 - swap.amount1_in + swap.amount1_out
    if pre_reserve0 < 0 or pre_reserve1 < 0:
        raise ReserveReconstructionError("Reconstructed pre-Swap reserves cannot be negative")
    if pre_reserve0 > MAX_UINT112 or pre_reserve1 > MAX_UINT112:
        raise ReserveReconstructionError("Reconstructed pre-Swap reserves must fit uint112")

    return (
        ReserveState(pre_reserve0, pre_reserve1),
        ReserveState(sync.reserve0, sync.reserve1),
    )


def _same_execution_context(swap: UniswapV2Swap, sync: UniswapV2Sync) -> bool:
    return (
        swap.pair_address.lower() == sync.pair_address.lower()
        and swap.transaction_hash.lower() == sync.transaction_hash.lower()
        and swap.transaction_index == sync.transaction_index
        and sync.log_index < swap.log_index
    )


def scan_receipt_swap_evidence(receipt: TransactionReceipt) -> ReceiptSwapScan:
    """Associate only immediately adjacent, transaction-local, pair-local Sync/Swap logs."""
    logs = tuple(sorted(receipt.logs, key=lambda log: log.log_index))
    decoded_syncs: dict[int, UniswapV2Sync] = {}
    malformed_sync_logs = 0
    for position, log in enumerate(logs):
        if not _has_topic(log, SYNC_EVENT_TOPIC):
            continue
        try:
            sync = decode_sync_log(log, block_number=receipt.block_number)
        except SyncDecodeError:
            malformed_sync_logs += 1
            continue
        if sync is not None:
            decoded_syncs[position] = sync

    contexts: list[SwapReserveContext] = []
    malformed_swap_logs = 0
    invalid_reconstructions = 0
    for position, log in enumerate(logs):
        if not _has_topic(log, SWAP_EVENT_TOPIC):
            continue
        try:
            swap = decode_swap_log(log, block_number=receipt.block_number)
        except SwapDecodeError:
            malformed_swap_logs += 1
            continue
        if swap is None:
            continue

        pre_reserves: ReserveState | None = None
        post_reserves: ReserveState | None = None
        sync = decoded_syncs.get(position - 1)
        if sync is not None and _same_execution_context(swap, sync):
            try:
                pre_reserves, post_reserves = reconstruct_reserves(swap, sync)
            except ReserveReconstructionError:
                invalid_reconstructions += 1
        contexts.append(SwapReserveContext(swap, pre_reserves, post_reserves))

    contexts.sort(key=lambda context: (context.swap.transaction_index, context.swap.log_index))
    return ReceiptSwapScan(
        swaps=tuple(contexts),
        malformed_swap_logs=malformed_swap_logs,
        malformed_sync_logs=malformed_sync_logs,
        invalid_reserve_reconstructions=invalid_reconstructions,
    )


def decode_receipt_swaps(receipt: TransactionReceipt) -> tuple[UniswapV2Swap, ...]:
    """Decode every valid supported Swap log from a receipt in log order."""
    return tuple(context.swap for context in scan_receipt_swap_evidence(receipt).swaps)


def _scan_block(
    rpc: EthereumRPC,
    block_number: int,
) -> tuple[
    tuple[SwapReserveContext, ...],
    dict[tuple[int, str], str],
    tuple[TransactionReceipt, ...],
    int,
    int,
    int,
]:
    block = rpc.get_block(block_number)
    transaction_senders = {
        transaction_identity(
            transaction.transaction_index,
            transaction.hash,
        ): transaction.from_address.lower()
        for transaction in block.transactions
    }
    contexts: list[SwapReserveContext] = []
    receipts: list[TransactionReceipt] = []
    malformed_swap_logs = 0
    malformed_sync_logs = 0
    invalid_reconstructions = 0
    for transaction in block.transactions:
        receipt = rpc.get_transaction_receipt(transaction.hash)
        receipts.append(receipt)
        scan = scan_receipt_swap_evidence(receipt)
        contexts.extend(scan.swaps)
        malformed_swap_logs += scan.malformed_swap_logs
        malformed_sync_logs += scan.malformed_sync_logs
        invalid_reconstructions += scan.invalid_reserve_reconstructions
    contexts.sort(key=lambda context: (context.swap.transaction_index, context.swap.log_index))
    return (
        tuple(contexts),
        transaction_senders,
        tuple(receipts),
        malformed_swap_logs,
        malformed_sync_logs,
        invalid_reconstructions,
    )


def collect_block_swaps(rpc: EthereumRPC, block_number: int) -> tuple[UniswapV2Swap, ...]:
    """Fetch each receipt once and return all valid Swap events without metadata calls."""
    contexts, _, _, _, _, _ = _scan_block(rpc, block_number)
    return tuple(context.swap for context in contexts)


def _decode_address_result(result: bytes, *, field_name: str) -> str:
    if len(result) != 32 or any(result[:12]):
        raise MetadataDecodeError(f"{field_name} did not return an ABI-encoded address")
    address = result[12:]
    if not any(address):
        raise MetadataDecodeError(f"{field_name} returned the zero address")
    return f"0x{address.hex()}"


def _decode_decimals_result(result: bytes) -> int:
    if len(result) != 32:
        raise MetadataDecodeError("decimals() did not return one ABI word")
    decimals = int.from_bytes(result)
    if decimals > 255:
        raise MetadataDecodeError("decimals() result does not fit uint8")
    return decimals


def _decode_symbol_result(result: bytes) -> str:
    if len(result) == 32:
        encoded_symbol = result.rstrip(b"\x00")
    else:
        if len(result) < 64:
            raise MetadataDecodeError("symbol() returned malformed ABI data")
        offset = int.from_bytes(result[:32])
        if offset + 32 > len(result):
            raise MetadataDecodeError("symbol() returned an invalid string offset")
        length = int.from_bytes(result[offset : offset + 32])
        start = offset + 32
        if start + length > len(result):
            raise MetadataDecodeError("symbol() returned a truncated string")
        encoded_symbol = result[start : start + length]
    try:
        return encoded_symbol.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MetadataDecodeError("symbol() did not return valid UTF-8") from exc


class MetadataResolver:
    """Command-local historical metadata resolver with pair and token caches."""

    def __init__(self, rpc: EthereumRPC, block_number: int) -> None:
        self._rpc = rpc
        self._block_number = block_number
        self._pairs: dict[str, UniswapV2PairMetadata] = {}
        self._tokens: dict[str, TokenMetadata] = {}
        self.lookup_failures = 0

    def _address_call(self, address: str, selector: str, field_name: str) -> str | None:
        try:
            result = self._rpc.eth_call(address, selector, self._block_number)
            return _decode_address_result(result, field_name=field_name)
        except (BlockScopeError, MetadataDecodeError):
            self.lookup_failures += 1
            return None

    def pair(self, pair_address: str) -> UniswapV2PairMetadata:
        key = pair_address.lower()
        if key not in self._pairs:
            self._pairs[key] = UniswapV2PairMetadata(
                pair_address=pair_address,
                factory_address=self._address_call(
                    pair_address, FACTORY_SELECTOR, "factory()"
                ),
                token0_address=self._address_call(pair_address, TOKEN0_SELECTOR, "token0()"),
                token1_address=self._address_call(pair_address, TOKEN1_SELECTOR, "token1()"),
            )
        return self._pairs[key]

    def token(self, token_address: str) -> TokenMetadata:
        key = token_address.lower()
        if key not in self._tokens:
            decimals: int | None = None
            symbol: str | None = None
            try:
                result = self._rpc.eth_call(
                    token_address,
                    DECIMALS_SELECTOR,
                    self._block_number,
                )
                decimals = _decode_decimals_result(result)
            except (BlockScopeError, MetadataDecodeError):
                self.lookup_failures += 1
            try:
                result = self._rpc.eth_call(
                    token_address,
                    SYMBOL_SELECTOR,
                    self._block_number,
                )
                symbol = _decode_symbol_result(result)
            except (BlockScopeError, MetadataDecodeError):
                self.lookup_failures += 1
            self._tokens[key] = TokenMetadata(token_address, decimals, symbol)
        return self._tokens[key]


def analyze_block_swaps(rpc: EthereumRPC, block_number: int) -> BlockSwapAnalysis:
    """Scan, reconstruct, and enrich all V2-compatible Swap evidence in a block."""
    (
        contexts,
        transaction_senders,
        receipts,
        malformed_swaps,
        malformed_syncs,
        invalid_reconstructions,
    ) = _scan_block(rpc, block_number)
    resolver = MetadataResolver(rpc, block_number)
    enriched: list[EnrichedUniswapV2Swap] = []
    for context in contexts:
        pair = resolver.pair(context.swap.pair_address)
        token0 = resolver.token(pair.token0_address) if pair.token0_address else None
        token1 = resolver.token(pair.token1_address) if pair.token1_address else None
        identity = transaction_identity(
            context.swap.transaction_index,
            context.swap.transaction_hash,
        )
        enriched.append(
            EnrichedUniswapV2Swap(
                context,
                pair,
                token0,
                token1,
                transaction_senders.get(identity),
            )
        )

    reconstructed = sum(context.pre_reserves is not None for context in contexts)
    diagnostics = SwapDiagnostics(
        valid_swaps=len(contexts),
        malformed_swap_logs=malformed_swaps,
        malformed_sync_logs=malformed_syncs,
        swaps_with_reconstructed_reserves=reconstructed,
        swaps_without_reconstructed_reserves=len(contexts) - reconstructed,
        invalid_reserve_reconstructions=invalid_reconstructions,
        metadata_lookup_failures=resolver.lookup_failures,
    )
    return BlockSwapAnalysis(block_number, tuple(enriched), diagnostics, receipts)
