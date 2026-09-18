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

**Candidate D — operator spec, MEASURED 2026-09-18.** NY only, 1 entry/day,
plain $1 stop / $2 target, no breakeven, no trail. Added `breakeven_enabled` /
`trail_enabled` (zero is not a legal distance, so "off" needed a switch) and a
`HH:MM@Zone` suffix on `--sessions` so running NY alone doesn't fall back to a
fixed-UTC clock an hour early each winter.

- d0 zero-cost: 105 trades, **31.4% win, gross −$2,160**. A 2:1 bracket needs
  33.3% — short by 2 wins in 105. t = −0.43: no edge, and none refuted either.
- d-bracket real costs: net −$44,054 (gross −$4,910, costs $39,144, n=105).
  At 1 ct: −$1,101/yr. Halves the ORB's bleed but is still a loss.
- d1 both sessions: −$45,233, n=126. NY-only was the better call.
- **The breakeven fix worked as designed**: gross→net win rate 31.4%→29.5%
  (2 trades flipped) vs the ORB's 49.8%→25.0% (62 flipped).

Verdict: exit geometry was never the binding problem. The ORB entry is a coin
flip and stays one. Do NOT retune stop/target against this year — 31.4% vs
33.3% is inside the noise, and fitting it is exactly the trap in
`STRATEGY_ANALYSIS.md` §7.3.

**TARGET SWEEP — the entry is a random walk (2026-09-18, `scripts/target_sweep.sh`).**
Same 105 NY signals, $1 stop, targets $1.50→$6.00, hit rate measured at zero
cost. A driftless random walk hits +T before −S with probability S/(T+S). The
measured rates track that curve across all seven targets:

```
target  1.50   2.00   2.50   3.00   4.00   5.00   6.00
actual  39.0   31.4   26.7   25.7   23.8   19.0   14.3
coinflip40.0   33.3   28.6   25.0   20.0   16.7   14.3
edge    -1.0   -1.9   -1.9   +0.7   +3.8   +2.3   +0.0   mean +0.29 pts (1 SE 4.2)
```

margin = edge − cost/(T+S), exact on every row. **No target clears.** The ORB
entry carries no directional information at ANY geometry — this closes the
whole family, not one setting. Don't re-run variants of it.

Design constraint for future entries: the cost term is smallest at a wide
target ($4–6 → 8–11 pts, vs 19–23 pts at $1.50–2.00). Any new signal needs
roughly 10+ pts of genuine edge, and is cheapest to harvest on big moves.

**Replay costs understate reality by 40%.** The fill model uses 1 tick of
slippage ($9.32/contract round trip); the measured MSL spread is 5 ticks
(~$13.08). Every net number in the comparison is optimistic by that factor.

**Hold benchmark is −$96,278 this year** — SOL fell, so "beats buy-and-hold"
is a near-worthless bar in this window. Read the zero-cost column instead.

**Live vs replay.** `app/main.py:188` passes only `position_contracts` to the
strategy. Every other rule is replay-only. Arming any of this needs new fields
in `app/config.py` plus the passthrough in `main.py`.

**Open items:** import 2021→2025 SOL history and re-run `strategy_compare.sh`
— a ROBUSTNESS CHECK, not a verdict: at the implied per-trade Sharpe, n≈60
gives E[t]≈0.89 against the 1.96 needed, so it will look confident and settle
nothing (an earlier note here wrongly said it would settle candidate B). The
sign-flip null in `docs/STRATEGY_ANALYSIS.md` §7 is the real inference work.
Also: ask the document's author the five questions
(only Q2 resting-stop fills could still rescue its table); roll MSLQ6 before
Aug 28 (no auto-rollover — `ibkr-checkout --contract-month 202609`, update
DEFAULT_CONTRACT_MONTH, restart); decide whether to stay armed meanwhile.

**Note:** `backtest` needs `bars` + `contract_metadata` from the VPS
database. It cannot run in an ephemeral Claude container — don't try.
