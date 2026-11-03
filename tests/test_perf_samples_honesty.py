"""A perf sample is a measurement or it is not stored.

The demo path has no runtime behind it: it writes a reply this app composed and a usage line whose
figures are constants (29.5 or 42.1 tok/s, 155 prefill, 0.32 s to first token). The UI learned to
decline those figures long ago - every surface checks the `demo` flag before drawing a badge - but
the write path did not, so the constants landed in `perf_samples`, the table the throughput card
averages and that any later report about real hardware would read. This is that gap closed, and it
is tested here rather than only where it was found, because the record is the thing a reader trusts.

These tests never start a server: `record_perf` is the one place a sample enters the table, and the
call-site guard below is what keeps a future call site from bypassing it.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from pathlib import Path

import pytest

import app as studio

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEMO_CONSTANTS = {"decode_tps": 29.5, "prefill_tps": 155.0, "ttft": 0.32, "completion_tokens": 12}


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "perf.db"
    monkeypatch.setattr(studio, "DB_PATH", db_path)
    studio.init_db()
    return db_path


def _samples(db_path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT mode, model, decode_tps, prefill_tps, ttft, completion_tokens FROM perf_samples").fetchall()


def _record(db_path: Path, **overrides: object) -> None:
    kwargs: dict[str, object] = {"mode": "chat", "model": "demo-model", **DEMO_CONSTANTS}
    kwargs.update(overrides)
    studio.record_perf(**kwargs)  # type: ignore[arg-type]


def test_a_demo_reply_stores_no_sample(store: Path) -> None:
    _record(store, demo=True)
    assert _samples(store) == []


def test_a_measured_reply_still_stores_its_sample(store: Path) -> None:
    """The refusal has to be about the demo flag, not about distrusting the numbers."""
    _record(store, decode_tps=41.25, ttft=0.11, completion_tokens=64)
    rows = _samples(store)
    assert len(rows) == 1
    assert rows[0]["decode_tps"] == pytest.approx(41.25)
    assert rows[0]["completion_tokens"] == 64


def test_the_throughput_summary_never_averages_a_demo_reply(store: Path) -> None:
    """`/api/telemetry` reads this summary, and the card draws its leaderboard."""
    _record(store, demo=True)
    _record(store, decode_tps=30.0, completion_tokens=30)
    summary = studio._recent_perf_summary()
    models = [row["model"] for row in summary["leaderboard"]]
    assert models == ["demo-model"]
    # One sample, the measured one: the demo constants would have made 29.75 out of two.
    assert summary["leaderboard"][0]["runs"] == 1
    assert summary["leaderboard"][0]["avg_tps"] == pytest.approx(30.0)


def test_every_call_site_passes_the_demo_flag() -> None:
    """A new call site that forgets the flag would re-open the hole silently."""
    tree = ast.parse((PROJECT_ROOT / "app.py").read_text(encoding="utf-8"))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "record_perf"
    ]
    assert len(calls) == 4, f"expected the four known call sites, found {len(calls)}"
    for call in calls:
        assert any(keyword.arg == "demo" for keyword in call.keywords), (
            f"the record_perf call at app.py:{call.lineno} does not pass the demo flag"
        )


def test_the_demo_stream_labels_its_usage_line() -> None:
    """The flag has to travel with the figures, or nothing downstream can decline them."""
    source = (PROJECT_ROOT / "app.py").read_text(encoding="utf-8")
    demo_usage = re.search(r'"type": "usage", "decode_tps": 29\.5 if "27B" in model else 42\.1.*?"demo": True', source, re.DOTALL)
    assert demo_usage, "the demo usage line no longer carries `demo: True` beside its constants"


def test_the_standalone_bundle_carries_the_same_refusal() -> None:
    """`studio.py` is generated from `app.py`; a stale bundle would keep the old write path."""
    bundle = (PROJECT_ROOT / "studio.py").read_text(encoding="utf-8")
    assert "demo: bool = False" in bundle
    assert bundle.count("demo=bool(") >= 4
