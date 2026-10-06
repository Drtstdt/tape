"""D120: pumpfundata store v2 -- the vectorised conversion must reproduce
tape/sources/pumpfundata.py::_row_to_swap row for row (same filters, units,
mapping), and the resume planner must never skip or double-stage work."""

import math
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pandas as pd  # noqa: E402

from tape import pf_store as ps  # noqa: E402
from tape.sources.pumpfundata import _row_to_swap, _parse_timestamp_ms  # noqa: E402
from tape.swaps_cache import bucket_of as cache_bucket_of  # noqa: E402

T0 = 1770530400  # 2026-02-08 06:00:00 UTC, seconds


def raw_frame(ts_kind="seconds"):
    """Object-dtype frame, the shape pandas 2 read_parquet produces."""
    rows = []

    def add(**kw):
        base = dict(event_type="swap", token_mint="MintA", slot_number=100, can_be_frozen=False,
                    signature="sigA", timestamp=T0 + 5, token_creator="CreatorA",
                    virtual_token_reserve=1_000_000_000_000_000, virtual_lamports_reserve=32_000_000_000,
                    real_token_reserve=700_000_000_000_000, real_lamports_reserve=2_000_000_000,
                    action="buy", token_amount=3_000_000_000, lamports_amount=100_000_000,
                    fee_lamports=1_000_000, user_wallet="W1", token_total_supply=None,
                    is_mayhem_mode=False)
        base.update(kw)
        rows.append(base)

    add()
    add(signature="sigB", action="sell", token_amount=-2_500_000_000, lamports_amount=-90_000_000,
        user_wallet="", timestamp=T0 + 7, slot_number=101)
    add(signature="sigC", token_mint="MintB", token_creator=None, user_wallet=None, timestamp=T0 + 3599)
    add(signature="sigD", token_amount=0, timestamp=T0 + 10)                    # price -> 0.0
    add(signature="sigE", virtual_token_reserve=None, virtual_lamports_reserve=None)
    add(signature="sigF", timestamp=T0 - 6)                                     # boundary noise
    # rows _row_to_swap drops:
    add(signature="", token_mint="MintX")
    add(signature=None)
    add(token_mint="", signature="sigG")
    add(action="unknown", signature="sigH")
    add(timestamp=None, signature="sigI")
    add(timestamp=-5, signature="sigJ")
    add(slot_number=None, signature="sigK")
    add(token_amount=None, signature="sigL")
    add(lamports_amount=None, signature="sigM")
    # events
    add(event_type="create", signature="sigC1", token_mint="MintNew", token_total_supply=1_000_000_000_000_000,
        action=None, token_amount=None, lamports_amount=None, timestamp=T0 + 1)
    add(event_type="create", signature="sigC2", token_mint="MintOdd", token_total_supply=5, action=None,
        timestamp=T0 + 2)
    add(event_type="bonding_complete", signature="sigBC", token_mint="MintA", action=None, timestamp=T0 + 30)
    df = pd.DataFrame(rows, dtype=object)
    if ts_kind == "ms":
        df["timestamp"] = pd.Series([None if x is None else x * 1000 for x in df["timestamp"]], dtype=object)
    elif ts_kind == "datetime":
        # tz-AWARE: the scalar adapter calls datetime.timestamp(), which reads a NAIVE
        # datetime as LOCAL time (see test_naive_datetime_is_utc_here)
        df["timestamp"] = pd.to_datetime(
            df["timestamp"].map(lambda x: None if x is None or x <= 0 else x), unit="s", utc=True)
    return df


def same(a, b):
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


FIELDS = ["mint", "venue", "pool", "ts_ms", "slot", "sig", "side", "base_amount", "quote_amount",
          "quote_mint", "price", "wallet", "base_reserve_after", "quote_reserve_after", "source"]


