"""Run BlockScope's staged, deterministic corpus evaluation."""

from __future__ import annotations

import argparse
from pathlib import Path

from blockscope.evaluation import (
    EvaluationConfiguration,
    render_evaluation_report,
    run_evaluation,
    write_evaluation_artifact,
)
from blockscope.rpc import EthereumRPC, rpc_url_from_env


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start-block", type=int, required=True)
    parser.add_argument("--end-block", type=int, required=True)
    parser.add_argument("--candidate-limit", type=int, default=50)
    parser.add_argument("--evm-limit", type=int, default=5)
    parser.add_argument("--evm-cooldown-seconds", type=int, default=0)
    parser.add_argument("--anvil", default="anvil")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configuration = EvaluationConfiguration(
        args.start_block,
        args.end_block,
        args.candidate_limit,
        args.evm_limit,
        args.evm_cooldown_seconds,
    )
    output = args.output or Path(
        f"artifacts/evaluation_{configuration.start_block}_{configuration.end_block}.json"
    )
    rpc_url = rpc_url_from_env()
    artifact = run_evaluation(
        EthereumRPC(rpc_url),
        rpc_url,
        configuration,
        anvil_executable=args.anvil,
        progress=lambda message: print(message, flush=True),
    )
    write_evaluation_artifact(artifact, output)
    print()
    print(render_evaluation_report(artifact))
    print(f"\nArtifact: {output}")


if __name__ == "__main__":
    main()
