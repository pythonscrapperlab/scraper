# Airbnb (nightly) breakdown

Lens `airbnb`; label “Airbnb (nightly)”; config `v3`.

## Components

| Key | Label | Weight | Sub-score formation | Driver fields |
|---|---|---:|---|---|
| `location` | Leisure-traveller location | 0.40 | Available blend: restaurants 0.24, nightlife 0.22, vibrant 0.18, cafes 0.14, shopping 0.12, pedestrian 0.10; source values ×10. Add beach ≤1.5 mi +14, theme park ≤8 mi +12, waterfront +10 or water view +5; clamp. POI-only rows start at 40. | named `location_scores`, POI names/distances, waterfront/view |
| `revenue` | Nightly revenue proxy | 0.30 | Daily long-term rent ×2.0. Occupancy interpolates 40–65% by location tier. Subtract 28% management/cleaning, operating costs and $18/sqft furnishing amortized five years. Net yield on price+rehab maps 2%→0 to 10%→100. | rent, price, rehab, sqft, operating costs and proxy output fields |
| `property_fit` | Nightly-rental property fit | 0.30 | Bedroom base 0:25, 1:40, 2:60, 3:78, 4:92, 5+:100 (missing 50); +18 private pool or +5.4 community, +16 waterfront or +8 view, up to +10 furnished, +4 spa; clamp. | bedrooms and parsed property amenities |

Missing components export zero/unavailable and are excluded from the denominator. Revenue is always labelled a proxy, never a forecast.

## Gates, caps, bonuses and confidence

Every candidate is exported even when inactive. Priority is age-restricted cap 10; HOA/rental/minimum-lease cap 25; municipal prohibited 5 or restricted 45; condo without positive STR advertising cap 60; unverified local rule cap 78. When candidates overlap, the scorer uses the effective minimum once: `score × cap/100`. A permitted municipal rule removes only the municipal-unverified gate, not a condo-document gate.

Confidence is coverage × `(0.4 + 0.6 × rent_confidence)`, then ×0.7 when rules are unverified. Apply the floor-0.55 shrink toward 50, clamp and round. Location/property bonuses are component anatomy and are not reapplied by recomposition.

## Percentile pool

City at ≥30 analysis rows, otherwise state; null scores excluded, ties midpoint-ranked and `pool.n` counts scored rows. Grades use 80/65/50/35 and tiers use percentile 95/80.
