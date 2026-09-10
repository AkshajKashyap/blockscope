"""Observed historical transaction replay through a subprocess-backed Anvil fork."""

from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Self, TextIO

import httpx

from blockscope.rpc import BlockScopeError, EthereumRPC
from blockscope.types import AccessListEntry, Block, Transaction, TransactionReceipt
from blockscope.uniswap_v2 import ReserveState, scan_receipt_swap_evidence


class ReplayError(BlockScopeError):
    """Base error for replay failures that are safe to show in the CLI."""


class AnvilUnavailableError(ReplayError):
    """Raised when the external Anvil backend is missing or unusable."""


class ReplayRPCError(ReplayError):
    """Raised when local Anvil JSON-RPC fails."""


class ReplayInputError(ReplayError):
    """Raised when historical evidence cannot form a faithful replay request."""


class UnsupportedTransactionType(ReplayInputError):
    """Raised for transaction envelopes this milestone does not support."""


class ReplaySubmissionStatus(StrEnum):
    """Lifecycle state of one planned replay transaction."""

    REPLAYED = "replayed"
    SUBMISSION_FAILED = "submission failed"
    NOT_ATTEMPTED = "not attempted"


class ComparisonState(StrEnum):
    """Non-probabilistic comparison state."""

    MATCH = "MATCH"
    DIFFER = "DIFFER"
    UNAVAILABLE = "UNAVAILABLE"


def _quantity(value: int) -> str:
    if value < 0:
        raise ReplayInputError("RPC quantities cannot be negative")
    return hex(value)


@dataclass(frozen=True, slots=True)
class ReplayTransactionRequest:
    """Historical execution fields submitted through sender impersonation."""

    from_address: str
    to_address: str | None
    nonce: int
    value: int
    input_data: str
    gas: int
    transaction_type: int
    gas_price: int | None
    max_fee_per_gas: int | None
    max_priority_fee_per_gas: int | None
    chain_id: int | None
    access_list: tuple[AccessListEntry, ...]

    def to_rpc(self) -> dict[str, Any]:
        """Return the JSON-RPC transaction object without inventing fee fields."""
        request: dict[str, Any] = {
            "from": self.from_address,
            "nonce": _quantity(self.nonce),
            "value": _quantity(self.value),
            "data": self.input_data,
            "gas": _quantity(self.gas),
            "type": _quantity(self.transaction_type),
        }
        if self.to_address is not None:
            request["to"] = self.to_address
        if self.chain_id is not None:
            request["chainId"] = _quantity(self.chain_id)
        if self.transaction_type in (0, 1):
            if self.gas_price is None:
                raise ReplayInputError(
                    f"type {self.transaction_type} transaction has no gasPrice"
                )
            request["gasPrice"] = _quantity(self.gas_price)
        if self.transaction_type in (1, 2):
            request["accessList"] = [
                {
                    "address": entry.address,
                    "storageKeys": list(entry.storage_keys),
                }
                for entry in self.access_list
            ]
        if self.transaction_type == 2:
            if self.max_fee_per_gas is None or self.max_priority_fee_per_gas is None:
                raise ReplayInputError("type 2 transaction lacks EIP-1559 fee fields")
            request["maxFeePerGas"] = _quantity(self.max_fee_per_gas)
            request["maxPriorityFeePerGas"] = _quantity(self.max_priority_fee_per_gas)
        return request


def transaction_replay_request(transaction: Transaction) -> ReplayTransactionRequest:
    """Convert one normalized historical transaction without changing its semantics."""
    transaction_type = transaction.transaction_type
    if transaction_type not in (0, 1, 2):
        shown = "missing" if transaction_type is None else str(transaction_type)
        raise UnsupportedTransactionType(
            f"transaction #{transaction.transaction_index} has unsupported type {shown}; "
            "observed replay currently supports types 0, 1, and 2"
        )
    if transaction.nonce is None:
        raise ReplayInputError(f"transaction #{transaction.transaction_index} has no nonce")
    if transaction_type == 0 and transaction.access_list:
        raise ReplayInputError("type 0 transaction unexpectedly contains an access list")
    request = ReplayTransactionRequest(
        from_address=transaction.from_address,
        to_address=transaction.to_address,
        nonce=transaction.nonce,
        value=transaction.value,
        input_data=transaction.input_data,
        gas=transaction.gas,
        transaction_type=transaction_type,
        gas_price=transaction.gas_price,
        max_fee_per_gas=transaction.max_fee_per_gas,
        max_priority_fee_per_gas=transaction.max_priority_fee_per_gas,
        chain_id=transaction.chain_id,
        access_list=transaction.access_list,
    )
    request.to_rpc()
    return request