class TestParityWithRowToSwap(unittest.TestCase):
    def _check(self, kind):
        df = raw_frame(kind)
        out, ev, stats = ps.convert_raw_frame(df, "2026-02-08_06", T0 * 1000)
        expected = []
        for r in df.to_dict("records"):
            if r["event_type"] != "swap":
                continue
            if kind == "datetime" and r["timestamp"] is not None and pd.isna(r["timestamp"]):
                r["timestamp"] = None                 # NaT == "no timestamp" for both paths
            s = _row_to_swap(r)
            if s is not None:
                expected.append(s)
        self.assertEqual(len(out), len(expected), kind)
        for got, exp in zip(out.to_dict("records"), expected):
            for f in FIELDS:
                g = got[f]
                if isinstance(g, float) and math.isnan(g):
                    g = float("nan")
                e = getattr(exp, f)
                if f == "wallet" and g is not None and isinstance(g, float) and math.isnan(g):
                    g = None
                if f in ("base_reserve_after", "quote_reserve_after") and e is not None and math.isnan(e):
                    self.assertTrue(g is None or math.isnan(g), (kind, f))
                    continue
                if f in ("base_reserve_after", "quote_reserve_after") and e is None:
                    self.assertTrue(g is None or math.isnan(g), (kind, f, g))
                    continue
                self.assertTrue(same(g, e), (kind, f, g, e))
        return out, ev, stats

    def test_seconds(self):
        out, ev, stats = self._check("seconds")
        self.assertEqual(stats["n_swaps"], 6)
        self.assertEqual(stats["n_swap_rows"], 15)
        self.assertEqual(stats["n_swaps_dropped"], 9)

    def test_milliseconds(self):
        self._check("ms")

    def test_datetime(self):
        self._check("datetime")

    def test_extras_and_stats(self):
        out, ev, stats = ps.convert_raw_frame(raw_frame(), "2026-02-08_06", T0 * 1000)
        a = out[out["sig"] == "sigA"].iloc[0]
        self.assertAlmostEqual(a["fee_sol"], 0.001)
        self.assertAlmostEqual(a["real_quote_reserve_after"], 2.0)
        self.assertAlmostEqual(stats["fee_ratio_p50"], 0.001 / 0.1, places=6)
        self.assertEqual(int(a["bucket"]), cache_bucket_of("MintA"))
        self.assertEqual(stats["n_outside_hour"], 1)
        self.assertAlmostEqual(stats["outside_max_s"], 6.0)
        self.assertEqual(ps.classify_file(stats), "boundary_noise")
        self.assertEqual(stats["n_create"], 2)
        self.assertEqual(stats["n_bonding_complete"], 1)
        self.assertEqual(stats["n_bad_supply"], 1)
        self.assertEqual(sorted(ev["event_type"]), ["bonding_complete", "create", "create"])
        self.assertEqual(set(out["src"]), {"2026-02-08_06"})


class TestTimestamps(unittest.TestCase):
    def test_numeric_matches_scalar(self):
        vals = [1770530405, 1770530405123, 1770530405123456, 1770530405123456789, 0, -3, None]
        got = ps.ts_to_ms(pd.Series(vals, dtype=object))
        for v, g in zip(vals, got):
            e = _parse_timestamp_ms(v)
            if e is None:
                self.assertTrue(math.isnan(g))
            else:
                self.assertEqual(int(g), e)

    def test_datetime_naive_and_tz(self):
        s = pd.Series(pd.to_datetime([T0 + 1, T0 + 2], unit="s"))
        self.assertEqual(list(ps.ts_to_ms(s).astype("int64")), [(T0 + 1) * 1000, (T0 + 2) * 1000])
        tz = s.dt.tz_localize("UTC").dt.tz_convert("Europe/Warsaw")
        self.assertEqual(list(ps.ts_to_ms(tz).astype("int64")), [(T0 + 1) * 1000, (T0 + 2) * 1000])
        for v, g in zip(tz, ps.ts_to_ms(tz)):
            self.assertEqual(int(g), _parse_timestamp_ms(v))

    def test_nan_slot_or_timestamp_dropped_not_crashing(self):
        df = raw_frame()
        df.loc[0, "slot_number"] = float("nan")
        df.loc[1, "timestamp"] = float("nan")
        out, _, stats = ps.convert_raw_frame(df, "x", T0 * 1000)
        self.assertEqual(stats["n_swaps"], 4)
        self.assertNotIn("sigA", set(out["sig"]))
        self.assertNotIn("sigB", set(out["sig"]))

    def test_classify(self):
        self.assertEqual(ps.classify_file({"n_outside_hour": 0, "outside_max_s": 0}), "ok")
        self.assertEqual(ps.classify_file({"n_outside_hour": 1226, "outside_max_s": 72}), "boundary_noise")
        self.assertEqual(ps.classify_file({"n_outside_hour": 5, "outside_max_s": 3601}), "ts_suspect")


