# Working notes

## Responses

Keep replies short and plain. Usage limits are a real constraint here.

- Lead with the answer or the result. No preamble, no recap of what was asked.
- Report findings as a few lines, not a structured report. Skip tables,
  headers and bold unless they genuinely carry the content.
- Don't restate what a command does, what was verified, or what a commit
  message already says.
- Flag open questions in a sentence. Don't enumerate options.
- Long form only when asked for it, or for a doc written to a file.

## Orientation (so a new session need not re-read the source)

SOL futures bot, deployed on VPS at `/opt/sol-futures-trading-bot`, branch
`claude/futures-trading-system-infra-ktlidi`. Armed for paper at 1 contract.

**ORB strategy verdict.** −$81k/yr at 40 contracts, 25% win rate vs the
document's claimed 57.8%. Raw edge ≈ zero; costs dominate.

**Cost floor.** Round trip = 2×$3.41 commission + 2×$1.25 slippage = $9.32
per contract = **$0.373/SOL**. The document's breakeven lock is $0.05 —
below the floor, so every breakeven exit loses by arithmetic (+$50 gross,
−$322.80 net, 58/yr ≈ −$18.7k).

**Source-document contradictions:**
- NY open — RESOLVED. Document said both 14:30 UTC and 9:30 ET. Now anchored
  to 9:30 America/New_York (tracks DST: 13:30 UTC summer, 14:30 winter). Was
  14:30 UTC = 10:30 ET, an hour late all summer. Fixed-UTC still reachable via
  `--sessions`. Re-running any backtest now tests 9:30 ET, so the −$81k
  baseline (computed at 10:30 ET) will move.
- CME listed SOL futures in 2025, so the document's 5-year table cannot be
  MSL — likely spot, therefore likely no commission. Would explain the gap.

See `docs/ORB_DOCUMENT_QUESTIONS.md`.

**Verdict detail (9:30 ET re-run, 2026-08-20).** Same −$81k at 40 ct. Raw
edge +$4.8k/yr ≈ zero; costs $86k. Gross win rate 58.9% ≈ the document's
57.8% — its table was almost certainly counted before costs. Clock was never
the problem.

**Replacement candidates** (built 2026-08-20): `sol-trend` (daily Donchian
20/10 + ATR stops) and a big-range ORB re-derived against the cost floor.
`scripts/strategy_compare.sh` runs baseline + both candidates ± costs in one
pass on the VPS.

**Comparison verdict (2026-08-21, correct 9:30 ET clock, 40 ct, 1yr spot):**
- ORB verbatim: net −$92,984 (gross −$530, costs $92,454, n=248). Dead; raw
  edge zero at either clock. Stops −$94.6k/97 trades, breakeven bleed −$22.4k.
- Big-range ORB (A): 1 trade/yr — $3 range filter too tight, no verdict.
- Trend (B): net −$11,354 (gross −$6,240, n=12). Costs work as designed
  ($373/trade ≈ 4% of move) but entries lost; n too small to judge.

**Open items:** import 2021→2025 SOL history and re-run `strategy_compare.sh`
(n≈60 settles candidate B); ask the document's author the five questions
(only Q2 resting-stop fills could still rescue its table); roll MSLQ6 before
Aug 28 (no auto-rollover — `ibkr-checkout --contract-month 202609`, update
DEFAULT_CONTRACT_MONTH, restart); decide whether to stay armed meanwhile.

**Note:** `backtest` needs `bars` + `contract_metadata` from the VPS
database. It cannot run in an ephemeral Claude container — don't try.
