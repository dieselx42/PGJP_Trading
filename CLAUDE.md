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

**Two unresolved contradictions in the source document:**
- NY open stated as both 14:30 UTC and 9:30 ET — an hour apart under DST.
  Deployed follows 14:30 UTC = 10:30 ET.
- CME listed SOL futures in 2025, so the document's 5-year table cannot be
  MSL — likely spot, therefore likely no commission. Would explain the gap.

See `docs/ORB_DOCUMENT_QUESTIONS.md`.

**Open items:** run `scripts/orb_experiments.sh` on the VPS and paste the
digest; ask the document's author the five questions; roll MSLQ6 before
Aug 28 (no auto-rollover — manual `ibkr-checkout --contract-month`).

**Note:** `backtest` needs `bars` + `contract_metadata` from the VPS
database. It cannot run in an ephemeral Claude container — don't try.
