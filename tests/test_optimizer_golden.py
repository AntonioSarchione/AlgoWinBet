"""The TypeScript slip optimizer (web/lib/optimizer.ts) is checked against web/scripts/optimizer.golden.json. This test fails when
the Python optimizer (or the engine feeding it) changes without regenerating that file: run `python scripts/optimizer_golden.py`
and then `node scripts/optimizer-parity.mjs` in web/."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_golden_file_matches_the_python_optimizer():
    spec = importlib.util.spec_from_file_location("optimizer_golden", ROOT / "scripts" / "optimizer_golden.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    fresh = mod.build()
    saved = json.loads((ROOT / "web" / "scripts" / "optimizer.golden.json").read_text(encoding="utf-8"))
    assert fresh["settings"] == saved["settings"]
    assert [(c["name"], c["no_bet"], [s["legs"] for s in c["slips"]]) for c in fresh["cases"]] == \
           [(c["name"], c["no_bet"], [s["legs"] for s in c["slips"]]) for c in saved["cases"]]
    for fc, sc in zip(fresh["cases"], saved["cases"]):
        for fs, ss in zip(fc["slips"], sc["slips"]):
            assert fs["ev"] == pytest.approx(ss["ev"], rel=1e-9) and fs["stake"] == pytest.approx(ss["stake"], abs=0.011)
