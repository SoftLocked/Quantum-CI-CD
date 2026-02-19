from quantum_ci.loader import load_circuit
from quantum_ci.analyzer import analyze_circuit
from quantum_ci.runner import run_shots, compute_tvd
from quantum_ci.reporter import build_comment

root = "."  # use the repo itself as the "checkout"

circuit = load_circuit(root, "circuits.example_bell", "build_circuit")
print("Loaded:", circuit)

stats = analyze_circuit(circuit)
print("Depth:", stats.depth, "| Gates:", stats.gate_counts)

dist = run_shots(circuit, shots=512)
print("Top outcomes:", sorted(dist.items(), key=lambda x: -x[1])[:3])

# Simulate a comparison against itself (TVD should be ~0)
tvd = compute_tvd(dist, dist)
print("Self-TVD:", tvd)

# Print the comment markdown without posting it
comment = build_comment(
    pr_build_ok=True, pr_error=None, pr_stats=stats, pr_dist=dist,
    base_build_ok=True, base_error=None, base_stats=stats, base_dist=dist,
    tvd=tvd, tvd_threshold=0.1, shots=512,
)
print("\n--- COMMENT PREVIEW ---\n")
print(comment)