@dataclass(frozen=True, slots=True)
class ReplayPlan:
    """A block prefix and target to execute together in canonical order."""

    block_number: int
    fork_block_number: int
    target_transaction_index: int
    prefix: tuple[Transaction, ...]
    target: Transaction

    @property
    def transactions(self) -> tuple[Transaction, ...]:
        return (*self.prefix, self.target)


def plan_observed_replay(block: Block, target_transaction_index: int) -> ReplayPlan:
    """Select the complete canonical prefix and target from a pre-block fork state."""
    if block.number <= 0:
        raise ReplayInputError("replay block must be greater than zero")
    if target_transaction_index < 0:
        raise ReplayInputError("target transaction index must be non-negative")
    by_index = {transaction.transaction_index: transaction for transaction in block.transactions}
    required_indexes = tuple(range(target_transaction_index + 1))
    missing = tuple(index for index in required_indexes if index not in by_index)
    if missing:
        raise ReplayInputError(
            f"block {block.number} does not contain required transaction index(es): "
            + ", ".join(str(index) for index in missing)
        )
    ordered = tuple(by_index[index] for index in required_indexes)
    return ReplayPlan(
        block_number=block.number,
        fork_block_number=block.number - 1,
        target_transaction_index=target_transaction_index,
        prefix=ordered[:-1],
        target=ordered[-1],
    )


def infer_ethereum_hardfork(block: Block) -> str:
    """Select a deterministic mainnet EVM revision from historical header markers."""
    if block.raw.get("requestsHash") is not None:
        return "prague"
    if block.raw.get("parentBeaconBlockRoot") is not None:
        return "cancun"
    if block.raw.get("withdrawalsRoot") is not None:
        return "shanghai"
    if block.difficulty == 0:
        return "paris"
    if block.base_fee_per_gas is not None:
        return "london"
    raise ReplayInputError(
        "cannot infer a supported deterministic hardfork from this pre-London block header"
    )


@dataclass(frozen=True, slots=True)
class SemanticLog:
    """Receipt log content with local-chain bookkeeping removed."""

    address: str
    topics: tuple[str, ...]
    data: str


@dataclass(frozen=True, slots=True)
class PairSwapEvidence:
    """Meaningful V2 Swap and adjacent post-Swap Sync evidence."""

    pair_address: str
    direction: str
    amount0_in: int
    amount1_in: int
    amount0_out: int
    amount1_out: int
    post_reserves: ReserveState | None

    @property
    def swap_identity(self) -> tuple[str, str, int, int, int, int]:
        return (
            self.pair_address,
            self.direction,
            self.amount0_in,
            self.amount1_in,
            self.amount0_out,
            self.amount1_out,
        )


def _semantic_logs(receipt: TransactionReceipt) -> tuple[SemanticLog, ...]:
    return tuple(
        SemanticLog(
            log.address.lower(),
            tuple(topic.lower() for topic in log.topics),
            log.data.lower(),
        )
        for log in sorted(receipt.logs, key=lambda item: item.log_index)
    )


def _pair_swap_evidence(receipt: TransactionReceipt) -> tuple[PairSwapEvidence, ...]:
    return tuple(
        PairSwapEvidence(
            pair_address=context.swap.pair_address.lower(),
            direction=context.swap.direction,
            amount0_in=context.swap.amount0_in,
            amount1_in=context.swap.amount1_in,
            amount0_out=context.swap.amount0_out,
            amount1_out=context.swap.amount1_out,
            post_reserves=context.post_reserves,
        )
        for context in scan_receipt_swap_evidence(receipt).swaps
    )


