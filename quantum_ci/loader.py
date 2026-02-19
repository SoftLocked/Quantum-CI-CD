"""
loader.py - Dynamically load a QuantumCircuit from an arbitrary on-disk path.

Two copies of the same circuit module (PR branch and base branch) must be
loadable independently without colliding in sys.modules.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


class CircuitLoadError(Exception):
    """Raised when the circuit cannot be imported or instantiated."""


def load_circuit(
    root_path: str | Path,
    module_dotpath: str,
    function_name: str,
    kwargs: dict[str, Any] | None = None,
) -> Any:  # returns QuantumCircuit
    """
    Load and instantiate a QuantumCircuit from a specific on-disk checkout.

    Parameters
    ----------
    root_path:
        Absolute path to the checkout root (e.g. /workspace/pr_branch).
    module_dotpath:
        Dotted module path relative to root_path (e.g. "circuits.bell").
    function_name:
        Name of the callable or QuantumCircuit attribute inside the module.
    kwargs:
        Optional keyword arguments forwarded to the callable.

    Returns
    -------
    QuantumCircuit instance.

    Raises
    ------
    CircuitLoadError
        If the file is missing, cannot be imported, or the callable fails.
    """
    root = Path(root_path).resolve()
    kwargs = kwargs or {}

    # Convert dotted path ("circuits.bell") to filesystem path (circuits/bell.py)
    rel_path = Path(*module_dotpath.split(".")).with_suffix(".py")
    abs_path = root / rel_path

    if not abs_path.exists():
        raise CircuitLoadError(f"Circuit file not found: {abs_path}")

    # Use a unique module name keyed to the root directory name so that
    # loading the same logical module from pr_branch and base_branch never
    # produces a sys.modules collision.
    safe_module = module_dotpath.replace(".", "_")
    unique_name = f"_quantum_ci_{root.name}_{safe_module}"

    spec = importlib.util.spec_from_file_location(
        unique_name,
        abs_path,
        submodule_search_locations=[],
    )
    if spec is None or spec.loader is None:
        raise CircuitLoadError(f"Could not create module spec for {abs_path}")

    module = importlib.util.module_from_spec(spec)

    # Temporarily add root to sys.path so that relative imports within the
    # user's circuit package resolve correctly (e.g. `from circuits import utils`).
    sys.path.insert(0, str(root))
    try:
        # Register before exec_module to support modules that import siblings.
        sys.modules[unique_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        raise CircuitLoadError(
            f"Error while importing '{module_dotpath}': {exc}"
        ) from exc
    finally:
        sys.path.remove(str(root))
        sys.modules.pop(unique_name, None)

    try:
        factory = getattr(module, function_name)
    except AttributeError as exc:
        raise CircuitLoadError(
            f"Module '{module_dotpath}' has no attribute '{function_name}'"
        ) from exc

    try:
        from qiskit import QuantumCircuit

        if callable(factory):
            circuit = factory(**kwargs)
        else:
            circuit = factory

        if not isinstance(circuit, QuantumCircuit):
            raise CircuitLoadError(
                f"'{function_name}' returned {type(circuit).__name__}, "
                f"expected QuantumCircuit"
            )
    except CircuitLoadError:
        raise
    except Exception as exc:
        raise CircuitLoadError(
            f"Error while calling '{function_name}': {exc}"
        ) from exc

    return circuit