class TestPlanning(unittest.TestCase):
    def setUp(self):
        self.raw = {"date=2026-02-08/hour=06.parquet": (10, 1), "date=2026-02-08/hour=08.parquet": (20, 2),
                    "date=2026-03-01/hour=00.parquet": (30, 3)}

    def rec(self, rel, status="ok"):
        s, m = self.raw[rel]
        return {"file": rel, "size": s, "mtime_ns": m, "status": status, "n_swaps": 1}

    def test_fresh(self):
        stage, months = ps.plan_work(self.raw, {}, {}, lambda r: False)
        self.assertEqual(stage, sorted(self.raw))
        self.assertEqual(months, ["2026-02", "2026-03"])

    def test_resume_after_stage1(self):
        man = {r: self.rec(r) for r in self.raw}
        stage, months = ps.plan_work(self.raw, man, {}, lambda r: True)
        self.assertEqual(stage, [])
        self.assertEqual(months, ["2026-02", "2026-03"])

    def test_staging_lost_before_compaction_restages(self):
        man = {r: self.rec(r) for r in self.raw}
        stage, _ = ps.plan_work(self.raw, man, {}, lambda r: False)
        self.assertEqual(stage, sorted(self.raw))

    def test_all_done(self):
        man = {r: self.rec(r) for r in self.raw}
        comp = {"2026-02": {"fingerprint": ps.month_fingerprint({k: v for k, v in self.raw.items() if "02-08" in k})},
                "2026-03": {"fingerprint": ps.month_fingerprint({k: v for k, v in self.raw.items() if "03-01" in k})}}
        stage, months = ps.plan_work(self.raw, man, comp, lambda r: False)
        self.assertEqual((stage, months), ([], []))

    def test_new_file_in_compacted_month_restages_whole_month(self):
        man = {r: self.rec(r) for r in self.raw}
        comp = {"2026-02": {"fingerprint": ps.month_fingerprint({k: v for k, v in self.raw.items() if "02-08" in k})},
                "2026-03": {"fingerprint": ps.month_fingerprint({k: v for k, v in self.raw.items() if "03-01" in k})}}
        raw2 = dict(self.raw)
        raw2["date=2026-02-09/hour=00.parquet"] = (5, 9)
        stage, months = ps.plan_work(raw2, man, comp, lambda r: False)
        self.assertEqual(months, ["2026-02"])
        self.assertEqual(stage, ["date=2026-02-08/hour=06.parquet", "date=2026-02-08/hour=08.parquet",
                                 "date=2026-02-09/hour=00.parquet"])

    def test_changed_file_restaged_and_skipped_not_restaged(self):
        man = {r: self.rec(r) for r in self.raw}
        man["date=2026-03-01/hour=00.parquet"]["status"] = "ts_suspect"
        raw2 = dict(self.raw)
        raw2["date=2026-02-08/hour=06.parquet"] = (11, 7)
        stage, _ = ps.plan_work(raw2, man, {}, lambda r: True)
        self.assertEqual(stage, ["date=2026-02-08/hour=06.parquet"])

    def test_manifest_torn_line_and_last_wins(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.jsonl"
            p.write_text('{"file": "a", "status": "read_error"}\n{"file": "a", "status": "ok"}\n{"file": "b", "sta',
                         encoding="utf-8")
            m = ps.read_manifest(p)
            self.assertEqual(m, {"a": {"file": "a", "status": "ok"}})

    def test_building_dirs_never_read(self):
        with tempfile.TemporaryDirectory() as d:
            for m in ("month=2026-02", "month=2026-03.building"):
                (Path(d) / "months" / m).mkdir(parents=True)
                (Path(d) / "months" / m / "bucket=05.parquet").write_text("x")
            self.assertEqual([p.parent.name for p in ps.bucket_files(d, 5)], ["month=2026-02"])

    def test_paths(self):
        self.assertEqual(ps.parse_raw_relpath("date=2026-05-22\\hour=07.parquet"), ("2026-05-22", 7))
        self.assertIsNone(ps.parse_raw_relpath("date=2026-05-22/hour=07.parquet.ingested"))
        self.assertEqual(ps.hour_window_ms("2026-02-08", 6), (T0 * 1000, T0 * 1000 + 3_600_000))
        for m in ("MintA", "7xKXtg2CW87d97TXJSDpbD5jBkheTqA83TZRuJosgAsU"):
            self.assertEqual(ps.bucket_of(m), cache_bucket_of(m))


try:
    import duckdb  # noqa: F401
    HAVE_DUCKDB = True
except ImportError:  # sandbox without duckdb; runs on the research machine
    HAVE_DUCKDB = False


@unittest.skipUnless(HAVE_DUCKDB, "duckdb not installed")
class TestDedupD133(unittest.TestCase):
    """D133: two distinct swaps of one tx with equal token amounts must both
    survive; a true duplicate (same row in two hourly files) collapses; the
    surviving copy and the output order are deterministic."""

    def _run(self, rows):
        con = ps.open_connection()
        con.register("j", pd.DataFrame(rows))
        return con.execute(f"SELECT * EXCLUDE (rn) FROM (SELECT *, {ps._DEDUP_WINDOW} AS rn FROM j) "
                           f"WHERE rn = 1 ORDER BY {ps._FETCH_ORDER}").df()

    @staticmethod
    def _row(**kw):
        r = dict(mint="M", ts_ms=1000, slot=7, sig="S", side="buy", base_amount=1.5e6,
                 real_quote_reserve_after=1.0, source="pumpfundata", src="date=2026-02-08/hour=06.parquet",
                 fee_sol=0.01)
        r.update(kw)
        return r

    def test_same_amount_distinct_swaps_kept(self):
        out = self._run([self._row(real_quote_reserve_after=1.0), self._row(real_quote_reserve_after=1.05)])
        self.assertEqual(len(out), 2)
        self.assertEqual(out["real_quote_reserve_after"].tolist(), [1.0, 1.05])

    def test_true_duplicate_collapses_deterministically(self):
        a = self._row(src="date=2026-02-08/hour=07.parquet", ts_ms=1500)
        b = self._row(src="date=2026-02-08/hour=06.parquet", ts_ms=1000)
        for rows in ([a, b], [b, a]):
            out = self._run(rows)
            self.assertEqual(len(out), 1)
            self.assertEqual(out["src"].iloc[0], "date=2026-02-08/hour=06.parquet")   # earliest ts wins
            self.assertEqual(int(out["ts_ms"].iloc[0]), 1000)


if __name__ == "__main__":
    unittest.main()
