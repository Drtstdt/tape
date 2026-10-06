"""D144: pumplive -- a live PAPER bot for pump.fun bonding-curve tokens.

  events.py   decode pump TradeEvent from transaction logs ("Program data: ...")
  meta.py     token metadata (uri JSON) and social-link checks
  rails.py    rug/background rails at create time and at decision time
  state.py    per-token live state (exact curve state from every event)
  paper.py    paper positions for many configs at once (anchored model, slot latency)
  learner.py  rolling evaluation of every config; champion = best LOWER bound > 0, else ABSTAIN
  feeds.py    PumpPortal (new tokens, migrations; free) + Helius logsSubscribe (trades)
  recorder.py everything to hourly jsonl.gz (new live data for later research)
Paper only: there is no code path that signs or sends a transaction.
"""
