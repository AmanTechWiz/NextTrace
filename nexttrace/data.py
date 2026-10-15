"""Reads RCAEval RE1-OB cases. Layout and quirks: docs/data_notes.md."""
import os
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

DATA_DIR = Path(os.environ.get("NEXTTRACE_DATA", Path(__file__).parent.parent / "data" / "RE1-OB"))


@dataclass
class Case:
    id: str            # e.g. "currencyservice_disk/3"
    root_cause: str    # ground truth: scorer only, never put in the graph or a prompt
    fault: str
    inject_time: int   # epoch seconds
    metrics: pd.DataFrame  # column "time" + "<service>_<kind>" columns, 1 row per second

    def before(self) -> pd.DataFrame:
        return self.metrics[self.metrics.time < self.inject_time]

    def after(self) -> pd.DataFrame:
        return self.metrics[self.metrics.time >= self.inject_time]


def case_ids(data_dir: Path = DATA_DIR) -> list[str]:
    return sorted(f"{p.parent.parent.name}/{p.parent.name}" for p in data_dir.glob("*/*/data.csv"))


def load_case(case_id: str, data_dir: Path = DATA_DIR) -> Case:
    folder = data_dir / case_id
    df = pd.read_csv(folder / "data.csv")
    df = df[[c for c in df.columns if not c.startswith("time.")]]  # cpu/mem files repeat the time column
    root, fault = case_id.split("/")[0].rsplit("_", 1)
    return Case(case_id, root, fault, int((folder / "inject_time.txt").read_text().strip()), df)


def metric_service(column: str) -> str:
    return column.split("_", 1)[0]
