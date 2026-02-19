"""
cli.py - Main entry point for Quantum CI.

Invoked by the GitHub Actions workflow:

    python -m quantum_ci.cli \\
        --pr-path   /workspace/pr_branch   \\
        --base-path /workspace/base_branch \\
        --pr-number 42                     \\
        --repo      owner/repo

Reads GITHUB_TOKEN from the environment.
Always exits 0 (warn-only; results are posted as a PR comment).
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
from .reporter import build_comment, upsert_comment
from .runner import compute_tvd, run_shots

CONFIG_FILENAME = "quantum-ci.yaml"
DEFAULT_SHOTS = 1024
DEFAULT_SEED = 42
DEFAULT_TVD_THRESHOLD = 0.1


def _load_config(root: Path) -> Optional[dict]:
    """
    Parse quantum-ci.yaml from `root`. Returns None if absent.
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


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Quantum CI analysis tool")
    p.add_argument("--pr-path",   required=True)
    p.add_argument("--base-path", required=True)
    p.add_argument("--pr-number", required=True, type=int)
    p.add_argument("--repo",      required=True)
    return p.parse_args()


def main() -> None:
    args = _parse_args()
    pr_path   = Path(args.pr_path).resolve()
    base_path = Path(args.base_path).resolve()
    pr_number = args.pr_number
    repo      = args.repo
    token     = os.environ.get("GITHUB_TOKEN", "")

    # Mutable state collected across pipeline stages
    pr_build_ok:   bool                   = False
    pr_error:      Optional[str]          = None
    pr_stats:      Optional[CircuitStats] = None
    pr_dist:       Optional[dict]         = None

    base_build_ok: Optional[bool]         = None  # None = no base circuit at all
    base_error:    Optional[str]          = None
    base_stats:    Optional[CircuitStats] = None
    base_dist:     Optional[dict]         = None

    tvd:           Optional[float]        = None
    shots          = DEFAULT_SHOTS
    seed           = DEFAULT_SEED
    tvd_threshold  = DEFAULT_TVD_THRESHOLD

    try:
        # ── 1. Load PR config ─────────────────────────────────────────────────
        pr_config = _load_config(pr_path)
        if pr_config is None:
            _post_minimal(
                "PR branch has no `quantum-ci.yaml`. Nothing to analyze.",
                repo, pr_number, token,
            )
            return

        c_cfg = pr_config["circuit"]
        shots        = int(pr_config.get("runner", {}).get("shots", DEFAULT_SHOTS))
        seed         = int(pr_config.get("runner", {}).get("seed",  DEFAULT_SEED))
        tvd_threshold = float(
            pr_config.get("analysis", {}).get("tvd_warn_threshold", DEFAULT_TVD_THRESHOLD)
        )

        # ── 2. Build check — PR branch ────────────────────────────────────────
        try:
            pr_circuit  = load_circuit(pr_path, c_cfg["module"], c_cfg["function"],
                                       c_cfg.get("kwargs", {}))
            pr_build_ok = True
        except CircuitLoadError as exc:
            pr_error = str(exc)

        # ── 3. Complexity analysis — PR branch ────────────────────────────────
        if pr_build_ok:
            pr_stats = analyze_circuit(pr_circuit)

        # ── 4. Load base config (optional — may not exist for new circuits) ───
        base_config = _load_config(base_path)
        if base_config is not None:
            bc_cfg = base_config["circuit"]
            try:
                base_circuit  = load_circuit(base_path, bc_cfg["module"], bc_cfg["function"],
                                             bc_cfg.get("kwargs", {}))
                base_build_ok = True
            except CircuitLoadError as exc:
                base_build_ok = False
                base_error    = str(exc)

            if base_build_ok:
                base_stats = analyze_circuit(base_circuit)

        # ── 5. Shot simulation ────────────────────────────────────────────────
        if pr_build_ok:
            pr_dist = run_shots(pr_circuit, shots=shots, seed=seed)

        if base_build_ok:
            base_dist = run_shots(base_circuit, shots=shots, seed=seed)

        # ── 6. Total Variation Distance ───────────────────────────────────────
        if pr_dist is not None and base_dist is not None:
            tvd = compute_tvd(pr_dist, base_dist)

    except Exception:
        # Catch-all: config errors, unexpected failures — surface in comment.
        pr_error = traceback.format_exc()

    # ── 7. Format and post PR comment ─────────────────────────────────────────
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
        tvd_threshold=tvd_threshold,
        shots=shots,
    )

    try:
        upsert_comment(repo, pr_number, token, body)
        print("Quantum CI: comment posted.")
    except Exception as exc:
        print(f"Quantum CI WARNING: failed to post comment: {exc}", file=sys.stderr)

    sys.exit(0)


def _post_minimal(message: str, repo: str, pr_number: int, token: str) -> None:
    body = f"<!-- quantum-ci-report -->\n## ⚛️ Quantum CI\n\n{message}"
    try:
        upsert_comment(repo, pr_number, token, body)
    except Exception:
        pass
    sys.exit(0)


if __name__ == "__main__":
    main()
