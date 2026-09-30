# Mid-term rental (30+ days) breakdown

Lens `str`; label “Mid-term rental (30+ days)”; config `v3`.

## Components

| Key | Label | Weight | Sub-score formation | Driver fields |
|---|---|---:|---|---|
| `location` | Mid-term resident location | 0.30 | Available blend: quiet 0.26, wellness 0.20, groceries 0.18, restaurants 0.14, parks 0.12, pedestrian 0.10; 0–10 values ×10. | named `location_scores` fields |
| `revenue` | Seasonal rental economics | 0.40 | Seasonal net vs annual lease: four in-season months at city override (default 1.9×), four shoulder months at 1.1×, four vacant. Subtract 15% management, operating costs and $12/sqft furnishing amortized five years. Blend uplift score 0.55 (full at +40%) and net-yield score 0.45 (2%→0, 8%→100). | rent/price/sqft/operating costs, market city/state, derived seasonal and annual net fields |
| `suitability` | Seasonal tenant suitability | 0.30 | Starts 50; +10 age-restricted, up to +14 furnished, +5 pool, +10 hospital within three miles, −12 lease restriction, +4 pets; clamp. | age/amenity fields and POI name/distance |

Unavailable components are exported as zero/unavailable and omitted from the denominator.

## Adjustments, gates and confidence

Component bonuses are shown but not reapplied. After blending, subtract 25 for minimum lease ≥7 months. If annual lease uplift is negative, apply cap 45 by compression. Confidence is coverage × `(0.5 + 0.5 × rent_confidence)`, then floor-0.55 shrink toward 50, clamp and round.

## Percentile pool

City at ≥30 analysis rows, otherwise state; null scores excluded, ties midpoint-ranked, `pool.n` counts scored rows. Grades use 80/65/50/35 and tiers use percentile 95/80.