@dataclass(frozen=True, slots=True)
class ReplayComparison:
    """Independent receipt, pair-execution, and gas comparison facts."""

    status: ComparisonState
    semantic_logs: ComparisonState
    pair_swaps: ComparisonState
    post_sync_reserves: ComparisonState
    gas_used: ComparisonState
    historical_pair_evidence: tuple[PairSwapEvidence, ...]
    replay_pair_evidence: tuple[PairSwapEvidence, ...]
    receipt_semantics_exact_match: bool
    pair_execution_exact_match: bool
    mismatches: tuple[str, ...]


def compare_replay_receipts(
    historical: TransactionReceipt,
    replay: TransactionReceipt,
) -> ReplayComparison:
    """Compare normalized execution evidence, intentionally ignoring hashes/indexes."""
    status_match = historical.status is not None and historical.status == replay.status
    logs_match = _semantic_logs(historical) == _semantic_logs(replay)
    historical_pairs = _pair_swap_evidence(historical)
    replay_pairs = _pair_swap_evidence(replay)
    historical_swap_identities = tuple(item.swap_identity for item in historical_pairs)
    replay_swap_identities = tuple(item.swap_identity for item in replay_pairs)
    pair_swaps_match = bool(historical_pairs) and (
        historical_swap_identities == replay_swap_identities
    )
    reserves_match = (
        bool(historical_pairs)
        and len(historical_pairs) == len(replay_pairs)
        and all(item.post_reserves is not None for item in historical_pairs)
        and tuple(item.post_reserves for item in historical_pairs)
        == tuple(item.post_reserves for item in replay_pairs)
    )
    if historical.gas_used is None or replay.gas_used is None:
        gas_state = ComparisonState.UNAVAILABLE
    elif historical.gas_used == replay.gas_used:
        gas_state = ComparisonState.MATCH
    else:
        gas_state = ComparisonState.DIFFER

    mismatches: list[str] = []
    if not status_match:
        mismatches.append(
            f"status differs: historical={historical.status}, replay={replay.status}"
        )
    if not logs_match:
        mismatches.append("normalized receipt log content/order differs")
    if not pair_swaps_match:
        reason = "historical receipt has no supported V2 Swap evidence"
        if historical_pairs:
            reason = "V2 pair address/direction/amount evidence differs"
        mismatches.append(reason)
    if not reserves_match:
        mismatches.append("V2 post-Swap Sync reserve evidence differs or is unavailable")
    if gas_state is ComparisonState.DIFFER:
        mismatches.append(
            f"gas used differs: historical={historical.gas_used}, replay={replay.gas_used}"
        )
    return ReplayComparison(
        status=ComparisonState.MATCH if status_match else ComparisonState.DIFFER,
        semantic_logs=ComparisonState.MATCH if logs_match else ComparisonState.DIFFER,
        pair_swaps=ComparisonState.MATCH if pair_swaps_match else ComparisonState.DIFFER,
        post_sync_reserves=(
            ComparisonState.MATCH if reserves_match else ComparisonState.DIFFER
        ),
        gas_used=gas_state,
        historical_pair_evidence=historical_pairs,
        replay_pair_evidence=replay_pairs,
        receipt_semantics_exact_match=status_match and logs_match,
        pair_execution_exact_match=status_match and pair_swaps_match and reserves_match,
        mismatches=tuple(mismatches),
    )


@dataclass(frozen=True, slots=True)
class ReplayBlockContext:
    """Execution-relevant block fields available from normalized RPC blocks."""

    number: int
    timestamp: int
    base_fee_per_gas: int | None
    gas_limit: int
    coinbase: str | None
    prevrandao: str | None
    difficulty: int | None
    chain_id: int

    @classmethod
    def from_block(cls, block: Block, chain_id: int) -> ReplayBlockContext:
        return cls(
            number=block.number,
            timestamp=block.timestamp,
            base_fee_per_gas=block.base_fee_per_gas,
            gas_limit=block.gas_limit,
            coinbase=None if block.miner_address is None else block.miner_address.lower(),
            prevrandao=None if block.mix_hash is None else block.mix_hash.lower(),
            difficulty=block.difficulty,
            chain_id=chain_id,
        )


