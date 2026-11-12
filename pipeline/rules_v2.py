"""The screener's v2 rules (approved by Cade 2026-11-11; evidence in docs/v2-proposal.md).

Same scoring as v1 (rules_v1.py) with three changes:
  * strength gate 60 -> 80
  * no 2-per-sector cap on finalists
  * no price stop in the paper engine (the 2.5 x ATR distance is kept only for sizing)
And the paper book holds the swing trades as a 30% sleeve, 70% in VOO (engines/engine.py).
"""
import rules_v1 as _v1
from rules_v1 import skip1, pct_rank, GROUP_OF_SECTOR   # noqa: F401  (re-exported)

RULES_VERSION = "v2"
RS_GATE = 80
EXT_CUT = _v1.EXT_CUT          # 12% above the 50-day -> wait for a pullback (unchanged)
STOP_ATR = _v1.STOP_ATR        # 2.5 x ATR: sizing / risk reference only in v2
TOP_N = _v1.TOP_N              # 8 finalists (unchanged)
PER_GROUP = 99                 # no sector cap
SLEEVE = 0.30


def score(df, spy, rs_gate=RS_GATE):
    return _v1.score(df, spy, rs_gate=rs_gate)


def pick_finalists(df, n=TOP_N, per_group=PER_GROUP):
    return _v1.pick_finalists(df, n=n, per_group=per_group)
