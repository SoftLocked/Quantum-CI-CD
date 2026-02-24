"""
cli.py - Quantum CI pipeline orchestrator.

Invoked by the GitHub Actions workflow:

    python -m quantum_ci.cli \\
        --pr-path   /workspace/pr_branch   \\
        --base-path /workspace/base_branch \\
        --pr-number 42                     \\
        --repo      owner/repo

Pipeline stages
---------------
1.  Load PR config (quantum-ci.yaml).  Exit 0 gracefully if absent.
2.  Build check — PR branch (hermetic importlib sandbox).
3.  Complexity analysis — PR branch.
4.  Load base config (optional — new circuits have no base).
5.  Build check + complexity analysis — base branch.
6.  Transpilation fidelity analysis — both branches → FidelityDecayResult.
7.  Shot simulation (primary, statevector backend).
8.  Integration testing — cross-backend consistency check (PR branch only).
9.  Total Variation Distance — PR vs base distributions.
10. Regression gate — aggregates checks, determines exit code.
11. Telemetry — record metrics to OTLP and JSONL history file.
12. Build and upsert PR comment.

Exit codes
----------
0   All checks passed (or informational-only warnings).
1   PR circuit fails to build  OR  any regression check fails.
    This causes GitHub to block the merge of the required status check.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path
from typing import Optional

import yaml

from .analyzer import CircuitStats, analyze_circuit
from .loader import CircuitLoadError, load_circuit
from .regression import RegressionResult, run_regression_checks
from .reporter import build_comment, upsert_comment
from .runner import IntegrationTestResult, compute_tvd, run_integration_tests, run_shots
from .telemetry import HISTORY_FILE_DEFAULT, TelemetryRecorder
from .transpiler import FidelityDecayResult, compute_fidelity_decay

CONFIG_FILENAME = "quantum-ci.yaml"

# Defaults (overridden by quantum-ci.yaml and/or CLI flags)
DEFAULT_SHOTS = 1024
DEFAULT_SEED = 42
DEFAULT_TVD_WARN = 0.1
DEFAULT_TVD_BLOCK = 0.1
DEFAULT_DECAY_THRESHOLD = 15.0


# ── Config loading ──────────────────────────────────────────────────────────────


def _load_config(root: Path) -> Optional[dict]:
    """
    Parse quantum-ci.yaml from *root*.  Returns None if absent.
    Raises ValueError on malformed YAML or missing required keys.
    """
    path = root / CONFIG_FILENAME
    if not path.exists():
        return None

    with path.open() as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ValueError(f"{path} is not a YAML mapping")

    circuit_cfg = raw.get("circuit")
    if not circuit_cfg:
        raise ValueError(f"{path}: missing required 'circuit' section")
    for key in ("module", "function"):
        if key not in circuit_cfg:
            raise ValueError(f"{path}: missing required 'circuit.{key}'")

    return raw


# ── CLI argument parsing ────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Quantum CI — automated quantum circuit analysis")
    p.add_argument("--pr-path",                 required=True,  help="Path to PR branch checkout")
    p.add_argument("--base-path",               required=True,  help="Path to base branch checkout")
    p.add_argument("--pr-number",               required=True,  type=int)
    p.add_argument("--repo",                    required=True,  help="owner/repo")
    p.add_argument("--otlp-endpoint",           default="",     help="OTLP HTTP endpoint, e.g. http://localhost:4318")
    p.add_argument("--history-file",            default="",     help="Path to JSONL history file for drift tracking")
    p.add_argument("--tvd-block-threshold",     default=None,   type=float, help="Override tvd_block_threshold from config")
    p.add_argument("--transpilation-decay-threshold", default=None, type=float, help="Override transpilation_decay_threshold_pct")
    return p.parse_args()


# ── Minimal comment helper ──────────────────────────────────────────────────────


def _post_minimal(message: str, repo: str, pr_number: int, token: str) -> None:
    body = f"<!-- quantum-ci-report -->\n## ⚛️ Quantum CI\n\n{message}"
    try:
        upsert_comment(repo, pr_number, token, body)
    except Exception:
        pass
    sys.exit(0)


# ── Main pipeline ───────────────────────────────────────────────────────────────


def main() -> None:  # noqa: C901  (intentionally long — pipeline stages are sequential)
    args = _parse_args()
    pr_path   = Path(args.pr_path).resolve()
    base_path = Path(args.base_path).resolve()
    pr_number = args.pr_number
    repo      = args.repo
    token     = os.environ.get("GITHUB_TOKEN", "")

    # Observability
    otlp_endpoint = args.otlp_endpoint or os.environ.get("QUANTUM_CI_OTLP_ENDPOINT", "")
    history_file  = (
        Path(args.history_file) if args.history_file else HISTORY_FILE_DEFAULT
    )
    tel = TelemetryRecorder(
        otlp_endpoint=otlp_endpoint or None,
        history_file=history_file,
    )

    # ── Mutable pipeline state ──────────────────────────────────────────────────
    pr_build_ok:   bool                        = False
    pr_error:      Optional[str]               = None
    pr_stats:      Optional[CircuitStats]      = None
    pr_dist:       Optional[dict]              = None
    pr_circuit = None

    base_build_ok: Optional[bool]              = None
    base_error:    Optional[str]               = None
    base_stats:    Optional[CircuitStats]      = None
    base_dist:     Optional[dict]              = None
    base_circuit = None

    tvd:           Optional[float]             = None
    decay_result:  Optional[FidelityDecayResult] = None
    integration:   Optional[IntegrationTestResult] = None
    regression:    Optional[RegressionResult]  = None

    shots          = DEFAULT_SHOTS
    seed           = DEFAULT_SEED
    tvd_warn       = DEFAULT_TVD_WARN
    pr_config: dict = {}

    try:
        with tel.span("quantum-ci.pipeline"):

            # ── 1. Load PR config ─────────────────────────────────────────────
            pr_config = _load_config(pr_path)  # type: ignore[assignment]
            if pr_config is None:
                _post_minimal(
                    "PR branch has no `quantum-ci.yaml`. Nothing to analyse.",
                    repo, pr_number, token,
                )
                return

            c_cfg  = pr_config["circuit"]
            shots  = int(pr_config.get("runner", {}).get("shots", DEFAULT_SHOTS))
            seed   = int(pr_config.get("runner", {}).get("seed",  DEFAULT_SEED))
            tvd_warn = float(
                pr_config.get("analysis", {}).get("tvd_warn_threshold", DEFAULT_TVD_WARN)
            )

            # CLI overrides (action inputs take precedence over config file)
            if args.tvd_block_threshold is not None:
                pr_config.setdefault("analysis", {})
                pr_config["analysis"]["tvd_block_threshold"] = args.tvd_block_threshold
            if args.transpilation_decay_threshold is not None:
                pr_config.setdefault("analysis", {})
                pr_config["analysis"]["transpilation_decay_threshold_pct"] = (
                    args.transpilation_decay_threshold
                )

            # ── 2. Build check — PR branch ────────────────────────────────────
            with tel.span("quantum-ci.build.pr"):
                try:
                    pr_circuit  = load_circuit(
                        pr_path, c_cfg["module"], c_cfg["function"],
                        c_cfg.get("kwargs", {}),
                    )
                    pr_build_ok = True
                except CircuitLoadError as exc:
                    pr_error = str(exc)

            # ── 3. Complexity analysis — PR branch ────────────────────────────
            if pr_build_ok:
                pr_stats = analyze_circuit(pr_circuit)
                tel.record(
                    circuit_depth_pr=pr_stats.depth,
                    circuit_qubits_pr=pr_stats.num_qubits,
                    circuit_gates_pr=pr_stats.size,
                )

            # ── 4 & 5. Base branch (optional) ────────────────────────────────
            base_config = _load_config(base_path)
            if base_config is not None:
                bc_cfg = base_config["circuit"]
                with tel.span("quantum-ci.build.base"):
                    try:
                        base_circuit  = load_circuit(
                            base_path, bc_cfg["module"], bc_cfg["function"],
                            bc_cfg.get("kwargs", {}),
                        )
                        base_build_ok = True
                    except CircuitLoadError as exc:
                        base_build_ok = False
                        base_error    = str(exc)

                if base_build_ok:
                    base_stats = analyze_circuit(base_circuit)

            # ── 6. Transpilation fidelity decay ───────────────────────────────
            if pr_build_ok:
                with tel.span("quantum-ci.transpilation"):
                    decay_result = compute_fidelity_decay(
                        pr_circuit,
                        base_circuit if base_build_ok else None,
                        threshold_pct=float(
                            pr_config.get("analysis", {}).get(
                                "transpilation_decay_threshold_pct",
                                DEFAULT_DECAY_THRESHOLD,
                            )
                        ),
                    )
                    tel.record(
                        transpilation_decay_pct=decay_result.decay_pct,
                        transpiled_2q_gates_pr=decay_result.pr_stats.transpiled_2q_count,
                    )

            # ── 7. Shot simulation ────────────────────────────────────────────
            if pr_build_ok:
                with tel.span("quantum-ci.simulation.pr"):
                    pr_dist = run_shots(pr_circuit, shots=shots, seed=seed)

            if base_build_ok:
                with tel.span("quantum-ci.simulation.base"):
                    base_dist = run_shots(base_circuit, shots=shots, seed=seed)

            # ── 8. Integration testing (cross-backend consistency) ────────────
            if pr_build_ok:
                with tel.span("quantum-ci.integration"):
                    integration = run_integration_tests(
                        pr_circuit, shots=shots, seed=seed
                    )

            # ── 9. Total Variation Distance ───────────────────────────────────
            if pr_dist is not None and base_dist is not None:
                tvd = compute_tvd(pr_dist, base_dist)
                tel.record(tvd=tvd)

            # ── 10. Regression gate ───────────────────────────────────────────
            regression = run_regression_checks(
                pr_stats=pr_stats,
                base_stats=base_stats,
                tvd=tvd,
                decay_result=decay_result,
                config=pr_config,
            )
            tel.record(regression_passed=int(regression.passed))

    except Exception:
        pr_error = traceback.format_exc()

    # ── 11. Telemetry flush ────────────────────────────────────────────────────
    try:
        tel.flush()
    except Exception as exc:
        print(f"Quantum CI WARNING: telemetry flush failed: {exc}", file=sys.stderr)

    # ── 12. Build and post PR comment ──────────────────────────────────────────
    drift = tel.compute_drift("tvd")
    body = build_comment(
        pr_build_ok=pr_build_ok,
        pr_error=pr_error,
        pr_stats=pr_stats,
        pr_dist=pr_dist,
        base_build_ok=base_build_ok,
        base_error=base_error,
        base_stats=base_stats,
        base_dist=base_dist,
        tvd=tvd,
        tvd_warn_threshold=tvd_warn,
        shots=shots,
        decay_result=decay_result,
        integration=integration,
        regression=regression,
        drift=drift,
    )

    try:
        upsert_comment(repo, pr_number, token, body)
        print("Quantum CI: comment posted.")
    except Exception as exc:
        print(f"Quantum CI WARNING: failed to post comment: {exc}", file=sys.stderr)

    # ── Determine exit code ────────────────────────────────────────────────────
    if not pr_build_ok:
        print("Quantum CI: PR circuit build failed — blocking merge.", file=sys.stderr)
        sys.exit(1)

    if regression is not None and not regression.passed:
        failed = ", ".join(c.name for c in regression.failed_checks)
        print(
            f"Quantum CI: regression checks failed ({failed}) — blocking merge.",
            file=sys.stderr,
        )
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
