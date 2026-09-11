"""Narrow Anvil trace acquisition and backend-independent call semantics."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol

from blockscope.rpc import BlockScopeError


class TraceError(BlockScopeError):
    """Base error for bounded, user-facing local trace failures."""


class TraceNormalizationError(TraceError):
    """Raised when a backend response cannot be normalized faithfully."""


class TraceMode(StrEnum):
    """Supported Anvil trace response families."""

    CALL_TRACER = "debug_traceTransaction callTracer"
    PARITY = "trace_transaction trace-address fallback"


class TransactionTracer(Protocol):
    """Only the two Anvil trace calls required by observed attribution."""

    def debug_trace_transaction(self, transaction_hash: str) -> Any: ...

    def parity_trace_transaction(self, transaction_hash: str) -> Any: ...


@dataclass(frozen=True, slots=True)
class CallFrame:
    """One immutable semantic call frame with deterministic execution identity."""

    path: str
    call_type: str
    from_address: str | None
    to_address: str | None
    execution_context: str | None
    value_wei: int
    input_data: str
    output_data: str | None
    gas: int | None
    gas_used: int | None
    error: str | None
    children: tuple[CallFrame, ...]

    @property
    def selector(self) -> str | None:
        """Return exact raw four-byte calldata selector when present."""
        data = self.input_data.lower()
        return data[:10] if data.startswith("0x") and len(data) >= 10 else None

    @property
    def succeeded(self) -> bool:
        return self.error is None


@dataclass(frozen=True, slots=True)
class NativeValueTransfer:
    """A realized value edge supported by call execution semantics."""

    from_address: str
    to_address: str
    value_wei: int
    call_path: str
    mechanism: str


@dataclass(frozen=True, slots=True)
class TraceAcquisition:
    """Normalized trace plus explicit backend capability and fallback evidence."""

    root_call: CallFrame | None
    mode: TraceMode | None
    preferred_call_tracer_supported: bool
    fallback_used: bool
    diagnostics: tuple[str, ...]


KNOWN_SELECTOR_LABELS = {
    "0xa9059cbb": "ERC-20 transfer",
    "0x23b872dd": "ERC-20 transferFrom",
    "0x095ea7b3": "ERC-20 approve",
    "0xd0e30db0": "WETH deposit",
    "0x2e1a7d4d": "WETH withdraw",
    "0x022c0d9f": "Uniswap V2 Pair swap",
}


def selector_label(selector: str | None) -> str | None:
    """Label only independently known interfaces used by BlockScope."""
    return None if selector is None else KNOWN_SELECTOR_LABELS.get(selector.lower())


def _bounded_error(exc: BaseException, limit: int = 500) -> str:
    text = " ".join(str(exc).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _mapping(value: Any, context: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TraceNormalizationError(f"{context} must be an object")
    return value


def _quantity(value: Any, field: str, *, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TraceNormalizationError(f"{field} must be an integer quantity")
    try:
        parsed = int(value, 0) if isinstance(value, str) else value
    except ValueError as exc:
        raise TraceNormalizationError(f"{field} is not a valid integer quantity") from exc
    if parsed < 0:
        raise TraceNormalizationError(f"{field} cannot be negative")
    return parsed


def _address(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TraceNormalizationError(f"{field} must be a string address")
    return value.lower()


def _hex_data(value: Any, field: str, *, default: str = "0x") -> str:
    if value is None:
        return default
    if not isinstance(value, str) or not value.startswith("0x"):
        raise TraceNormalizationError(f"{field} must be 0x-prefixed data")
    try:
        bytes.fromhex(value[2:])
    except ValueError as exc:
        raise TraceNormalizationError(f"{field} is not valid hex data") from exc
    return value.lower()


def _error(value: Any) -> str | None:
    if value is None:
        return None
    return _bounded_error(Exception(str(value)))


def normalize_call_tracer_response(raw: Any) -> CallFrame:
    """Normalize nested Anvil callTracer output without retaining backend dictionaries."""

    def visit(value: Any, path: str, parent_context: str | None) -> CallFrame:
        item = _mapping(value, f"call frame {path}")
        raw_type = item.get("type", "UNKNOWN")
        call_type = str(raw_type).upper()
        from_address = _address(item.get("from"), f"{path}.from")
        to_address = _address(item.get("to"), f"{path}.to")
        execution_context = (
            parent_context
            if call_type in {"DELEGATECALL", "CALLCODE"}
            else to_address
        )
        raw_children = item.get("calls", ())
        if raw_children is None:
            raw_children = ()
        if isinstance(raw_children, (str, bytes)) or not isinstance(raw_children, Sequence):
            raise TraceNormalizationError(f"{path}.calls must be an array")
        children = tuple(
            visit(child, f"{path}/{index}", execution_context)
            for index, child in enumerate(raw_children)
        )
        return CallFrame(
            path=path,
            call_type=call_type,
            from_address=from_address,
            to_address=to_address,
            execution_context=execution_context,
            value_wei=_quantity(item.get("value"), f"{path}.value", default=0) or 0,
            input_data=_hex_data(item.get("input"), f"{path}.input"),
            output_data=(
                None
                if item.get("output") is None
                else _hex_data(item.get("output"), f"{path}.output")
            ),
            gas=_quantity(item.get("gas"), f"{path}.gas"),
            gas_used=_quantity(item.get("gasUsed"), f"{path}.gasUsed"),
            error=_error(item.get("error") or item.get("revertReason")),
            children=children,
        )

    return visit(raw, "root", None)


@dataclass(frozen=True, slots=True)
class _FlatFrame:
    address: tuple[int, ...]
    frame: CallFrame


def _flat_trace_frame(raw: Any, position: int) -> _FlatFrame:
    item = _mapping(raw, f"flat trace item {position}")
    raw_address = item.get("traceAddress")
    if isinstance(raw_address, (str, bytes)) or not isinstance(raw_address, Sequence):
        raise TraceNormalizationError(f"flat trace item {position}.traceAddress must be an array")
    try:
        address = tuple(int(part) for part in raw_address)
    except (TypeError, ValueError) as exc:
        raise TraceNormalizationError("traceAddress components must be integers") from exc
    if any(part < 0 for part in address):
        raise TraceNormalizationError("traceAddress components cannot be negative")
    action = _mapping(item.get("action"), f"flat trace item {position}.action")
    result_value = item.get("result")
    result = {} if result_value is None else _mapping(
        result_value, f"flat trace item {position}.result"
    )
    trace_type = str(item.get("type", "unknown")).lower()
    if trace_type == "call":
        call_type = str(action.get("callType", "CALL")).upper()
        from_address = _address(action.get("from"), "action.from")
        to_address = _address(action.get("to"), "action.to")
        value = _quantity(action.get("value"), "action.value", default=0) or 0
        input_data = _hex_data(action.get("input"), "action.input")
        output_data = (
            None if result.get("output") is None else _hex_data(result["output"], "result.output")
        )
    elif trace_type == "create":
        call_type = str(action.get("creationMethod", "CREATE")).upper()
        from_address = _address(action.get("from"), "action.from")
        to_address = _address(result.get("address"), "result.address")
        value = _quantity(action.get("value"), "action.value", default=0) or 0
        input_data = _hex_data(action.get("init"), "action.init")
        output_data = (
            None if result.get("code") is None else _hex_data(result["code"], "result.code")
        )
    elif trace_type in {"suicide", "selfdestruct"}:
        call_type = "SELFDESTRUCT"
        from_address = _address(action.get("address"), "action.address")
        to_address = _address(action.get("refundAddress"), "action.refundAddress")
        value = _quantity(action.get("balance"), "action.balance", default=0) or 0
        input_data = "0x"
        output_data = None
    else:
        call_type = trace_type.upper() or "UNKNOWN"
        from_address = _address(action.get("from"), "action.from")
        to_address = _address(action.get("to"), "action.to")
        value = _quantity(action.get("value"), "action.value", default=0) or 0
        input_data = _hex_data(action.get("input"), "action.input")
        output_data = None
    path = "root" + "".join(f"/{part}" for part in address)
    return _FlatFrame(
        address,
        CallFrame(
            path,
            call_type,
            from_address,
            to_address,
            None,
            value,
            input_data,
            output_data,
            _quantity(action.get("gas"), "action.gas"),
            _quantity(result.get("gasUsed"), "result.gasUsed"),
            _error(item.get("error")),
            (),
        ),
    )


def normalize_parity_trace_response(raw: Any) -> CallFrame:
    """Rebuild Anvil's explicit traceAddress hierarchy without opcode inference."""
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
        raise TraceNormalizationError("trace_transaction result must be an array")
    flat = tuple(_flat_trace_frame(item, index) for index, item in enumerate(raw))
    by_address = {item.address: item.frame for item in flat}
    if len(by_address) != len(flat):
        raise TraceNormalizationError("trace_transaction returned duplicate traceAddress values")
    if () not in by_address:
        raise TraceNormalizationError("trace_transaction did not return a root trace")
    for address in by_address:
        if address and address[:-1] not in by_address:
            raise TraceNormalizationError(f"traceAddress {address!r} has no parent")

    def build(address: tuple[int, ...], parent_context: str | None) -> CallFrame:
        frame = by_address[address]
        execution_context = (
            parent_context
            if frame.call_type in {"DELEGATECALL", "CALLCODE"}
            else frame.to_address
        )
        child_addresses = sorted(
            (
                candidate
                for candidate in by_address
                if len(candidate) == len(address) + 1 and candidate[:-1] == address
            ),
            key=lambda item: item[-1],
        )
        return replace(
            frame,
            execution_context=execution_context,
            children=tuple(build(child, execution_context) for child in child_addresses),
        )

    return build((), None)


