from __future__ import annotations

import os
import time
from pathlib import Path
from typing import List, Optional, Tuple

import duckdb

from .store import Store


class StoreV2(Store):
    """
    Read-only performance wrapper around Store.

    IMPORTANT:
    - Never writes to data/swaps/*.parquet
    - Never opens source Parquet files in write mode
    - Does not create/modify the main Store database
    - chronological_universe() avoids one gigantic GROUP BY over the
      entire Parquet corpus.
    """

    def __init__(
        self,
        data_dir: str | Path,
        *,
        read_only: bool = True,
        temp_dir: str | Path | None = None,
        memory_limit: str = "4GB",
        max_temp_size: str = "1GB",
    ) -> None:
        if not read_only:
            raise ValueError(
                "StoreV2 is intentionally read-only for the audit. "
                "Do not use read_only=False."
            )

        self.data_dir = Path(data_dir)
        self.swaps_dir = self.data_dir / "swaps"

        if not self.swaps_dir.exists():
            raise FileNotFoundError(
                f"Missing swaps directory: {self.swaps_dir}"
            )

        # Keep the original Store interface for iter_swaps(), sql(), etc.
        super().__init__(self.data_dir, read_only=True)

        # Separate in-memory DuckDB connection ONLY for the V2 universe scan.
        # It never modifies the original Store DB.
        self._v2_con = duckdb.connect(":memory:")

        # Put any possible DuckDB spill somewhere explicit and bounded.
        if temp_dir is None:
            temp_dir = self.data_dir / ".audit_tmp_v2"

        self.temp_dir = Path(temp_dir)
        self.temp_dir.mkdir(parents=True, exist_ok=True)

        self._v2_con.execute(
            "SET memory_limit = ?",
            [memory_limit],
        )
        self._v2_con.execute(
            "SET max_temp_directory_size = ?",
            [max_temp_size],
        )
        self._v2_con.execute(
            "SET temp_directory = ?",
            [str(self.temp_dir.resolve())],
        )

        # Avoid DuckDB spawning lots of threads for tiny per-file queries.
        self._v2_con.execute("SET threads = 1")

    @staticmethod
    def _sql_quote(value: str) -> str:
        """Quote a SQL string literal safely."""
        return "'" + value.replace("'", "''") + "'"

    def _parquet_files(self) -> List[Path]:
        files = sorted(
            p for p in self.swaps_dir.rglob("*.parquet")
            if p.is_file()
        )

        if not files:
            raise FileNotFoundError(
                f"No parquet files found under {self.swaps_dir}"
            )

        return files

    def chronological_universe(
        self,
        since: Optional[str] = None,
        until: Optional[str] = None,
        *,
        progress_every_s: float = 5.0,
    ) -> List[Tuple[str, int, int]]:
        """
        Exact same logical output as the old:

            SELECT mint, min(ts_ms), count(*)
            FROM swaps
            WHERE ...
            GROUP BY mint
            ORDER BY first_ts_ms, mint

        but computed file-by-file.

        Returns:
            [(mint, first_ts_ms, n_swaps), ...]
        """

        files = self._parquet_files()

        print(
            f"[StoreV2] universe scan: {len(files)} parquet file(s)",
            flush=True,
        )
        print(
            f"[StoreV2] source: {self.swaps_dir}",
            flush=True,
        )
        print(
            f"[StoreV2] temp:   {self.temp_dir}",
            flush=True,
        )

        # mint -> [first_ts_ms, n_swaps]
        merged: dict[str, list[int]] = {}

        t0 = time.time()
        last_print = t0

        for i, path in enumerate(files, start=1):
            path_sql = self._sql_quote(str(path.resolve()))

            where_parts: list[str] = []

            if since:
                where_parts.append(
                    f"dt >= {self._sql_quote(since)}"
                )

            if until:
                where_parts.append(
                    f"dt <= {self._sql_quote(until)}"
                )

            where_sql = ""
            if where_parts:
                where_sql = "WHERE " + " AND ".join(where_parts)

            # IMPORTANT:
            # Only ONE physical parquet file is aggregated at a time.
            #
            # This is the key difference from:
            #
            #   FROM swaps GROUP BY mint
            #
            # over the entire corpus.
            query = f"""
                SELECT
                    mint,
                    min(ts_ms) AS first_ts_ms,
                    count(*) AS n
                FROM read_parquet(
                    {path_sql},
                    hive_partitioning = 1,
                    union_by_name = true
                )
                {where_sql}
                GROUP BY mint
            """

            rows = self._v2_con.execute(query).fetchall()

            for mint, first_ts_ms, n in rows:
                if mint is None:
                    continue

                mint = str(mint)
                first_ts_ms = int(first_ts_ms)
                n = int(n)

                current = merged.get(mint)

                if current is None:
                    merged[mint] = [first_ts_ms, n]
                else:
                    if first_ts_ms < current[0]:
                        current[0] = first_ts_ms
                    current[1] += n

            now = time.time()

            if (
                now - last_print >= progress_every_s
                or i == len(files)
            ):
                elapsed = now - t0
                rate = i / elapsed if elapsed > 0 else 0.0
                remaining = len(files) - i
                eta = remaining / rate if rate > 0 else float("nan")

                print(
                    f"[StoreV2] {i}/{len(files)} files "
                    f"({i / len(files):.1%}) "
                    f"mints={len(merged):,} "
                    f"elapsed={elapsed:.0f}s "
                    f"ETA~{eta:.0f}s",
                    flush=True,
                )

                last_print = now

        # Same final ordering semantics as the original SQL query.
        result = [
            (mint, vals[0], vals[1])
            for mint, vals in merged.items()
        ]

        result.sort(key=lambda x: (x[1], x[0]))

        elapsed = time.time() - t0

        print(
            f"[StoreV2] universe done in {elapsed:.1f}s "
            f"-- {len(result):,} mint(s)",
            flush=True,
        )

        return result

    def close(self) -> None:
        try:
            self._v2_con.close()
        finally:
            # Preserve Store's cleanup if it has one.
            close_fn = getattr(super(), "close", None)
            if callable(close_fn):
                close_fn()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False