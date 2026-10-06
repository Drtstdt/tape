"""Parquet + DuckDB. No server, no ORM, no JSONL.

v3 writes one JSONL file per token per day -- fine at 20 MB/day, useless once
the universe widens to every Solana venue, and you will re-read the whole
corpus thousands of times while iterating on features. DuckDB over partitioned
Parquet gives predicate pushdown and columnar reads over hundreds of millions
of rows on a laptop. The difference is minutes versus hours per iteration,
multiplied by every iteration you will ever run.

Layout:
    data/swaps/venue=<v>/dt=<YYYY-MM-DD>/part-*.parquet
    data/bars/kind=<k>/dt=<YYYY-MM-DD>/part-*.parquet
    data/pools/dt=<YYYY-MM-DD>/part-*.parquet
    data/meta/tokens.parquet
    data/graph/creator_stats.parquet, wallet_stats.parquet
    data/market/regime.parquet

Swaps are append-only. Graphs are REBUILT, never appended, because a reputation
table that has been incrementally patched cannot be recomputed as-of any past
date -- and as-of-time correctness is the whole value of the graph.

Two things below exist only because they were wrong once:

  - `venue` and `kind` are dropped from the written Parquet FILE, same as `dt`
    already was. They live in the hive path only. Keeping a partition column
    inside the file as well is a duplicate-column conflict waiting to happen
    the moment `hive_partitioning=1` reads it back -- and the failure would
    have shown up mid-backfill, which is a much worse place to find it than
    here.
  - an empty store (no Parquet files yet -- true on a fresh clone before any
    ingestion has run) returns EMPTY TYPED RESULTS, not an exception.
    `read_parquet('.../**/*.parquet')` over zero matching files raises an
    IOException in DuckDB; callers up the stack (Store.mints(), and
    scripts/information_audit.py's `status=collecting_data` branch) are
    written to handle "no data yet" as a normal, expected state -- they were
    never given the chance to, because the view registration itself blew up
    first. `_empty_view_sql` gives them a same-shaped, zero-row relation
    instead, so "no data collected yet" degrades to an empty result exactly
    where the code already expects it to.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import os
import uuid
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

from .schema import Bar, CanonicalSwap


def _dt_str(ts_ms: int) -> str:
    return _dt.datetime.utcfromtimestamp(ts_ms / 1000).strftime("%Y-%m-%d")


def _has_parquet(directory: Path) -> bool:
    return directory.exists() and any(directory.rglob("*.parquet"))


# schema.py has `from __future__ import annotations`, so dataclass field
# annotations are PEP 563 strings ("Optional[str]"), not type objects -- this
# maps the small, closed set of annotations actually used in CanonicalSwap and
# Bar. It is deliberately string-based rather than typing.get_origin/get_args
# gymnastics, because those need real type objects to introspect.
_DUCKDB_TYPE = {"str": "VARCHAR", "int": "BIGINT", "float": "DOUBLE", "bool": "BOOLEAN"}


def _duckdb_type(annotation: str) -> str:
    base = annotation.replace("Optional[", "").replace("]", "").strip()
    return _DUCKDB_TYPE.get(base, "VARCHAR")


def _empty_view_sql(view_name: str, dataclass_type, extra_cols: Optional[dict] = None) -> str:
    """A zero-row relation with the RIGHT column names and types.

    Lets every downstream query (GROUP BY, aggregates, column selection) run
    exactly as it would over real data -- it just returns nothing, which is
    what "no data yet" should look like to a caller, not a crash.
    """
    cols = [f"CAST(NULL AS {_duckdb_type(f.type)}) AS {f.name}"
            for f in dataclasses.fields(dataclass_type)]
    for name, duck_type in (extra_cols or {}).items():
        cols.append(f"CAST(NULL AS {duck_type}) AS {name}")
    # D114 (docs/DECISIONS.md): TEMP, same reasoning as _register_views()'s
    # real views -- session-local, works under a read_only connection, never
    # relied on to persist between runs.
    return f"CREATE OR REPLACE TEMP VIEW {view_name} AS SELECT {', '.join(cols)} WHERE FALSE"


class Store:
    def __init__(self, root: str | Path = "data", read_only: bool = False) -> None:
        """`read_only` (D114, docs/DECISIONS.md): opens the underlying
        DuckDB connection with `read_only=True`, so MULTIPLE processes can
        each hold their own connection to the same `tape.duckdb` file at
        once -- DuckDB's concurrency model allows many concurrent read-only
        connections, but not a read-only connection alongside (or a second)
        read-write one. This exists specifically so read-only, measurement-
        only scripts (`scripts/information_audit.py --workers N`) can
        parallelize across OS processes without fighting over the file lock.
        Default is unchanged (`False`, read-write) -- every existing
        single-process caller (ingest/backfill scripts, anything that calls
        `write_swaps`/`write_bars`) is unaffected. Opening read-only against
        a `tape.duckdb` that doesn't exist yet will raise (DuckDB can't
        create a new file in read-only mode) -- acceptable here since a
        read-only workload only makes sense once something has already
        written data."""
        self.root = Path(root)
        (self.root / "swaps").mkdir(parents=True, exist_ok=True)
        (self.root / "bars").mkdir(parents=True, exist_ok=True)
        (self.root / "pools").mkdir(parents=True, exist_ok=True)
        (self.root / "meta").mkdir(parents=True, exist_ok=True)
        (self.root / "graph").mkdir(parents=True, exist_ok=True)
        (self.root / "market").mkdir(parents=True, exist_ok=True)
        self._con = None
        self._read_only = read_only

    # -- writing --------------------------------------------------------

    def write_swaps(self, swaps: Sequence[CanonicalSwap]) -> List[Path]:
        """Partition by venue and UTC date, one part file per call.

        `venue` and `dt` are dropped from the file itself -- they live in the
        hive path only, so a read with hive_partitioning=1 does not see the
        same value twice under two different provenances.

        Dedup on `dedup_key` happens on READ, not write: running two sources
        over the same pool simultaneously is the point (corroboration), so
        duplicates are expected and ingestion must stay idempotent and cheap.
        """
        import pandas as pd

        if not swaps:
            return []
        rows = [dataclasses.asdict(s) for s in swaps]
        df = pd.DataFrame(rows)
        df["dt"] = df["ts_ms"].map(_dt_str)
        written = []
        for (venue, dt), chunk in df.groupby(["venue", "dt"], sort=False):
            d = self.root / "swaps" / f"venue={venue}" / f"dt={dt}"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"part-{uuid.uuid4().hex[:12]}.parquet"
            chunk.drop(columns=["dt", "venue"]).to_parquet(p, index=False, compression="zstd")
            written.append(p)
        return written

    def write_bars(self, bars: Sequence[Bar]) -> List[Path]:
        """Same partition-column treatment as write_swaps, on `kind` / `dt`."""
        import pandas as pd

        if not bars:
            return []
        df = pd.DataFrame([dataclasses.asdict(b) for b in bars])
        df["dt"] = df["close_ts_ms"].map(_dt_str)
        written = []
        for (kind, dt), chunk in df.groupby(["kind", "dt"], sort=False):
            d = self.root / "bars" / f"kind={kind}" / f"dt={dt}"
            d.mkdir(parents=True, exist_ok=True)
            p = d / f"part-{uuid.uuid4().hex[:12]}.parquet"
            chunk.drop(columns=["dt", "kind"]).to_parquet(p, index=False, compression="zstd")
            written.append(p)
        return written

    # -- reading --------------------------------------------------------

    @property
    def con(self):
        if self._con is None:
            import duckdb
            self._con = duckdb.connect(str(self.root.parent / "tape.duckdb"),
                                       read_only=self._read_only)
            # D80 (docs/DECISIONS.md): a real OutOfMemoryException hit live on
            # scripts/token_dna_report.py's chronological_universe() query,
            # after many incremental discover/backfill runs had accumulated a
            # large Parquet corpus. DuckDB's default memory target is a
            # fraction of detected system RAM, and "Allocation failure" means
            # it hit that ceiling without a place to spill -- an explicit,
            # conservative memory_limit plus a real temp_directory next to
            # the DB file gives it somewhere to spill to disk instead of
            # failing outright. This does not change what any query returns,
            # only whether a big one can complete on a loaded machine.
            # D115 (docs/DECISIONS.md): per-PROCESS subdirectory, not one
            # shared `.duckdb_tmp` for every connection. Found live: a single
            # query (chronological_universe()'s GROUP BY scan, now over a
            # much larger data/swaps/ corpus than the 2GB memory_limit below
            # comfortably fits) had already spilled ~3.76GB into the old
            # shared path. That's an expected, self-cleaning cost on its own
            # -- but D114 just made it possible for multiple OS processes
            # (`--workers N`'s worker pool, each with its own Store/
            # connection) to spill AT THE SAME TIME. DuckDB's internal spill
            # block naming (`duckdb_temp_storage_<class>-<n>.tmp`) is scoped
            # to ONE connection's own allocator, not guaranteed collision-
            # safe against a DIFFERENT OS process writing into the exact same
            # folder -- unverified in this sandbox (no duckdb here), but not
            # a risk worth taking now that concurrent processes sharing this
            # path is a real, reachable case, not a hypothetical one.
            # Isolating by PID removes the question entirely.
            # D116: TAPE_DUCKDB_TMP / TAPE_DUCKDB_MEMORY_LIMIT override the
            # defaults (spill root next to the data dir; 2GB) so spill can
            # live on a roomier drive without code changes or symlinks.
            _tmp_root = os.environ.get("TAPE_DUCKDB_TMP")
            temp_dir = (
                (Path(_tmp_root) if _tmp_root else self.root.parent / ".duckdb_tmp")
                / f"pid{os.getpid()}"
            )
            temp_dir.mkdir(parents=True, exist_ok=True)
            try:
                self._con.execute(f"PRAGMA temp_directory='{temp_dir.as_posix()}'")
                self._con.execute(
                    "PRAGMA memory_limit='%s'"
                    % os.environ.get("TAPE_DUCKDB_MEMORY_LIMIT", "2GB"))
            except Exception as e:  # noqa: BLE001 -- D114: these are session-
                # level settings, not writes to the database file, so they
                # should work fine under read_only=True per DuckDB's own
                # docs -- but this is unverified in this sandbox (no duckdb
                # installed here, D93/D104's standing limitation), so this
                # degrades to "no spill-to-disk safety net" rather than a
                # hard crash if some DuckDB version disagrees under
                # read_only. Queries still run; a huge one just has less
                # protection against an allocation failure (D80).
                print(f"  (could not set memory_limit/temp_directory "
                      f"pragmas -- {e}; continuing without them)")
            self._register_views()
        return self._con

    def _register_views(self) -> None:
        """D114 (docs/DECISIONS.md): uses `CREATE OR REPLACE TEMP VIEW`, not
        a persistent `CREATE OR REPLACE VIEW` -- these views are already
        re-created from scratch every single time ANY Store opens a fresh
        connection (nothing relies on them surviving between runs, and
        `CREATE OR REPLACE` already meant any stale definition was
        overwritten regardless), so making them session-local is a pure
        behavior-preserving change for every existing single-process caller.
        It is also what makes `read_only=True` connections usable at all:
        a non-temporary `CREATE VIEW` writes a catalog entry into the actual
        `tape.duckdb` file, which a read-only connection cannot do -- a
        TEMP view lives only in the session, touches no on-disk catalog, and
        is exactly what lets multiple read-only worker processes
        (`scripts/information_audit.py --workers N`) each register their own
        views concurrently without needing write access at all."""
        swaps_dir = self.root / "swaps"
        bars_dir = self.root / "bars"

        if _has_parquet(swaps_dir):
            pattern = str(swaps_dir / "**" / "*.parquet")
            # hive_partitioning exposes venue / dt as real columns, so a query
            # for one day reads one directory rather than the corpus.
            self._con.execute(f"""
                CREATE OR REPLACE TEMP VIEW swaps_raw AS
                SELECT * FROM read_parquet('{pattern}', hive_partitioning=1, union_by_name=1)
            """)
        else:
            # Fresh store, nothing ingested yet. This is the NORMAL starting
            # state, not an error -- callers (Store.mints(), the information
            # audit's `status=collecting_data` branch) are written to handle
            # it, and now get the chance to.
            self._con.execute(_empty_view_sql("swaps_raw", CanonicalSwap, {"dt": "VARCHAR"}))

        # Dedup on read. `source` ordering is deterministic so two runs of the
        # same query return the same rows.
        self._con.execute("""
            CREATE OR REPLACE TEMP VIEW swaps AS
            SELECT * EXCLUDE (rn) FROM (
                SELECT *, row_number() OVER (
                    PARTITION BY sig, mint, side, round(base_amount, 12)
                    ORDER BY source
                ) AS rn
                FROM swaps_raw
            ) WHERE rn = 1
        """)

        if _has_parquet(bars_dir):
            pattern = str(bars_dir / "**" / "*.parquet")
            self._con.execute(f"""
                CREATE OR REPLACE TEMP VIEW bars AS
                SELECT * FROM read_parquet('{pattern}', hive_partitioning=1, union_by_name=1)
            """)
        else:
            self._con.execute(_empty_view_sql("bars", Bar, {"dt": "VARCHAR"}))

    def sql(self, query: str):
        return self.con.execute(query).df()

    def iter_swaps(self, mint: str) -> Iterator[CanonicalSwap]:
        """Replay one token's swaps in time order. This is what feeds the
        replay engine, and it must return exactly what the live socket did."""
        df = self.sql(f"SELECT * FROM swaps WHERE mint = '{mint}' ORDER BY ts_ms, slot, sig")
        cols = {f.name for f in dataclasses.fields(CanonicalSwap)}
        for row in df.to_dict("records"):
            yield CanonicalSwap(**{k: v for k, v in row.items() if k in cols})

    def mints(self, since: Optional[str] = None, until: Optional[str] = None,
              min_swaps: int = 1) -> List[str]:
        """Discover the universe.

        `min_swaps` is a CORPUS filter, not a selection rule. Never select the
        training universe on anything only knowable later ("reached $X
        liquidity", "top movers") -- that trains on a universe you cannot
        identify at entry time, and the backtest comes back beautiful while the
        live bot loses money. Select on an event with a timestamp: created,
        migrated, first seen in a block range.

        Returns [] on an empty store -- callers treat that as
        `status=collecting_data`, not as a fault.
        """
        where = []
        if since:
            where.append(f"dt >= '{since}'")
        if until:
            where.append(f"dt <= '{until}'")
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        df = self.sql(f"""
            SELECT mint, count(*) AS n FROM swaps {clause}
            GROUP BY mint HAVING n >= {min_swaps} ORDER BY mint
        """)
        return df["mint"].tolist()

    def coverage(self) -> "object":
        return self.sql("""
            SELECT venue, dt, count(*) AS swaps, count(DISTINCT mint) AS mints,
                   sum(CASE WHEN wallet IS NULL THEN 1 ELSE 0 END) AS unattributed
            FROM swaps GROUP BY venue, dt ORDER BY dt DESC, venue
        """)
