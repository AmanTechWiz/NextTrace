"""Alarms from metrics: the input incidents start from.

Reads a metrics CSV in the common wide layout (a `time` column in epoch seconds, then one column per
`<service>_<metric>`, e.g. ts-order-service_latency-90) and flags metrics that moved far from their own baseline after
`since`. Service names are taken from the column prefixes and kept only if they are on the map, so this works for any
system whose metric names start with the service name. The detector is deliberately plain (z-score); its job is to
produce realistic alarm input, not to find the cause.
"""
import pandas as pd


def from_metrics(csv, since, known_services, threshold=3.0, top=20):
    """[{service, signal, at, z}] for metrics whose mean after `since` moved >= threshold standard deviations from the
    mean before it. Strongest first, at most `top`. Columns for services not on the map are ignored."""
    df = pd.read_csv(csv)
    if "time" not in df.columns:
        raise ValueError("metrics CSV needs a 'time' column (epoch seconds)")
    known = sorted(known_services, key=len, reverse=True)  # longest first: ts-order-other-service before ts-order
    before, after = df[df["time"] < since], df[df["time"] >= since]
    if before.empty or after.empty:
        raise ValueError(f"'since'={since} must fall inside the data ({df['time'].min()}..{df['time'].max()})")
    out = []
    for col in df.columns:
        service = next((s for s in known if col.startswith(s + "_")), None)
        if service is None:
            continue
        base, now = pd.to_numeric(before[col], errors="coerce"), pd.to_numeric(after[col], errors="coerce")
        sd = base.std()
        if not sd or pd.isna(sd):
            continue
        z = abs(now.mean() - base.mean()) / sd
        if z >= threshold:
            moved = now.sub(base.mean()).abs().gt(threshold * sd)
            first = int(after["time"][moved].iloc[0]) if moved.any() else int(since)
            out.append({"service": service, "signal": col[len(service) + 1:], "at": first, "z": round(float(z), 1)})
    return sorted(out, key=lambda a: -a["z"])[:top]
