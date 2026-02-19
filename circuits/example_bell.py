"""
example_bell.py - Example Bell state circuit.

Demonstrates the expected structure for circuits used with Quantum CI/CD.
The `build_circuit` function returns a QuantumCircuit with no measurements;
Quantum CI adds measurements automatically for simulation.

To experiment with the CI/CD pipeline, try modifying this circuit:
  - Add a T gate before the CX to shift the output distribution
  - Increase to 3 qubits with a GHZ state
  - Introduce a deliberate error (e.g. an extra X gate) to see TVD spike
"""

from qiskit import QuantumCircuit


def build_circuit() -> QuantumCircuit:
    """Build and return a 2-qubit Bell state |Φ+⟩ = (|00⟩ + |11⟩) / √2."""
    qc = QuantumCircuit(2)
    qc.h(0)
    qc.cx(0, 1)
    return qc
