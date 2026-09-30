# Fix & flip breakdown

Lens `fix_flip`; label “Fix & flip”; config `v3`.

## Components

| Key | Label | Weight | Sub-score formation | Driver fields |
|---|---|---:|---|---|
| `offer_gap` | Maximum-offer gap | 0.70 | Logistic of `(MAO - asking) / asking`, midpoint −8%, steepness 7. If the 70% rule yields no positive MAO, sub-score is 0. | `price`, risk-adjusted `arv`, `rehab_cost_mid`, holding costs, `max_allowable_offer`, `offer_vs_asking_pct` |
| `levered_return` | Levered flip return | 0.30 | Logistic ROI on cash invested, midpoint 15%, steepness 9, blended with a 6% linear tail over ±400 points. | purchase/rehab/closing/holding/financing/selling costs, `projected_profit`, `cash_invested`, `roi_pct` |

ARV is first haircutted for comp dispersion above CV 0.15, reaching 12% at CV 0.35. The underwrite uses 2% purchase closing, 8% selling costs, six-month hold, 85% loan-to-cost, 12% interest and two points. MAO is `ARV × 0.70 - rehab - holding`. The ARV haircut is exported as component-level input anatomy, not applied again by recomposition.

## Adjustments, gates and confidence

1. Seller-pressure multiplier ranges 0.80–1.20 around motivated-seller neutral 35.
2. Exit/insurance multiplier subtracts 10% per insurance proxy (max 30%), 20% for ARV/sqft above 1.15× market p75, and up to 15% for median DOM above 60; combined penalty max 45%.
3. Candidate gates: uncorroborated/disputed value cap 55; comp property-type mismatch cap 60. The effective minimum cap is applied once by compression: `score × cap/100`.
4. Confidence is valuation confidence, ×0.85 when rehab is age-derived. Shrink toward 50 with floor 0.30, then clamp/round.

`min_viable_roi_pct = 5` is configured but is not a frozen scorer gate. Placeholder prices, missing price/ARV/rehab and auction opening bids are unscoreable.

## Percentile pool

City pool at ≥30 analysis rows, otherwise state; null scores excluded, ties midpoint-ranked, and `pool.n` counts non-null scores. Grade thresholds are 80/65/50/35; tier thresholds are percentiles 95/80.
