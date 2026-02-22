#!/usr/bin/env python3
"""
Calculate CAPEX and OPEX totals for the gas+CCS mid scenario.

This script reuses gas_ccs_cost_projection.py so assumptions stay consistent.
"""

import argparse
import json
from pathlib import Path

import gas_ccs_cost_projection as gas_proj


def main():
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Calculate total CAPEX and OPEX for gas+CCS mid scenario."
    )
    parser.add_argument(
        "--config",
        default=str(script_dir / "gas_ccs_config_midrange.json"),
        help="Path to gas+CCS JSON config file.",
    )
    parser.add_argument(
        "--fuel-scenario",
        default=None,
        help="Optional fuel scenario override (e.g. low/base/high).",
    )
    parser.add_argument(
        "--json-out",
        default=None,
        help="Optional output path for totals JSON.",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    cfg = gas_proj.load_config(config_path)
    if args.fuel_scenario is not None:
        cfg["use_fuel_price_scenario"] = args.fuel_scenario

    gas_proj.validate_config(cfg)
    cfg.update(gas_proj.resolve_capacity_and_generation(cfg))
    cfg["plant_count_for_maintenance"] = gas_proj.get_plant_count_for_maintenance(cfg)

    scenario = cfg["use_fuel_price_scenario"]
    fuel_price = float(cfg["fuel_price_scenarios_gbp_per_mwh_th"][scenario])
    projection = gas_proj.compute_yearly_projection(cfg, fuel_price)

    totals = {
        "scenario_name": cfg.get("scenario_name"),
        "config_file": str(config_path),
        "currency": cfg.get("currency", "GBP"),
        "fuel_scenario_selected": scenario,
        "fuel_price_gbp_per_mwh_th_selected": fuel_price,
        "project_lifecycle_years": int(cfg["project_lifecycle_years"]),
        "capex_gbp": float(cfg["capital_cost_total_gbp_mid"]),
        "total_opex_gbp": float(projection["total_opex_gbp"]),
        "total_discounted_opex_gbp": float(projection["total_discounted_opex_gbp"]),
        "total_expenditure_gbp": float(projection["total_expenditure_gbp"]),
        "total_discounted_expenditure_gbp": float(projection["total_discounted_expenditure_gbp"]),
        "annual_average_opex_gbp": float(projection["annual_average_opex_gbp"]),
        "maintenance_event_cost_gbp": float(projection["maintenance_event_cost_gbp"]),
        "lcoe_undiscounted_gbp_per_mwh": projection["lcoe_undiscounted_gbp_per_mwh"],
        "lcoe_discounted_gbp_per_mwh": projection["lcoe_discounted_gbp_per_mwh"],
    }

    if args.json_out is not None:
        out_path = Path(args.json_out)
    else:
        out_path = config_path.parent / "gas_ccs_mid_totals.json"

    with out_path.open("w") as f:
        json.dump(totals, f, indent=2)

    print(f"Totals written: {out_path}")
    print(f"CAPEX (GBP): {totals['capex_gbp']:,.2f}")
    print(f"Total OPEX over {totals['project_lifecycle_years']} years (GBP): {totals['total_opex_gbp']:,.2f}")
    print(f"Total expenditure over {totals['project_lifecycle_years']} years (GBP): {totals['total_expenditure_gbp']:,.2f}")


if __name__ == "__main__":
    main()