def acquire_transaction_trace(
    tracer: TransactionTracer,
    transaction_hash: str,
    *,
    preferred_mode: TraceMode | None = None,
) -> TraceAcquisition:
    """Acquire a real call hierarchy, falling back only to Anvil traceAddress evidence."""
    diagnostics: list[str] = []
    if preferred_mode in (None, TraceMode.CALL_TRACER):
        try:
            root = normalize_call_tracer_response(
                tracer.debug_trace_transaction(transaction_hash)
            )
            return TraceAcquisition(root, TraceMode.CALL_TRACER, True, False, ())
        except TraceNormalizationError:
            raise
        except BlockScopeError as exc:
            diagnostics.append(f"callTracer unavailable: {_bounded_error(exc)}")
            if preferred_mode is TraceMode.CALL_TRACER:
                return TraceAcquisition(None, None, False, False, tuple(diagnostics))

    try:
        root = normalize_parity_trace_response(
            tracer.parity_trace_transaction(transaction_hash)
        )
        return TraceAcquisition(
            root,
            TraceMode.PARITY,
            False,
            True,
            tuple(diagnostics),
        )
    except TraceNormalizationError:
        raise
    except BlockScopeError as exc:
        diagnostics.append(f"trace_transaction fallback unavailable: {_bounded_error(exc)}")
        return TraceAcquisition(None, None, False, True, tuple(diagnostics))