@dataclass(frozen=True, slots=True)
class ReplayEnvironmentEvidence:
    """Historical/local block context comparison and failed control attempts."""

    historical: ReplayBlockContext
    local: ReplayBlockContext | None
    matched_fields: tuple[str, ...]
    mismatched_fields: tuple[str, ...]
    unavailable_fields: tuple[str, ...]
    setup_warnings: tuple[str, ...]


def compare_replay_environment(
    historical: ReplayBlockContext,
    local: ReplayBlockContext | None,
    setup_warnings: tuple[str, ...] = (),
) -> ReplayEnvironmentEvidence:
    """Compare each known execution-context field without hiding unavailable data."""
    field_names = (
        "number",
        "timestamp",
        "base_fee_per_gas",
        "gas_limit",
        "coinbase",
        "prevrandao",
        "difficulty",
        "chain_id",
    )
    if local is None:
        return ReplayEnvironmentEvidence(
            historical,
            None,
            (),
            (),
            field_names,
            setup_warnings,
        )
    matched: list[str] = []
    mismatched: list[str] = []
    unavailable: list[str] = []
    for name in field_names:
        historical_value = getattr(historical, name)
        local_value = getattr(local, name)
        if historical_value is None or local_value is None:
            unavailable.append(name)
        elif historical_value == local_value:
            matched.append(name)
        else:
            mismatched.append(name)
    return ReplayEnvironmentEvidence(
        historical,
        local,
        tuple(matched),
        tuple(mismatched),
        tuple(unavailable),
        setup_warnings,
    )


@dataclass(frozen=True, slots=True)
class TransactionReplayResult:
    """Historical and local evidence for one planned transaction."""

    historical_transaction: Transaction
    historical_receipt: TransactionReceipt
    replay_request: ReplayTransactionRequest
    submission_status: ReplaySubmissionStatus
    local_transaction_hash: str | None
    replay_receipt: TransactionReceipt | None
    comparison: ReplayComparison | None
    error: str | None


@dataclass(frozen=True, slots=True)
class ObservedReplayReport:
    """Complete prefix/target observed replay evidence."""

    plan: ReplayPlan
    backend: str
    backend_version: str
    configured_hardfork: str
    environment: ReplayEnvironmentEvidence
    transactions: tuple[TransactionReplayResult, ...]
    prefix_receipt_evidence_exact: bool
    target_state_reliable: bool
    pair_execution_exact_match: bool
    replay_deviations: tuple[str, ...]

    @property
    def prefix(self) -> tuple[TransactionReplayResult, ...]:
        return self.transactions[:-1]

    @property
    def target(self) -> TransactionReplayResult:
        return self.transactions[-1]


def assemble_observed_replay_report(
    plan: ReplayPlan,
    backend_version: str,
    configured_hardfork: str,
    environment: ReplayEnvironmentEvidence,
    transactions: tuple[TransactionReplayResult, ...],
) -> ObservedReplayReport:
    """Build reliability labels from explicit prefix and target evidence."""
    if len(transactions) != len(plan.transactions):
        raise ReplayInputError("replay results do not align with the planned transactions")
    prefix_exact = all(
        result.comparison is not None
        and result.comparison.receipt_semantics_exact_match
        for result in transactions[:-1]
    )
    target = transactions[-1]
    target_reliable = prefix_exact and target.comparison is not None
    pair_exact = bool(
        target_reliable
        and target.comparison is not None
        and target.comparison.pair_execution_exact_match
    )
    return ObservedReplayReport(
        plan=plan,
        backend="Anvil",
        backend_version=backend_version,
        configured_hardfork=configured_hardfork,
        environment=environment,
        transactions=transactions,
        prefix_receipt_evidence_exact=prefix_exact,
        target_state_reliable=target_reliable,
        pair_execution_exact_match=pair_exact,
        replay_deviations=(
            "historical senders are impersonated; original signatures are not replayed",
            "eth_sendTransaction creates replay transaction hashes that may differ historically",
            "transactions are FIFO queued and manually mined together in one local block",
            "sender nonces and balances are not modified by BlockScope",
            f"Anvil EVM hardfork is explicitly pinned to {configured_hardfork}",
        ),
    )


