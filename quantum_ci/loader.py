"""
loader.py - Hermetic importlib sandbox for side-by-side circuit loading.

Two copies of the same circuit module (PR branch and base branch) must be
independently loadable without colliding in sys.modules.  This module solves
the problem with a three-layer isolation strategy:

1. **Unique module name** — each load gets a name keyed to the SHA-256 of the
   absolute checkout path, so two checkouts in directories with identical
   names (e.g., both named "repo") never produce the same key.

2. **sys.path bracketing** — the checkout root is prepended to sys.path only
   for the duration of exec_module(), then removed immediately in a finally
   block regardless of success or failure.

3. **Full sys.modules scrub** — after exec_module() completes, every module
   key that was absent before the call is removed from sys.modules.  This
   prevents sibling imports inside the user's circuit package from leaking
   state between the PR and base runs.

The returned QuantumCircuit object is a plain Qiskit data structure whose
lifetime is independent of the now-cleaned module namespace.
"""

from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path
from typing import Any


class CircuitLoadError(Exception):
    """Raised when the circuit cannot be imported or instantiated."""


# ── Helpers ────────────────────────────────────────────────────────────────────


def _unique_module_name(root: Path, module_dotpath: str) -> str:
    """
    Build a collision-proof module name for a given (root, dotpath) pair.

    Uses the first 12 hex digits of the SHA-256 of the absolute root path
    rather than the directory name, so two checkouts at different absolute
    paths but with the same basename ("repo/", "repo/") never collide.
    """
    root_hash = hashlib.sha256(str(root).encode()).hexdigest()[:12]
    safe_module = module_dotpath.replace(".", "_")
    return f"_quantum_ci_{root_hash}_{safe_module}"


# ── Public API ─────────────────────────────────────────────────────────────────


def load_circuit(
    root_path: str | Path,
    module_dotpath: str,
    function_name: str,
    kwargs: dict[str, Any] | None = None,
) -> Any:  # returns QuantumCircuit
    """
    Load and instantiate a QuantumCircuit from a specific on-disk checkout
    using a hermetic importlib sandbox.

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

    # ── Resolve file path ──────────────────────────────────────────────────────
    rel_path = Path(*module_dotpath.split(".")).with_suffix(".py")
    abs_path = root / rel_path

    if not abs_path.exists():
        raise CircuitLoadError(f"Circuit file not found: {abs_path}")

    unique_name = _unique_module_name(root, module_dotpath)

    spec = importlib.util.spec_from_file_location(
        unique_name,
        abs_path,
        submodule_search_locations=[],
    )
    if spec is None or spec.loader is None:
        raise CircuitLoadError(f"Could not create module spec for {abs_path}")

    module = importlib.util.module_from_spec(spec)

    # ── Hermetic execution ─────────────────────────────────────────────────────
    # Snapshot sys.modules before touching anything so we can scrub every new
    # entry — including sibling imports — when we're done.
    pre_load_modules: set[str] = set(sys.modules.keys())

    sys.path.insert(0, str(root))
    try:
        # Register before exec_module so that intra-package relative imports
        # targeting the unique name can resolve (rare but defensive).
        sys.modules[unique_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:
        raise CircuitLoadError(
            f"Error while importing '{module_dotpath}': {exc}"
        ) from exc
    finally:
        # Always restore sys.path, even on failure.
        try:
            sys.path.remove(str(root))
        except ValueError:
            pass
        # Scrub ALL newly registered modules so that neither the circuit module
        # nor any of its sibling imports pollute the next load_circuit() call.
        for key in set(sys.modules.keys()) - pre_load_modules:
            sys.modules.pop(key, None)

    # ── Locate and invoke the circuit factory ──────────────────────────────────
    try:
        factory = getattr(module, function_name)
    except AttributeError as exc:
        raise CircuitLoadError(
            f"Module '{module_dotpath}' has no attribute '{function_name}'"
        ) from exc

    try:
        from qiskit import QuantumCircuit

        circuit = factory(**kwargs) if callable(factory) else factory

        if not isinstance(circuit, QuantumCircuit):
            raise CircuitLoadError(
                f"'{function_name}' returned {type(circuit).__name__}, "
                "expected QuantumCircuit"
            )
    except CircuitLoadError:
        raise
    except Exception as exc:
        raise CircuitLoadError(
            f"Error while calling '{function_name}': {exc}"
        ) from exc

    return circuit
