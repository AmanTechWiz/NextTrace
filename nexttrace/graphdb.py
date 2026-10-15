"""Small FalkorDB helpers shared by the mapper and the agents."""
import time


def ensure_unique(graph, label: str, prop: str, wait_s: float = 5.0) -> None:
    """Index + unique constraint on label.prop. Without them, parallel MERGE makes duplicates (BUILD_GUIDE 3.6).

    Call after the graph exists: FalkorDB rejects index calls on an empty key.
    """
    if not any(label in str(r) and prop in str(r) for r in graph.list_indices().result_set):
        graph.create_node_range_index(label, prop)
    if not any(label in str(c) and prop in str(c) for c in graph.list_constraints()):
        graph.create_node_unique_constraint(label, prop)
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:  # constraints build asynchronously
        if any(label in str(c) and "OPERATIONAL" in str(c) for c in graph.list_constraints()):
            return
        time.sleep(0.05)
    raise RuntimeError(f"unique constraint on {label}.{prop} not operational: {graph.list_constraints()}")
