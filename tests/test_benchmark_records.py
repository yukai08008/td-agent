from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_reg_001_performance_baseline_is_machine_readable_and_indexed():
    stem = "2026-08-09-reg-001-deepseek-v4-flash"
    data_path = ROOT / "docs" / "benchmarks" / f"{stem}.json"
    report_path = ROOT / "docs" / "benchmarks" / f"{stem}.md"
    index_path = ROOT / "docs" / "benchmarks" / "README.md"

    data = json.loads(data_path.read_text(encoding="utf-8"))

    assert data["schema_version"] == "1.0"
    assert data["case_id"] == "REG-001"
    assert data["toe_dac"]["terminal_state"] == "succeeded"
    assert data["toe_dac"]["wall_seconds"] > data["direct"]["estimated_full_median_seconds"]
    assert len(data["direct"]["model_samples"]) == 3
    assert report_path.is_file()
    assert f"{stem}.md" in index_path.read_text(encoding="utf-8")
