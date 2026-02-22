#!/usr/bin/env python3
"""
Project CAPEX and OPEX for a gas + CCS fleet over a lifecycle horizon.

This script is intentionally deterministic and transparent so results are
reproducible from a single JSON config file.
"""

import argparse
import csv
import json
import math
from pathlib import Path


DEFAULTS = {
    "use_fuel_price_scenario": "base",
    "output_prefix": "gas_ccs_midrange",
    "write_yearly_csv": True,
    "hours_per_year": 8760.0,
    "natural_gas_emissions_tco2_per_mwh_th": 0.184,
    # If True, major-maintenance costing uses whole plants.
    "round_plant_count_for_maintenance_up": False,
    # Major-maintenance costing basis: per_plant | per_mw | per_gw
    "major_maintenance_cost_basis": "per_plant",
}


def load_config(path: Path):
    with path.open() as f:
        loaded = json.load(f)
    if not isinstance(loaded, dict):
        raise ValueError("Config file must contain a JSON object.")
    cfg = DEFAULTS.copy()
    cfg.update(loaded)

    # Backward-compatible defaults for per-capacity maintenance costing.
    if "major_maintenance_cost_gbp_per_mw_mid" not in loaded:
        unit_mw = float(cfg["plant_unit_capacity_mw"])
        if unit_mw <= 0:
            raise ValueError("plant_unit_capacity_mw must be > 0.")
        cfg["major_maintenance_cost_gbp_per_mw_mid"] = (
            float(cfg["major_maintenance_cost_gbp_per_plant_mid"]) / unit_mw
        )
    if "major_maintenance_cost_gbp_per_gw_mid" not in loaded:
        cfg["major_maintenance_cost_gbp_per_gw_mid"] = (
            float(cfg["major_maintenance_cost_gbp_per_mw_mid"]) * 1000.0
        )
    return cfg


def validate_config(cfg):
    lifecycle_years = int(cfg["project_lifecycle_years"])
    if lifecycle_years < 1:
        raise ValueError("project_lifecycle_years must be >= 1.")

    discount_rate = float(cfg["discount_rate"])
    if discount_rate < 0:
        raise ValueError("discount_rate must be >= 0.")

    demand_mw = float(cfg["demand_mw"])
    if demand_mw <= 0:
        raise ValueError("demand_mw must be > 0.")

    load_factor = float(cfg["load_factor"])
    if load_factor <= 0 or load_factor > 1:
        raise ValueError("load_factor must be in (0, 1].")

    if float(cfg["hours_per_year"]) <= 0:
        raise ValueError("hours_per_year must be > 0.")

    if float(cfg["net_heat_rate_mwh_th_per_mwh_e"]) <= 0:
        raise ValueError("net_heat_rate_mwh_th_per_mwh_e must be > 0.")

    capture_rate_pct = float(cfg["ccs_capture_rate_pct_mid"])
    if capture_rate_pct < 0 or capture_rate_pct > 100:
        raise ValueError("ccs_capture_rate_pct_mid must be in [0, 100].")

    for key in [
        "fixed_om_gbp_per_mw_year_mid",
        "variable_om_gbp_per_mwh_e",
        "ccs_consumables_gbp_per_mwh_e_mid_assumption",
        "co2_transport_storage_cost_gbp_per_tco2_mid",
        "residual_emissions_carbon_cost_gbp_per_tco2",
        "natural_gas_emissions_tco2_per_mwh_th",
        "capital_cost_total_gbp_mid",
        "major_maintenance_cost_gbp_per_plant_mid",
    ]:
        if float(cfg[key]) < 0:
            raise ValueError(f"{key} must be >= 0.")

    maintenance_interval = int(cfg["major_maintenance_interval_years_assumption"])
    if maintenance_interval < 1:
        raise ValueError("major_maintenance_interval_years_assumption must be >= 1.")

    if float(cfg["plant_unit_capacity_mw"]) <= 0:
        raise ValueError("plant_unit_capacity_mw must be > 0.")

    if int(cfg["plant_count_nominal"]) < 1:
        raise ValueError("plant_count_nominal must be >= 1.")

    maintenance_basis = str(cfg.get("major_maintenance_cost_basis", "per_plant")).lower()
    if maintenance_basis not in {"per_plant", "per_mw", "per_gw"}:
        raise ValueError("major_maintenance_cost_basis must be one of: per_plant, per_mw, per_gw.")
    if float(cfg["major_maintenance_cost_gbp_per_plant_mid"]) < 0:
        raise ValueError("major_maintenance_cost_gbp_per_plant_mid must be >= 0.")
    if float(cfg["major_maintenance_cost_gbp_per_mw_mid"]) < 0:
        raise ValueError("major_maintenance_cost_gbp_per_mw_mid must be >= 0.")
    if float(cfg["major_maintenance_cost_gbp_per_gw_mid"]) < 0:
        raise ValueError("major_maintenance_cost_gbp_per_gw_mid must be >= 0.")

    fuel_scenarios = cfg.get("fuel_price_scenarios_gbp_per_mwh_th")
    if not isinstance(fuel_scenarios, dict) or not fuel_scenarios:
        raise ValueError("fuel_price_scenarios_gbp_per_mwh_th must be a non-empty object.")

    selected = cfg["use_fuel_price_scenario"]
    if selected not in fuel_scenarios:
        raise ValueError(
            f"use_fuel_price_scenario '{selected}' not in "
            "fuel_price_scenarios_gbp_per_mwh_th."
        )

    if float(fuel_scenarios[selected]) < 0:
        raise ValueError("Selected fuel price scenario must be >= 0.")