class JsonRpcClient:
    """Minimal synchronous JSON-RPC client for the local Anvil process."""

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        self.url = url
        self.timeout = timeout
        self._request_id = 0

    def call(self, method: str, params: list[Any] | None = None) -> Any:
        self._request_id += 1
        try:
            response = httpx.post(
                self.url,
                json={
                    "jsonrpc": "2.0",
                    "id": self._request_id,
                    "method": method,
                    "params": [] if params is None else params,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ReplayRPCError(f"Anvil RPC {method} failed: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise ReplayRPCError(f"Anvil RPC {method} returned a non-object response")
        if payload.get("error") is not None:
            raise ReplayRPCError(f"Anvil RPC {method} failed: {payload['error']}")
        if "result" not in payload:
            raise ReplayRPCError(f"Anvil RPC {method} returned no result")
        return payload["result"]


class AnvilFork:
    """One manually mined historical Anvil fork with guaranteed process cleanup."""

    def __init__(
        self,
        upstream_rpc_url: str,
        fork_block_number: int,
        chain_id: int,
        *,
        startup_timeout: float = 10.0,
        receipt_timeout: float = 10.0,
        executable: str = "anvil",
        hardfork: str = "latest",
    ) -> None:
        self.upstream_rpc_url = upstream_rpc_url
        self.fork_block_number = fork_block_number
        self.chain_id = chain_id
        self.startup_timeout = startup_timeout
        self.receipt_timeout = receipt_timeout
        self.executable = executable
        self.hardfork = hardfork
        self.backend_version = "unknown"
        self.local_rpc_url: str | None = None
        self._process: subprocess.Popen[str] | None = None
        self._stderr: TextIO | None = None
        self._rpc: JsonRpcClient | None = None

    @classmethod
    def require_available(cls, executable: str = "anvil") -> tuple[str, str]:
        """Resolve Anvil and return its path/version, or an actionable error."""
        path = shutil.which(executable)
        if path is None:
            raise AnvilUnavailableError(
                "Anvil executable was not found on PATH. Install Foundry/Anvil separately, "
                "then verify with: anvil --version"
            )
        try:
            result = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AnvilUnavailableError(f"Could not run {path} --version: {exc}") from exc
        version = (result.stdout or result.stderr).strip()
        if result.returncode != 0:
            raise AnvilUnavailableError(
                f"{path} --version exited with {result.returncode}: {version}"
            )
        return path, version

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            return int(listener.getsockname()[1])

    def __enter__(self) -> Self:
        path, self.backend_version = self.require_available(self.executable)
        port = self._free_port()
        self.local_rpc_url = f"http://127.0.0.1:{port}"
        self._stderr = tempfile.TemporaryFile(mode="w+t")
        command = [
            path,
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--fork-url",
            self.upstream_rpc_url,
            "--fork-block-number",
            str(self.fork_block_number),
            "--chain-id",
            str(self.chain_id),
            "--hardfork",
            self.hardfork,
            "--no-mining",
            "--order",
            "fifo",
            "--silent",
        ]
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=self._stderr,
                text=True,
            )
        except OSError as exc:
            self.close()
            raise AnvilUnavailableError(f"Could not start Anvil: {exc}") from exc
        self._rpc = JsonRpcClient(self.local_rpc_url)
        deadline = time.monotonic() + self.startup_timeout
        last_error = "Anvil RPC did not become ready"
        try:
            while time.monotonic() < deadline:
                if self._process.poll() is not None:
                    last_error = (
                        f"Anvil exited during startup with code {self._process.returncode}"
                    )
                    break
                try:
                    self._rpc.call("eth_chainId")
                    return self
                except ReplayRPCError as exc:
                    last_error = str(exc)
                    time.sleep(0.05)
        except BaseException:
            self.close()
            raise
        stderr = self._stderr_text()
        self.close()
        detail = f"; stderr: {stderr}" if stderr else ""
        raise AnvilUnavailableError(f"Anvil startup failed: {last_error}{detail}")

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _stderr_text(self) -> str:
        if self._stderr is None:
            return ""
        try:
            self._stderr.flush()
            self._stderr.seek(0)
            return self._stderr.read().strip()
        except OSError:
            return ""

    def close(self) -> None:
        process = self._process
        try:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        finally:
            self._process = None
            if self._stderr is not None:
                self._stderr.close()
                self._stderr = None
            self._rpc = None

    def _call(self, method: str, params: list[Any] | None = None) -> Any:
        if self._rpc is None:
            raise ReplayRPCError("Anvil fork is not running")
        return self._rpc.call(method, params)

    def configure_next_block(self, context: ReplayBlockContext) -> tuple[str, ...]:
        """Best-effort exact block controls; return every unsupported/failed control."""
        controls: list[tuple[str, list[Any]]] = [
            ("evm_setNextBlockTimestamp", [context.timestamp]),
            ("evm_setBlockGasLimit", [_quantity(context.gas_limit)]),
        ]
        if context.base_fee_per_gas is not None:
            controls.append(
                ("anvil_setNextBlockBaseFeePerGas", [_quantity(context.base_fee_per_gas)])
            )
        if context.coinbase is not None:
            controls.append(("anvil_setCoinbase", [context.coinbase]))
        if context.prevrandao is not None:
            controls.append(("anvil_setPrevRandao", [context.prevrandao]))
        warnings: list[str] = []
        for method, params in controls:
            try:
                result = self._call(method, params)
                if result is False:
                    warnings.append(f"{method} returned false")
            except ReplayRPCError as exc:
                warnings.append(str(exc))
        return tuple(warnings)

    def send_impersonated(
        self,
        request: ReplayTransactionRequest,
    ) -> tuple[str, tuple[str, ...]]:
        """Submit exactly one historical sender request without funding/resetting it."""
        started = self._call("anvil_impersonateAccount", [request.from_address])
        if started is False:
            raise ReplayRPCError(f"Anvil refused to impersonate {request.from_address}")
        warnings: list[str] = []
        try:
            local_hash = self._call("eth_sendTransaction", [request.to_rpc()])
        finally:
            try:
                stopped = self._call("anvil_stopImpersonatingAccount", [request.from_address])
                if stopped is False:
                    warnings.append(f"Anvil did not stop impersonating {request.from_address}")
            except ReplayRPCError as exc:
                warnings.append(str(exc))
        if not isinstance(local_hash, str):
            raise ReplayRPCError("eth_sendTransaction did not return a transaction hash")
        return local_hash, tuple(warnings)

    def mine(self) -> None:
        """Mine all FIFO-queued prefix/target transactions into one controlled block."""
        self._call("evm_mine")

    def get_receipt(self, transaction_hash: str) -> TransactionReceipt:
        deadline = time.monotonic() + self.receipt_timeout
        while time.monotonic() < deadline:
            result = self._call("eth_getTransactionReceipt", [transaction_hash])
            if result is not None:
                if not isinstance(result, Mapping):
                    raise ReplayRPCError("Anvil returned a malformed transaction receipt")
                return TransactionReceipt.from_rpc(result)
            time.sleep(0.05)
        raise ReplayRPCError(f"Timed out waiting for replay receipt {transaction_hash}")

    def get_block(self, block_number: int) -> Block:
        result = self._call("eth_getBlockByNumber", [_quantity(block_number), True])
        if not isinstance(result, Mapping):
            raise ReplayRPCError(f"Anvil did not return replay block {block_number}")
        return Block.from_rpc(result)

    def get_chain_id(self) -> int:
        result = self._call("eth_chainId")
        if isinstance(result, bool) or not isinstance(result, (int, str)):
            raise ReplayRPCError("Anvil returned a malformed chain ID")
        try:
            return int(result, 0) if isinstance(result, str) else result
        except ValueError as exc:
            raise ReplayRPCError(f"Anvil returned a malformed chain ID: {result!r}") from exc


def _not_attempted_result(
    transaction: Transaction,
    receipt: TransactionReceipt,
    request: ReplayTransactionRequest,
    status: ReplaySubmissionStatus,
    error: str,
) -> TransactionReplayResult:
    return TransactionReplayResult(
        transaction,
        receipt,
        request,
        status,
        None,
        None,
        None,
        error,
    )


def replay_observed_transaction(
    upstream_rpc: EthereumRPC,
    upstream_rpc_url: str,
    block_number: int,
    target_transaction_index: int,
    *,
    anvil_executable: str = "anvil",
) -> ObservedReplayReport:
    """Replay a complete block prefix and target together from historical N-1 state."""
    executable_path, version = AnvilFork.require_available(anvil_executable)
    block = upstream_rpc.get_block(block_number)
    plan = plan_observed_replay(block, target_transaction_index)
    requests = tuple(transaction_replay_request(transaction) for transaction in plan.transactions)
    historical_receipts = tuple(
        upstream_rpc.get_transaction_receipt(transaction.hash)
        for transaction in plan.transactions
    )
    chain_id = upstream_rpc.get_chain_id()
    if chain_id != 1:
        raise ReplayInputError(
            f"observed Ethereum replay requires mainnet chain ID 1; connected chain ID is {chain_id}"
        )
    for request in requests:
        if request.chain_id is not None and request.chain_id != chain_id:
            raise ReplayInputError(
                f"transaction chain ID {request.chain_id} does not match upstream chain ID {chain_id}"
            )
    hardfork = infer_ethereum_hardfork(block)
    historical_context = ReplayBlockContext.from_block(block, chain_id)
    local_hashes: list[str] = []
    submission_error: tuple[int, str] | None = None
    setup_warnings: list[str] = []
    replay_receipts: dict[int, TransactionReceipt] = {}
    local_context: ReplayBlockContext | None = None

    with AnvilFork(
        upstream_rpc_url,
        plan.fork_block_number,
        chain_id,
        executable=executable_path,
        hardfork=hardfork,
    ) as fork:
        version = fork.backend_version
        setup_warnings.extend(fork.configure_next_block(historical_context))
        for position, request in enumerate(requests):
            try:
                local_hash, warnings = fork.send_impersonated(request)
                local_hashes.append(local_hash)
                setup_warnings.extend(warnings)
            except ReplayError as exc:
                submission_error = (position, str(exc))
                break
        if local_hashes:
            fork.mine()
            for position, local_hash in enumerate(local_hashes):
                try:
                    replay_receipts[position] = fork.get_receipt(local_hash)
                except (ReplayError, ValueError, TypeError) as exc:
                    setup_warnings.append(str(exc))
            try:
                local_block = fork.get_block(block_number)
                local_context = ReplayBlockContext.from_block(local_block, fork.get_chain_id())
            except (ReplayError, ValueError, TypeError) as exc:
                setup_warnings.append(str(exc))

    results: list[TransactionReplayResult] = []
    for position, (transaction, historical_receipt, request) in enumerate(
        zip(plan.transactions, historical_receipts, requests, strict=True)
    ):
        if submission_error is not None and position == submission_error[0]:
            results.append(
                _not_attempted_result(
                    transaction,
                    historical_receipt,
                    request,
                    ReplaySubmissionStatus.SUBMISSION_FAILED,
                    submission_error[1],
                )
            )
            continue
        if position >= len(local_hashes):
            results.append(
                _not_attempted_result(
                    transaction,
                    historical_receipt,
                    request,
                    ReplaySubmissionStatus.NOT_ATTEMPTED,
                    "not submitted because an earlier replay transaction failed",
                )
            )
            continue
        replay_receipt = replay_receipts.get(position)
        comparison = (
            None
            if replay_receipt is None
            else compare_replay_receipts(historical_receipt, replay_receipt)
        )
        results.append(
            TransactionReplayResult(
                transaction,
                historical_receipt,
                request,
                ReplaySubmissionStatus.REPLAYED,
                local_hashes[position],
                replay_receipt,
                comparison,
                None if replay_receipt is not None else "replay receipt unavailable",
            )
        )

    environment = compare_replay_environment(
        historical_context,
        local_context,
        tuple(setup_warnings),
    )
    return assemble_observed_replay_report(
        plan,
        version,
        hardfork,
        environment,
        tuple(results),
    )
