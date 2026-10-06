"""bot -- the trading layer built on top of the research pipeline.

Where the rest of `tape/` measures, this package DECIDES. It reuses the
verified machinery (canonical swaps, dollar bars, TokenState, labels, costs,
CV, the model artifact format, the Bitquery adapter) and adds exactly the
pieces that make a bot:

  spec.py       every threshold in one place (config/bot.yaml), with status
  strategy.py   decide_entry(): rails -> accumulation gate -> model ->
                credibility -> Kelly sizing. Pure: no I/O, no clock.
  exits.py      the exit state machine, porting v3's hard-won invariants.
  engine.py     the ONE loop both backtest and live drive. Abstentions are
                first-class ledger rows, same doctrine as tape/policy.py.
  executors.py  PaperExecutor (default) and a triple-gated LiveExecutor.
  autocorrect.py  drift monitor (PSI), rolling recalibration, bounded
                auto-tuning, daily-loss / drawdown guards, kill switch.
  train.py      parquet -> bars -> features -> labels -> calibrated +
                conformal LightGBM artifact, one per band.
  backtest.py   replays the store through the engine; reports honestly.
  live.py       Bitquery discovery + polling through the same engine.
  report.py     reads the ledgers; prints the numbers that matter.

Safety doctrine carried over verbatim from tape/__init__.py: PAPER TRADING
ONLY by default. The LiveExecutor refuses to submit a transaction unless
three independent gates are passed explicitly (--live, a keypair path, and
an acknowledgement flag), and even then every trade is logged before it is
sent. No code path in this package can sign anything unless the operator
supplies a keypair AND asks for it three times.

The strategy is described in docs/BOT.md. Parameters marked
`status: unvalidated` there are MY initial values -- they are starting
points to be fitted or frozen by the gates in docs/PLAN.md, never silently
acquiring authority by being old.
"""

from .spec import (  # noqa: F401
    AutoCorrectSpec,
    BandSpec,
    BotSpec,
    EntrySpec,
    ExitSpec,
    load_spec,
)
from .strategy import decide_entry  # noqa: F401
from .exits import ExitAction, Position, step_position  # noqa: F401

__version__ = "0.1.0"