def resolve_capacity_and_generation(cfg):
    demand_mw = float(cfg["demand_mw"])
    load_factor = float(cfg["load_factor"])
    hours_per_year = float(cfg["hours_per_year"])

    required_capacity_from_demand_mw = demand_mw / load_factor
    required_installed_capacity_mw = float(
        cfg.get("required_installed_capacity_mw", required_capacity_from_demand_mw)
    )

    annual_generation_from_demand_mwh = demand_mw * hours_per_year
    annual_generation_from_capacity_mwh = required_installed_capacity_mw * load_factor * hours_per_year

    return {
        "required_capacity_from_demand_mw": required_capacity_from_demand_mw,
        "required_installed_capacity_mw": required_installed_capacity_mw,
        "annual_generation_from_demand_mwh": annual_generation_from_demand_mwh,
        "annual_generation_from_capacity_mwh": annual_generation_from_capacity_mwh,
        # For service-equivalent comparison, tie to demand served.
        "annual_generation_mwh_modelled": annual_generation_from_demand_mwh,
    }


def get_plant_count_for_maintenance(cfg):
    if bool(cfg["round_plant_count_for_maintenance_up"]):
        required_capacity = float(cfg["required_installed_capacity_mw"])
        unit_capacity = float(cfg["plant_unit_capacity_mw"])
        return max(1, math.ceil(required_capacity / unit_capacity))
    return int(cfg["plant_count_nominal"])


def compute_maintenance_event_cost(cfg):
    basis = str(cfg.get("major_maintenance_cost_basis", "per_plant")).lower()
    if basis == "per_plant":
        return float(cfg["major_maintenance_cost_gbp_per_plant_mid"]) * float(
            cfg["plant_count_for_maintenance"]
        )
    if basis == "per_mw":
        return float(cfg["major_maintenance_cost_gbp_per_mw_mid"]) * float(
            cfg["required_installed_capacity_mw"]
        )
    if basis == "per_gw":
        return float(cfg["major_maintenance_cost_gbp_per_gw_mid"]) * (
            float(cfg["required_installed_capacity_mw"]) / 1000.0
        )
    raise ValueError(
        "Unsupported major_maintenance_cost_basis. Use per_plant, per_mw, or per_gw."
    )


