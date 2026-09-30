# Buy & hold breakdown

Lens `buy_hold`; label “Buy & hold”; config `v3`.

## Components

| Key | Label | Weight | Sub-score formation | Driver fields |
|---|---|---:|---|---|
| `cap_rate` | Net operating income yield | 0.50 | NOI over price plus required rehab; linear 2.0%→0 to 8.5%→100. | `cap_rate`, `noi_annual`, `price`, `rehab_cost_mid`, `annual_*` costs |
| `yield_vs_market` | Yield versus local market | 0.20 | Logistic of gross yield ÷ local median, midpoint 1.0, steepness 0.12. | `gross_yield`, `market_stats.median_gross_yield` |
| `appreciation` | Market appreciation | 0.15 | Linear −5%→0 to +8%→100. | `market_snapshots.yoy_price_change_pct` |
| `location` | Long-term tenant location | 0.15 | Available blend of school quality 45% and everyday amenities 55%, with 0–10 values scaled to 0–100. | named `location_scores` fields |

Missing components are exported as zero/unavailable and omitted from the denominator.

## Adjustments, gates and confidence

Add 6 for tenant in place; subtract 4 for HOA approval. Florida attached-unit risk applies a multiplier down to 0.60 from pre-1992 age, high HOA, assessment/reserve wording and cash-only status. Candidate caps are condo-hotel 25, attached unit missing HOA 45, and negative monthly cash flow or DSCR below 1.0 at 55. The minimum effective cap is applied once by compression.

Cash-on-cash, DSCR and monthly cash flow use 25% down, 6.75%/30-year debt, 2% closing and cash-funded rehab; they drive gates and explanations but are not extra weighted components. Confidence is coverage × `(0.4 + 0.6 × rent_confidence)`, then the standard floor-0.55 shrink, clamp and round.

## Percentile pool

City at ≥30 analysis rows, otherwise state. Null lens scores are excluded, ties get midpoint rank, and `pool.n` counts scored rows. Grades use 80/65/50/35; tiers use percentile 95/80.
