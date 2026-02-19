"""
analyzer.py - Circuit complexity analysis.

Collects structural metrics from a QuantumCircuit without running it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from qiskit import QuantumCircuit


@dataclass
class CircuitStats:
    depth: int
    width: int          # num_qubits + num_clbits
    num_qubits: int
    num_clbits: int
    size: int           # total instruction count (includes measurements)
    gate_counts: dict[str, int] = field(default_factory=dict)
    has_measurements: bool = False

    @property
    def cx_count(self) -> int:
        return self.gate_counts.get("cx", 0)

    @property
    def t_count(self) -> int:
        return self.gate_counts.get("t", 0)


def analyze_circuit(circuit: QuantumCircuit) -> CircuitStats:
    """
    Extract complexity metrics from a QuantumCircuit.

    Metrics are collected from the circuit as-is (before any measurement
    injection). The 'measure' and 'barrier' instructions are excluded from
    the displayed gate_counts so the table reflects only computational gates.
    """
    all_ops: dict[str, int] = dict(circuit.count_ops())
    display_ops = {
        k: v for k, v in all_ops.items()
        if k not in ("measure", "barrier", "reset")
    }

    return CircuitStats(
        depth=circuit.depth(),
        width=circuit.num_qubits + circuit.num_clbits,
        num_qubits=circuit.num_qubits,
        num_clbits=circuit.num_clbits,
        size=circuit.size(),
        gate_counts=display_ops,
        has_measurements=bool(circuit.clbits),
    )