def compute_annual_opex_components(cfg, annual_generation_mwh, fuel_price_gbp_per_mwh_th):
    heat_rate = float(cfg["net_heat_rate_mwh_th_per_mwh_e"])
    fixed_om = float(cfg["fixed_om_gbp_per_mw_year_mid"]) * float(cfg["required_installed_capacity_mw"])
    variable_om = float(cfg["variable_om_gbp_per_mwh_e"]) * annual_generation_mwh
    consumables = (
        float(cfg["ccs_consumables_gbp_per_mwh_e_mid_assumption"]) * annual_generation_mwh
    )

    thermal_input_mwh_th = annual_generation_mwh * heat_rate
    fuel_cost = thermal_input_mwh_th * fuel_price_gbp_per_mwh_th

    gas_emissions_factor = float(cfg["natural_gas_emissions_tco2_per_mwh_th"])
    gross_co2_tonnes = thermal_input_mwh_th * gas_emissions_factor
    capture_rate = float(cfg["ccs_capture_rate_pct_mid"]) / 100.0
    captured_co2_tonnes = gross_co2_tonnes * capture_rate
    residual_co2_tonnes = gross_co2_tonnes - captured_co2_tonnes

    t_and_s_cost = captured_co2_tonnes * float(cfg["co2_transport_storage_cost_gbp_per_tco2_mid"])
    residual_carbon_cost = (
        residual_co2_tonnes * float(cfg["residual_emissions_carbon_cost_gbp_per_tco2"])
    )

    return {
        "annual_generation_mwh": annual_generation_mwh,
        "thermal_input_mwh_th": thermal_input_mwh_th,
        "gross_co2_tonnes": gross_co2_tonnes,
        "captured_co2_tonnes": captured_co2_tonnes,
        "residual_co2_tonnes": residual_co2_tonnes,
        "fuel_cost_gbp": fuel_cost,
        "fixed_om_gbp": fixed_om,
        "variable_om_gbp": variable_om,
        "ccs_consumables_gbp": consumables,
        "co2_t_and_s_cost_gbp": t_and_s_cost,
        "residual_carbon_cost_gbp": residual_carbon_cost,
    }


