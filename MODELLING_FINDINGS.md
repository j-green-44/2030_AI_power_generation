# Modelling Findings (Strict Cyclic Non-Depleting Set)

This document supersedes earlier relaxed/depleting renewable+H2 results and earlier
"strict" results that did not explicitly enforce a cyclic non-depleting rule for
wind-only and solar-only optimizations.

## 1) Superseded (Discarded) Results

The following outputs are superseded and should not be used in conclusions:

1. `hybrid/hybrid_dispatch_no_gas_apples_opt_summary.json`
2. `hybrid/hybrid_dispatch_no_gas_opt_summary.json`
3. `hybrid/hybrid_dispatch_relaxed_opt_summary.json`
4. `wind/h2_total_expenditure_25y_nondepleting_summary.json`
5. `solar/solar_h2_total_expenditure_25y_nondepleting_summary.json`

## 2) Active Source Outputs (Strict Cyclic Set)

| Scenario | Summary File | Component Cost File |
|---|---|---|
| Wind + H2 (25y cyclic non-depleting) | `wind/h2_total_expenditure_25y_cyclic_summary.json` | `wind/h2_total_expenditure_25y_cyclic_component_cost_table.csv` |
| Solar + H2 (25y cyclic non-depleting) | `solar/solar_h2_total_expenditure_25y_cyclic_summary.json` | N/A (no feasible design in current bounds) |
| Hybrid Wind+Solar+H2, no gas (25y cyclic non-depleting) | `hybrid/hybrid_dispatch_no_gas_25y_nondepleting_opt_summary.json` | `hybrid/hybrid_dispatch_no_gas_25y_nondepleting_opt_component_cost_table.csv` |
| Hybrid 25y carry-over validation | `hybrid/hybrid_dispatch_no_gas_25y_nondepleting_validation.json` | N/A |
| Consolidated strict-cyclic comparison table | `strict_25y_cyclic_cost_comparison.csv` | N/A |

## 3) Common Constraint Set

| Parameter | Value |
|---|---|
| Demand | 8,200 MW flat |
| Lifecycle horizon | 25 years |
| Discount rate | 0.0 |
| H2 charge efficiency | 0.70 |
| H2 discharge efficiency | 0.64 |
| SOC floor | 10% |
| SOC ceiling | 100% |
| Start SOC | 100% |
| Cyclic requirement (wind/solar) | `require_h2_cyclic_non_depleting = true` |
| Cyclic SOC tolerance | 1.0 MWh(H2) |
| Currency basis | GBP_2026_placeholder |

## 4) Feasibility Rules Applied

| Scenario | Feasibility Rule Used | Pass/Fail |
|---|---|---|
| Wind + H2 | Repeated-year feasibility + explicit cyclic non-depleting check (`final_soc + tol >= start_soc`) | Pass |
| Solar + H2 | Repeated-year feasibility + explicit cyclic non-depleting check (`final_soc + tol >= start_soc`) | Fail in current search bounds |
| Hybrid Wind+Solar+H2 (no gas) | One-year cyclic non-depleting constraint (`end_soc >= start_soc`) + explicit 25-year carry-over replay | Pass |

## 5) 25-Year Cost Comparison (Strict Cyclic Only)

| Scenario | CAPEX (GBP bn) | OPEX, 25y (GBP bn) | Total Expenditure, 25y (GBP bn) | Implied Cost (GBP/MWh) |
|---|---:|---:|---:|---:|
| Wind + H2 | 118.84 | 124.49 | 243.33 | 135.50 |
| Solar + H2 | N/A | N/A | N/A | N/A |
| Hybrid Wind+Solar+H2 (no gas) | 129.61 | 117.66 | 247.27 | 137.70 |

## 6) Optimized Capacity Mix (Strict Cyclic Only)

| Scenario | Wind (MW) | Solar (MW) | Electrolyzer (MW) | H2 Turbine (MW) | H2 Storage (MWh-H2) |
|---|---:|---:|---:|---:|---:|
| Wind + H2 | 34,000.00 | 0.00 | 14,000.00 | 8,199.78 | 1,408,850.10 |
| Solar + H2 | N/A | N/A | N/A | N/A | N/A |
| Hybrid Wind+Solar+H2 (no gas) | 32,000.00 | 20,000.00 | 14,000.00 | 8,200.00 | 2,000,000.00 |

## 7) SOC Outcomes Under Strict Cyclic Runs

| Scenario | Start SOC (%) | End SOC (%) | Minimum SOC (%) |
|---|---:|---:|---:|
| Wind + H2 | 100.00 | 100.00 | 10.37 |
| Solar + H2 | N/A | N/A | N/A |
| Hybrid Wind+Solar+H2 (no gas) | 100.00 | 100.00 | 71.05 |

Hybrid carry-over replay (`hybrid/hybrid_dispatch_no_gas_25y_nondepleting_validation.json`)
confirms year-by-year operation remains feasible over 25 repeated years with no gas
use and no SOC drift below start.

## 8) Search Coverage

| Model | Search Stats |
|---|---|
| Wind + H2 | `wind_candidates=18, evaluated_points=222, feasible_points=91` |
| Solar + H2 | `solar_candidates=20, evaluated_points=420, feasible_points=0` |
| Hybrid Wind+Solar+H2 (no gas) | `total_candidate_points=48510, skipped_structural_points=4410, simulated_points=44100, feasible_points=5229` |

## 9) Top Cost Drivers (Strict Cyclic Set)

| Scenario | Top CAPEX Components | Top OPEX Components (25y) |
|---|---|---|
| Wind + H2 | wind (85.00 bn); electrolyzer (21.00 bn); h2_turbine (8.61 bn) | wind (76.50 bn); electrolyzer (40.57 bn); h2_turbine (7.35 bn) |
| Solar + H2 | N/A (no feasible design in current configured bounds) | N/A |
| Hybrid Wind+Solar+H2 (no gas) | wind (80.00 bn); electrolyzer (21.00 bn); solar (14.00 bn) | wind (72.00 bn); electrolyzer (31.64 bn); solar (7.00 bn) |

## 10) Key Findings (Strict Cyclic Conclusions)

1. After explicit cyclic enforcement, **Wind + H2** cost increased materially and the optimum shifted to higher wind/electrolyzer and lower storage.
2. Under current search bounds, **Solar + H2 has no feasible cyclic non-depleting solution**.
3. **Wind + H2** is currently the lowest-cost feasible strict-cyclic case (**GBP 243.33 bn**), with **Hybrid Wind+Solar+H2 (no gas)** close behind (**GBP 247.27 bn**).
4. The strict-cyclic ranking in this repository is now: **Wind + H2 < Hybrid Wind+Solar+H2 (no gas)**, with solar-only unresolved under current bounds.
