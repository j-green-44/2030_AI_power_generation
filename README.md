# 2030 AI Power Generation Case Study

Deterministic techno-economic modelling workflow for comparing:
- Wind + H2
- Solar + H2
- Gas + CCS
- Hybrid dispatch (wind/solar/H2 with optional gas)

All models are hourly, use a flat `8,200 MW` demand baseline, and evaluate 25-year lifecycle expenditure under configurable assumptions.

## Repository Structure

- `wind/`: wind + hydrogen sizing, CAPEX, and total expenditure models
- `solar/`: solar + hydrogen total expenditure model
- `gas/`: gas + CCS lifecycle cost model and fuel-price projection plot
- `hybrid/`: dispatch-coupled hybrid optimizer and hybrid plotting tools
- `refrecnes/`: parameter-reference table generator and exported reference CSVs
- `archive/`: superseded scripts and legacy outputs archived during cleanup

## Inputs

Primary generation profiles:
- `wind/ninja_wind_56.4559_-1.3674_corrected.csv`
- `solar/ninja_pv_50.9037_-1.4030_corrected.csv`

Main config files:
- `wind/hydrogen_storage_config.json`
- `solar/solar_config_midrange.json`
- `gas/gas_ccs_config_midrange.json`
- `hybrid/hybrid_dispatch_config_no_gas_25y_nondepleting.json`
- `hybrid/hybrid_dispatch_config.json`

## Environment

Uses Python 3 standard library only (no external packages required).

## Reproduce Core Runs

Run from repo root (`/home/User/Documents/energy`).

### 1) Wind + H2

```bash
python3 wind/hydrogen_storage_sizing.py --config wind/hydrogen_storage_config.json
python3 wind/optimize_h2_capex.py --config wind/hydrogen_storage_config.json
python3 wind/optimize_h2_total_expenditure.py --config wind/hydrogen_storage_config.json
python3 wind/plot_wind_h2_dispatch.py --config wind/hydrogen_storage_config.json --summary wind/h2_storage_from_config_summary.json
```

### 2) Solar + H2

```bash
python3 solar/optimize_solar_h2_total_expenditure.py --config solar/solar_config_midrange.json
```

### 3) Gas + CCS

```bash
python3 gas/gas_ccs_cost_projection.py --config gas/gas_ccs_config_midrange.json
python3 gas/plot_gas_ccs_fuel_price_projection.py --config gas/gas_ccs_config_midrange.json --wind-summary wind/h2_total_expenditure_opt_summary.json
```

### 4) Hybrid Dispatch (strict no-gas, cyclic non-depleting)

```bash
python3 hybrid/optimize_hybrid_dispatch.py \
  --config hybrid/hybrid_dispatch_config_no_gas_25y_nondepleting.json \
  --output-prefix hybrid/hybrid_dispatch_no_gas_25y_nondepleting_opt
```

### 5) Hybrid Dispatch (gas allowed relaxed variant)

```bash
python3 hybrid/optimize_hybrid_dispatch.py \
  --config hybrid/hybrid_dispatch_config.json \
  --output-prefix hybrid/hybrid_dispatch_relaxed_opt
```

## Hybrid Plotting (recommended explicit summary paths)

Use explicit `--summary` because historical defaults may point to archived files.

```bash
python3 hybrid/plot_hybrid_wind_solar_h2_dispatch.py \
  --summary hybrid/hybrid_dispatch_no_gas_25y_nondepleting_opt_summary.json \
  --output hybrid/hybrid_wind_solar_h2_dispatch_vs_flat_load.svg

python3 hybrid/plot_hybrid_monthly_energy_balance.py \
  --summary hybrid/hybrid_dispatch_no_gas_25y_nondepleting_opt_summary.json \
  --output hybrid/hybrid_monthly_energy_balance.svg \
  --fullness-mode end
```

## Key Active Outputs

- `MODELLING_FINDINGS.md`
- `strict_25y_cyclic_cost_comparison.csv`
- `wind/h2_total_expenditure_25y_cyclic_summary.json`
- `wind/h2_total_expenditure_25y_cyclic_component_cost_table.csv`
- `solar/solar_h2_total_expenditure_25y_cyclic_summary.json`
- `hybrid/hybrid_dispatch_no_gas_25y_nondepleting_opt_summary.json`
- `hybrid/hybrid_dispatch_no_gas_25y_nondepleting_opt_component_cost_table.csv`
- `hybrid/hybrid_dispatch_no_gas_25y_nondepleting_validation.json`

## Archived Content

Superseded scripts and legacy CSV/JSON outputs were moved to:
- `archive/redundant_scripts/`
- `archive/redundant_data/`

File manifest of archived data:
- `archive/redundant_data/MOVED_FILES.txt`