def compute_yearly_projection(cfg, fuel_price_gbp_per_mwh_th):
    lifecycle_years = int(cfg["project_lifecycle_years"])
    discount_rate = float(cfg["discount_rate"])
    annual_generation_mwh = float(cfg["annual_generation_mwh_modelled"])

    base = compute_annual_opex_components(cfg, annual_generation_mwh, fuel_price_gbp_per_mwh_th)
    maintenance_interval = int(cfg["major_maintenance_interval_years_assumption"])
    maintenance_event_cost = compute_maintenance_event_cost(cfg)

    base_opex_no_maintenance = (
        base["fuel_cost_gbp"]
        + base["fixed_om_gbp"]
        + base["variable_om_gbp"]
        + base["ccs_consumables_gbp"]
        + base["co2_t_and_s_cost_gbp"]
        + base["residual_carbon_cost_gbp"]
    )

    rows = []
    cumulative_opex = 0.0
    cumulative_discounted_opex = 0.0
    capex_total = float(cfg["capital_cost_total_gbp_mid"])

    discounted_generation_mwh = 0.0
    undiscounted_generation_mwh = annual_generation_mwh * lifecycle_years

    for year in range(1, lifecycle_years + 1):
        maintenance_cost = maintenance_event_cost if (year % maintenance_interval == 0) else 0.0
        total_opex = base_opex_no_maintenance + maintenance_cost
        discount_factor = 1.0 / ((1.0 + discount_rate) ** year) if discount_rate > 0 else 1.0
        discounted_opex = total_opex * discount_factor
        discounted_generation_mwh += annual_generation_mwh * discount_factor

        cumulative_opex += total_opex
        cumulative_discounted_opex += discounted_opex

        rows.append(
            {
                "year": year,
                "annual_generation_mwh": annual_generation_mwh,
                "thermal_input_mwh_th": base["thermal_input_mwh_th"],
                "gross_co2_tonnes": base["gross_co2_tonnes"],
                "captured_co2_tonnes": base["captured_co2_tonnes"],
                "residual_co2_tonnes": base["residual_co2_tonnes"],
                "fuel_cost_gbp": base["fuel_cost_gbp"],
                "fixed_om_gbp": base["fixed_om_gbp"],
                "variable_om_gbp": base["variable_om_gbp"],
                "ccs_consumables_gbp": base["ccs_consumables_gbp"],
                "co2_t_and_s_cost_gbp": base["co2_t_and_s_cost_gbp"],
                "residual_carbon_cost_gbp": base["residual_carbon_cost_gbp"],
                "major_maintenance_cost_gbp": maintenance_cost,
                "total_opex_gbp": total_opex,
                "discount_factor": discount_factor,
                "discounted_opex_gbp": discounted_opex,
                "cumulative_opex_gbp": cumulative_opex,
                "cumulative_discounted_opex_gbp": cumulative_discounted_opex,
                "cumulative_total_expenditure_gbp": capex_total + cumulative_opex,
                "cumulative_discounted_total_expenditure_gbp": capex_total
                + cumulative_discounted_opex,
            }
        )

    total_opex_gbp = cumulative_opex
    total_discounted_opex_gbp = cumulative_discounted_opex
    total_expenditure_gbp = capex_total + total_opex_gbp
    total_discounted_expenditure_gbp = capex_total + total_discounted_opex_gbp

    lcoe_undiscounted_gbp_per_mwh = (
        total_expenditure_gbp / undiscounted_generation_mwh
        if undiscounted_generation_mwh > 0
        else None
    )
    lcoe_discounted_gbp_per_mwh = (
        total_discounted_expenditure_gbp / discounted_generation_mwh
        if discounted_generation_mwh > 0
        else None
    )

    return {
        "base_annual_components": base,
        "base_opex_no_maintenance_gbp": base_opex_no_maintenance,
        "maintenance_event_cost_gbp": maintenance_event_cost,
        "rows": rows,
        "total_opex_gbp": total_opex_gbp,
        "total_discounted_opex_gbp": total_discounted_opex_gbp,
        "total_expenditure_gbp": total_expenditure_gbp,
        "total_discounted_expenditure_gbp": total_discounted_expenditure_gbp,
        "annual_average_opex_gbp": total_opex_gbp / lifecycle_years,
        "lcoe_undiscounted_gbp_per_mwh": lcoe_undiscounted_gbp_per_mwh,
        "lcoe_discounted_gbp_per_mwh": lcoe_discounted_gbp_per_mwh,
    }


def summarize_fuel_scenarios(cfg):
    scenarios = cfg["fuel_price_scenarios_gbp_per_mwh_th"]
    out = {}
    for name, price in scenarios.items():
        proj = compute_yearly_projection(cfg, float(price))
        out[name] = {
            "fuel_price_gbp_per_mwh_th": float(price),
            "total_opex_gbp": proj["total_opex_gbp"],
            "total_discounted_opex_gbp": proj["total_discounted_opex_gbp"],
            "total_expenditure_gbp": proj["total_expenditure_gbp"],
            "total_discounted_expenditure_gbp": proj["total_discounted_expenditure_gbp"],
            "annual_average_opex_gbp": proj["annual_average_opex_gbp"],
            "lcoe_undiscounted_gbp_per_mwh": proj["lcoe_undiscounted_gbp_per_mwh"],
            "lcoe_discounted_gbp_per_mwh": proj["lcoe_discounted_gbp_per_mwh"],
        }
    return out


