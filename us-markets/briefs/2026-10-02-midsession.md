# Mid-session sudden-movement check — Fri 2026-10-02 (1:00 PM ET / 10:30 PM IST)

Reference levels: Finnhub prev closes (note: state.json `last_prices` differ slightly for TSM/ASML — seeded from an earlier test run). Intraday quotes: Finnhub, ~1:00 PM ET (delayed ~15 min; never invent a price).

## All 8 at a glance (vs prev close)

| Name | Prev close | Intraday | Move | Alert? |
|---|---|---|---|---|
| GOOGL (holding) | 338.24 | 343.63 | +1.6% | — |
| NVDA (holding) | 230.86 | 235.12 | +1.9% | — |
| AAPL (holding/watch) | 330.32 | 333.43 | +0.9% | — |
| MSFT (holding) | 512.80 | 513.92 | +0.2% | — |
| META (watch) | 725.93 | 730.58 | +0.6% | — |
| TSM (watch) | 459.20 | 472.50 | +2.9% | ⚠️ ≥2% with news |
| ASML (watch) | 1808.49 | 1866.60 | +3.2% | ⚠️ ≥3% |
| AMZN (watch) | 248.23 | 250.91 | +1.1% | — |

Context: SPY +1.0%, QQQ +1.2% — broad risk-on, semis leading. NVDA +1.9% rides the same bid but stays below the flag line.

## ⚠️ ASML — +3.2% to $1,866.60

**What happened:** Two verified drivers compounding.
1. WSJ live markets update (0923 GMT today): report (S. Korean daily MK) that Samsung is quoting ~$4/gigabit for HBM4 memory — ~3× current rates. Tight-memory signal lifted European chip-equipment stocks (ASML +2.2% in Europe, ASM Intl +4.7%, Infineon +6%, STMicro +3.3%) and carried into the US session.
2. Sector sympathy from Synopsys (+12.8%): at its Oct 1 Investor Day, SNPS guided FY27 revenue $11.1–11.2B (vs $10.8B consensus), EPS $19.04–19.12 (vs $17.81), announced an OpenAI revenue-share deal for a chip-design AI model and a >$1B multi-year AWS IP licensing deal, targeting ~15% CAGR FY26–FY30 (Reuters, Oct 1; Investor's Business Daily). Fellow equipment/semi names also bid: AMAT +3.5%, LRCX +3.5%, AMD +3.1%, MRVL +3.5%.

**Koda's read:** Justified in direction — real pricing-power evidence (memory is the tightest link in AI hardware) plus a genuine AI-capex sentiment catalyst. Caution: the move is sentiment-driven, not ASML-specific news; ASML is +74% YTD with a 2.16 beta and reports Oct 14. Entry-timing lean: **hold off adding today** — better to wait for a pullback or the Oct 14 print to size a position.

## ⚠️ TSM — +2.9% to $472.50

**What happened:** Same two drivers — Samsung's HBM price quotes (validates strong AI memory demand for TSMC's leading nodes) and the Synopsys-led AI-capex bid lifting the whole semi complex.

**Koda's read:** Justified in direction, arguably stretched: +2.9% on sector beta ahead of confirmed Oct 15 earnings, with the stock +55% YTD and ~6% in five sessions. Entry-timing lean: **wait** — a strong quarter looks largely priced; prefer a post-earnings dip or a fade back toward the low-$440s to enter.

## Notes
- AMZN (+1.1%): the AWS >$1B Synopsys IP deal is a small positive for AWS custom-silicon momentum; not a mover on its own.
- Sources: US financial press (WSJ, Reuters, Investor's Business Daily) and Finnhub quotes. No Indian sources used.
- No change needed to `data/state.json` in this run; close recap will record closing prices.

*This is analysis, not financial advice — informational views only.*