def flatten_call_frames(root: CallFrame) -> tuple[CallFrame, ...]:
    """Return pre-order execution frames, preserving child execution order."""
    return (root, *(child for frame in root.children for child in flatten_call_frames(frame)))


def maximum_call_depth(root: CallFrame) -> int:
    """Return root-relative depth, where the top-level transaction is depth zero."""
    return max(frame.path.count("/") for frame in flatten_call_frames(root))


def realized_native_transfers(root: CallFrame) -> tuple[NativeValueTransfer, ...]:
    """Extract realized value edges once, excluding reverted subtrees and call-context reuse."""
    transfers: list[NativeValueTransfer] = []

    def visit(frame: CallFrame, ancestor_reverted: bool) -> None:
        reverted = ancestor_reverted or not frame.succeeded
        mechanism: str | None = None
        if frame.path == "root" and frame.call_type in {"CALL", "CREATE", "CREATE2"}:
            mechanism = "top-level transaction value"
        elif frame.call_type == "CALL":
            mechanism = "CALL value"
        elif frame.call_type in {"CREATE", "CREATE2"}:
            mechanism = f"{frame.call_type} endowment"
        elif frame.call_type == "SELFDESTRUCT":
            mechanism = "SELFDESTRUCT balance transfer"
        if (
            not reverted
            and mechanism is not None
            and frame.value_wei > 0
            and frame.from_address is not None
            and frame.to_address is not None
        ):
            transfers.append(
                NativeValueTransfer(
                    frame.from_address,
                    frame.to_address,
                    frame.value_wei,
                    frame.path,
                    mechanism,
                )
            )
        for child in frame.children:
            visit(child, reverted)

    visit(root, False)
    return tuple(transfers)