def write_yearly_csv(path: Path, rows):
    if not rows:
        with path.open("w", newline="") as f:
            f.write("")
        return
    with path.open("w", newline="") as f:
        fieldnames = list(rows[0].keys())
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Project 25-year CAPEX/OPEX for a gas + CCS fleet from config."
    )
    parser.add_argument(
        "--config",
        default=str(script_dir / "gas_ccs_config_midrange.json"),
        help="Path to gas+CCS JSON config file.",
    )
    parser.add_argument(
        "--output-prefix",
        help="Output prefix path. Defaults to <config_dir>/<output_prefix> from config.",
    )
    parser.add_argument(
        "--fuel-scenario",
        help="Fuel scenario name in fuel_price_scenarios_gbp_per_mwh_th (e.g., base).",
    )
    parser.add_argument(
        "--write-yearly-csv",
        dest="write_yearly_csv",
        action="store_true",
        help="Write year-by-year CSV output (overrides config).",
    )
    parser.add_argument(
        "--no-write-yearly-csv",
        dest="write_yearly_csv",
        action="store_false",
        help="Do not write year-by-year CSV output (overrides config).",
    )
    parser.set_defaults(write_yearly_csv=None)
    args = parser.parse_args()

    config_path = Path(args.config)
    cfg = load_config(config_path)

    if args.fuel_scenario is not None:
        cfg["use_fuel_price_scenario"] = args.fuel_scenario
    if args.write_yearly_csv is not None:
        cfg["write_yearly_csv"] = args.write_yearly_csv

    validate_config(cfg)

    derived = resolve_capacity_and_generation(cfg)
    cfg.update(derived)
    cfg["plant_count_for_maintenance"] = get_plant_count_for_maintenance(cfg)

    selected_scenario = cfg["use_fuel_price_scenario"]
    selected_fuel_price = float(cfg["fuel_price_scenarios_gbp_per_mwh_th"][selected_scenario])
    projection = compute_yearly_projection(cfg, selected_fuel_price)
    scenario_summary = summarize_fuel_scenarios(cfg)

    if args.output_prefix is not None:
        out_prefix = Path(args.output_prefix)
    else:
        out_prefix = config_path.parent / str(cfg["output_prefix"])
    summary_path = Path(f"{out_prefix}_summary.json")
    yearly_csv_path = Path(f"{out_prefix}_yearly.csv")

    summary = {
        "inputs": {
            "config_file": str(config_path),
            "scenario_name": cfg.get("scenario_name"),
            "currency": cfg.get("currency", "GBP"),
            "project_lifecycle_years": int(cfg["project_lifecycle_years"]),
            "discount_rate": float(cfg["discount_rate"]),
            "demand_mw": float(cfg["demand_mw"]),
            "load_factor": float(cfg["load_factor"]),
            "hours_per_year": float(cfg["hours_per_year"]),
            "required_capacity_from_demand_mw": float(cfg["required_capacity_from_demand_mw"]),
            "required_installed_capacity_mw": float(cfg["required_installed_capacity_mw"]),
            "annual_generation_from_demand_mwh": float(cfg["annual_generation_from_demand_mwh"]),
            "annual_generation_from_capacity_mwh": float(cfg["annual_generation_from_capacity_mwh"]),
            "annual_generation_mwh_modelled": float(cfg["annual_generation_mwh_modelled"]),
            "plant_unit_capacity_mw": float(cfg["plant_unit_capacity_mw"]),
            "plant_count_nominal": int(cfg["plant_count_nominal"]),
            "plant_count_for_maintenance": int(cfg["plant_count_for_maintenance"]),
            "capital_cost_total_gbp_mid": float(cfg["capital_cost_total_gbp_mid"]),
            "fixed_om_gbp_per_mw_year_mid": float(cfg["fixed_om_gbp_per_mw_year_mid"]),
            "variable_om_gbp_per_mwh_e": float(cfg["variable_om_gbp_per_mwh_e"]),
            "ccs_consumables_gbp_per_mwh_e_mid_assumption": float(
                cfg["ccs_consumables_gbp_per_mwh_e_mid_assumption"]
            ),
            "fuel_scenario_selected": selected_scenario,
            "fuel_price_gbp_per_mwh_th_selected": selected_fuel_price,
            "net_heat_rate_mwh_th_per_mwh_e": float(cfg["net_heat_rate_mwh_th_per_mwh_e"]),
            "natural_gas_emissions_tco2_per_mwh_th": float(
                cfg["natural_gas_emissions_tco2_per_mwh_th"]
            ),
            "ccs_capture_rate_pct_mid": float(cfg["ccs_capture_rate_pct_mid"]),
            "co2_transport_storage_cost_gbp_per_tco2_mid": float(
                cfg["co2_transport_storage_cost_gbp_per_tco2_mid"]
            ),
            "residual_emissions_carbon_cost_gbp_per_tco2": float(
                cfg["residual_emissions_carbon_cost_gbp_per_tco2"]
            ),
            "major_maintenance_interval_years_assumption": int(
                cfg["major_maintenance_interval_years_assumption"]
            ),
            "major_maintenance_cost_basis": str(cfg["major_maintenance_cost_basis"]).lower(),
            "major_maintenance_cost_gbp_per_plant_mid": float(
                cfg["major_maintenance_cost_gbp_per_plant_mid"]
            ),
            "major_maintenance_cost_gbp_per_mw_mid": float(
                cfg["major_maintenance_cost_gbp_per_mw_mid"]
            ),
            "major_maintenance_cost_gbp_per_gw_mid": float(
                cfg["major_maintenance_cost_gbp_per_gw_mid"]
            ),
            "maintenance_event_cost_gbp": projection["maintenance_event_cost_gbp"],
            "write_yearly_csv": bool(cfg["write_yearly_csv"]),
        },
        "selected_scenario_results": {
            "annual_base_components": projection["base_annual_components"],
            "annual_base_opex_excluding_major_maintenance_gbp": projection[
                "base_opex_no_maintenance_gbp"
            ],
            "total_opex_gbp": projection["total_opex_gbp"],
            "total_discounted_opex_gbp": projection["total_discounted_opex_gbp"],
            "capex_gbp": float(cfg["capital_cost_total_gbp_mid"]),
            "total_expenditure_gbp": projection["total_expenditure_gbp"],
            "total_discounted_expenditure_gbp": projection["total_discounted_expenditure_gbp"],
            "annual_average_opex_gbp": projection["annual_average_opex_gbp"],
            "lcoe_undiscounted_gbp_per_mwh": projection["lcoe_undiscounted_gbp_per_mwh"],
            "lcoe_discounted_gbp_per_mwh": projection["lcoe_discounted_gbp_per_mwh"],
        },
        "fuel_price_scenario_summaries": scenario_summary,
    }

    with summary_path.open("w") as f:
        json.dump(summary, f, indent=2)

    if cfg["write_yearly_csv"]:
        write_yearly_csv(yearly_csv_path, projection["rows"])

    print(f"Summary written: {summary_path}")
    if cfg["write_yearly_csv"]:
        print(f"Yearly table written: {yearly_csv_path}")
    print(f"Fuel scenario selected: {selected_scenario}")
    print(f"Fuel price (GBP/MWh_th): {selected_fuel_price:.2f}")
    print(f"CAPEX (GBP): {float(cfg['capital_cost_total_gbp_mid']):,.2f}")
    print(f"Total OPEX over {int(cfg['project_lifecycle_years'])} years (GBP): {projection['total_opex_gbp']:,.2f}")
    print(
        f"Total expenditure over {int(cfg['project_lifecycle_years'])} years (GBP): "
        f"{projection['total_expenditure_gbp']:,.2f}"
    )


if __name__ == "__main__":
    main()
