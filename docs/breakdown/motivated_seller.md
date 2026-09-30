# Motivated seller breakdown

Lens `motivated_seller`; label “Motivated seller”; config `v3`.

## Components

The top-level result is an available-weight mean. Missing inputs are exported as `subscore: 0, available: false` and omitted from the denominator; a measured zero remains available.

| Key | Label | Weight | Sub-score formation | Driver fields |
|---|---|---:|---|---|
| `distress` | Distress signals | 0.30 | Highest of REO 100, probate/estate 92, foreclosure 80, short sale 74, auction 70; measured none = 0. | `is_reo`, `is_probate_or_estate`, `is_foreclosure`, `is_short_sale`, `is_auction` |
| `price_cuts` | Current-listing price pressure | 0.22 | Current listing cycle only: available blend of cut velocity 0.45 (full at 0.75/30 days), depth 0.40 (full at 15%), recency 0.15 (full through day 45, zero at day 270). Add relist 14, prior withdrawal 18, two listing agreements 6 or three+ 14; clamp. Implausible original price below 25% of current is ignored. | `price_history` event/date/price/source event ID, `days_on_market`, `price` and exported cut/churn fields |
| `days_on_market` | Time on market | 0.15 | Linear from 1× to 3× local median; absent baseline falls back to 0–120 days. | `days_on_market`, `market_stats.median_dom` |
| `discount` | Discount to estimated value | 0.12 | Linear 0–100 from 0% to 12% below derived market value. | `price`, `property_valuation.market_value` |
| `equity` | Seller equity and loss pressure | 0.13 | Estimate remaining loan as 80% of last sale; holdings ≥15 years count fully paid. Equity ramps to 60 points at 50%; asking below a purchase within five years forces 100. | last sold `price_history`, `price`, derived market value |
| `keywords` | Listing urgency language | 0.08 | 60 per high-signal phrase and 30 per medium phrase; clamp. | `description`, `ai_summary` |

## Adjustments and confidence

Apply ×1.08 if vacant, then ×0.94 if tenant occupied. Confidence is available top-level weight. Shrink toward 50 with `50 + (score-50) × (floor + (1-floor) × confidence)`: floor 0.55 at coverage ≥0.45, otherwise 0.10. Clamp and round to two decimals. `recent_cut_bonus = 12` remains configured but is not separately applied; the frozen scorer uses the recency subcomponent.

There is no top-level cap. All component bonuses and both multipliers are exported whether triggered or not.

## Percentile pool

City is used when it has at least 30 analysis rows; otherwise state. Null lens scores are excluded from rank and `pool.n`; ties receive midpoint rank. Grades are A≥80, B≥65, C≥50, D≥35, else F. Tiers are `top` at percentile ≥95, `strong` at ≥80, else `rest`.
