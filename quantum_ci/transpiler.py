"""
transpiler.py - QPU resource optimization via transpilation fidelity analysis.

Compares how efficiently PR and base circuits map to native hardware gates.
A "transpilation fidelity decay" occurs when the PR version requires
significantly more 2-qubit entangling gates (CX, ECR, CZ, …) after
transpilation.  These gates are the dominant QPU cost driver — they are
~10–100× noisier than single-qubit gates and directly determine shot-budget
requirements.  Blocking merges that exceed the 15% decay threshold prevents
QPU time waste equivalent to ~$2k/month on a mid-tier provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from qiskit import QuantumCircuit, transpile
from qiskit.providers.fake_provider import GenericBackendV2

# All standard 2-qubit entangling gate names recognised across Qiskit backends.
_2Q_GATES = frozenset({"cx", "ecr", "cz", "iswap", "rzz", "rxx", "ryy", "cp"})

_TRANSPILE_SEED = 42          # fixed so results are reproducible across runs
_OPTIMIZATION_LEVEL = 1       # moderate — mirrors typical CI transpile settings


# ── Data classes ────────────────────────────────────────────────────────────────


@dataclass
class TranspilationStats:
    """Transpilation metrics for a single circuit on a generic backend."""

    backend_name: str
    num_qubits: int
    original_2q_count: int
    transpiled_2q_count: int
    original_depth: int
    transpiled_depth: int
    # Relative increase in 2Q gates from logical → native representation.
    overhead_pct: float


@dataclass
class FidelityDecayResult:
    """
    Comparison of transpilation overhead between the PR and base branches.

    ``decay_pct`` is positive when the PR circuit requires more 2-qubit
    native gates than the base, indicating a QPU cost regression.
    """

    pr_stats: TranspilationStats
    base_stats: Optional[TranspilationStats]
    # (pr_transpiled_2q - base_transpiled_2q) / max(base_transpiled_2q, 1) * 100
    decay_pct: float
    exceeds_threshold: bool
    threshold_pct: float


# ── Helpers ────────────────────────────────────────────────────────────────────


def _count_2q(circuit: QuantumCircuit) -> int:
    """Return the total 2-qubit entangling gate count."""
    ops = circuit.count_ops()
    return sum(ops.get(g, 0) for g in _2Q_GATES)


def _transpile(circuit: QuantumCircuit) -> tuple[QuantumCircuit, str]:
    """
    Transpile *circuit* to a generic backend sized to the circuit's qubit count.

    Returns (transpiled_circuit, backend_name).  Uses a fixed seed and
    optimization level 1 so that back-to-back runs produce identical results.
    """
    n = max(circuit.num_qubits, 2)          # GenericBackendV2 requires ≥ 2 qubits
    backend = GenericBackendV2(num_qubits=n)
    transpiled = transpile(
        circuit,
        backend=backend,
        optimization_level=_OPTIMIZATION_LEVEL,
        seed_transpiler=_TRANSPILE_SEED,
    )
    return transpiled, backend.name


# ── Public API ─────────────────────────────────────────────────────────────────


def analyze_transpilation(circuit: QuantumCircuit) -> TranspilationStats:
    """
    Measure the native-gate overhead introduced when *circuit* is compiled to
    a generic QPU backend.

    Parameters
    ----------
    circuit:
        Logical QuantumCircuit (before transpilation).

    Returns
    -------
    TranspilationStats with before/after gate counts and depths.
    """
    original_2q = _count_2q(circuit)
    original_depth = circuit.depth()

    transpiled, backend_name = _transpile(circuit)
    transpiled_2q = _count_2q(transpiled)
    transpiled_depth = transpiled.depth()

    overhead_pct = (transpiled_2q - original_2q) / max(original_2q, 1) * 100

    return TranspilationStats(
        backend_name=backend_name,
        num_qubits=circuit.num_qubits,
        original_2q_count=original_2q,
        transpiled_2q_count=transpiled_2q,
        original_depth=original_depth,
        transpiled_depth=transpiled_depth,
        overhead_pct=round(overhead_pct, 2),
    )


def compute_fidelity_decay(
    pr_circuit: QuantumCircuit,
    base_circuit: Optional[QuantumCircuit],
    threshold_pct: float = 15.0,
) -> FidelityDecayResult:
    """
    Compare PR vs base transpilation to detect QPU resource regressions.

    ``decay_pct`` = (PR transpiled 2Q gates − base transpiled 2Q gates)
                    / max(base transpiled 2Q gates, 1) × 100

    A positive value means the PR version is more expensive.  The pipeline
    blocks the merge when ``decay_pct > threshold_pct``.

    Parameters
    ----------
    pr_circuit:
        Circuit from the PR branch.
    base_circuit:
        Circuit from the base branch, or None if this is a new circuit.
    threshold_pct:
        Maximum tolerated percentage increase in 2Q gate count (default 15%).
    """
    pr_stats = analyze_transpilation(pr_circuit)
    base_stats = (
        analyze_transpilation(base_circuit) if base_circuit is not None else None
    )

    if base_stats is None or base_stats.transpiled_2q_count == 0:
        decay_pct = 0.0
    else:
        decay_pct = (
            (pr_stats.transpiled_2q_count - base_stats.transpiled_2q_count)
            / base_stats.transpiled_2q_count
            * 100
        )

    return FidelityDecayResult(
        pr_stats=pr_stats,
        base_stats=base_stats,
        decay_pct=round(decay_pct, 2),
        exceeds_threshold=decay_pct > threshold_pct,
        threshold_pct=threshold_pct,
    )
