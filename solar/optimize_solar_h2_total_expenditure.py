#!/usr/bin/env python3
"""
Min-total-expenditure optimizer for solar + hydrogen system sizing.

Objective:
- Minimize total lifecycle expenditure = CAPEX + discounted OPEX over lifecycle.

Constraints:
- Repeated-year dispatch feasibility for at least lifecycle_years.
- No unmet load within tolerance.
- SOC within configured floor/ceiling bounds.
"""

import argparse
import csv
import json
import math
import sys
import time
from pathlib import Path


# Reuse the existing sizing/feasibility engine implemented in wind/.
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
WIND_DIR = ROOT_DIR / "wind"
if str(WIND_DIR) not in sys.path:
    sys.path.insert(0, str(WIND_DIR))

import hydrogen_storage_sizing as hs  # noqa: E402
import optimize_h2_capex as capex_opt  # noqa: E402


DEFAULTS = {
    "lifecycle_years": 25,
    "lifecycle_discount_rate": 0.0,
    "total_expenditure_output_prefix": "solar/solar_h2_total_expenditure_opt",
}


def load_config(path: Path):
    with path.open() as f:
        raw_cfg = json.load(f)
    if not isinstance(raw_cfg, dict):
        raise ValueError("Config file must be a JSON object.")

    # Start from existing H2 config loader/defaults.
    cfg = capex_opt.load_config(path)
    for key, value in DEFAULTS.items():
        cfg.setdefault(key, value)

    # Solar aliases take precedence if provided explicitly.
    if "capex_solar_per_mw" in raw_cfg:
        cfg["capex_wind_per_mw"] = raw_cfg["capex_solar_per_mw"]
    cfg["capex_solar_per_mw"] = cfg["capex_wind_per_mw"]

    if "solar_fixed_om_per_mw_year" in raw_cfg:
        cfg["wind_fixed_om_per_mw_year"] = raw_cfg["solar_fixed_om_per_mw_year"]
    cfg["solar_fixed_om_per_mw_year"] = cfg["wind_fixed_om_per_mw_year"]

    if "solar_stress_factor" in raw_cfg:
        cfg["wind_stress_factor"] = raw_cfg["solar_stress_factor"]
    cfg["solar_stress_factor"] = cfg["wind_stress_factor"]

    for suffix in ("min", "max", "step"):
        solar_key = f"optimize_solar_{suffix}_mw"
        wind_key = f"optimize_wind_{suffix}_mw"
        if solar_key in raw_cfg:
            cfg[wind_key] = raw_cfg[solar_key]
        cfg[solar_key] = cfg[wind_key]

    cfg.setdefault("optimize_output_prefix", "solar/solar_h2_capex_opt")
    return cfg


def validate(cfg):
    capex_opt.validate(cfg)
    if int(cfg["lifecycle_years"]) < 1:
        raise ValueError("lifecycle_years must be >= 1.")
    if float(cfg["lifecycle_discount_rate"]) < 0:
        raise ValueError("lifecycle_discount_rate must be >= 0.")


def discounted_opex_total(annual_opex, lifecycle_years, discount_rate):
    if discount_rate <= 0:
        return annual_opex * lifecycle_years
    factor = (1.0 - (1.0 + discount_rate) ** (-lifecycle_years)) / discount_rate
    return annual_opex * factor


def compute_capex(cfg, solar_mw, electrolyzer_mw, h2_turbine_mw, storage_mwh_h2):
    return (
        float(cfg["capex_solar_per_mw"]) * solar_mw
        + float(cfg["capex_electrolyzer_per_mw"]) * electrolyzer_mw
        + float(cfg["capex_h2_turbine_per_mw"]) * h2_turbine_mw
        + float(cfg["capex_storage_per_mwh_h2"]) * storage_mwh_h2
    )


def compute_annual_opex(
    cfg,
    solar_mw,
    electrolyzer_mw,
    h2_turbine_mw,
    storage_mwh_h2,
    assessment,
):
    h2_produced_kg = float(assessment["first_year_h2_produced_tonnes"]) * 1000.0

    breakdown = {
        "solar_fixed_om": float(cfg["solar_fixed_om_per_mw_year"]) * solar_mw,
        "electrolyzer_fixed_om": (
            float(cfg["electrolyzer_fixed_om_per_mw_year"]) * electrolyzer_mw
        ),
        "electrolyzer_variable_om": (
            float(cfg["electrolyzer_variable_om_per_mwh_in"])
            * float(assessment["first_year_electrolyzer_input_mwh"])
        ),
        "h2_turbine_fixed_om": float(cfg["h2_turbine_fixed_om_per_mw_year"]) * h2_turbine_mw,
        "h2_turbine_variable_om": (
            float(cfg["h2_turbine_variable_om_per_mwh_out"])
            * float(assessment["first_year_h2_turbine_output_mwh"])
        ),
        "storage_fixed_om": float(cfg["storage_om_per_mwh_h2_year"]) * storage_mwh_h2,
        "electrolyzer_stack_replacement_annualized": (
            float(cfg["electrolyzer_stack_replacement_cost_per_mw"])
            * electrolyzer_mw
            / float(cfg["electrolyzer_stack_replacement_interval_years"])
        ),
        "water": float(cfg["water_cost_per_kg_h2"]) * h2_produced_kg,
        "compression_and_purification": (
            float(cfg["compression_and_purification_cost_per_kg_h2"]) * h2_produced_kg
        ),
    }
    total = sum(breakdown.values())
    return {"annual_opex_total": total, "annual_opex_breakdown": breakdown}


def build_component_cost_table_rows(best, lifecycle_years, lifecycle_discount_rate):
    annual_opex = best["annual_opex_breakdown"]
    capex = best["capex_breakdown"]
    opex_factor = discounted_opex_total(1.0, lifecycle_years, lifecycle_discount_rate)

    annual_opex_by_component = {
        "solar": float(annual_opex.get("solar_fixed_om", 0.0)),
        "electrolyzer": (
            float(annual_opex.get("electrolyzer_fixed_om", 0.0))
            + float(annual_opex.get("electrolyzer_variable_om", 0.0))
            + float(annual_opex.get("electrolyzer_stack_replacement_annualized", 0.0))
            + float(annual_opex.get("water", 0.0))
            + float(annual_opex.get("compression_and_purification", 0.0))
        ),
        "h2_turbine": (
            float(annual_opex.get("h2_turbine_fixed_om", 0.0))
            + float(annual_opex.get("h2_turbine_variable_om", 0.0))
        ),
        "storage": float(annual_opex.get("storage_fixed_om", 0.0)),
    }

    rows = [
        {
            "design_component": "solar",
            "best_design_(MW/MWh-H2)": f"{best['solar_mw']:.2f} MW",
            "capex_for_best_design": float(capex.get("solar", 0.0)),
            "opex_over_25_years_for_best_design": annual_opex_by_component["solar"]
            * opex_factor,
        },
        {
            "design_component": "electrolyzer",
            "best_design_(MW/MWh-H2)": f"{best['electrolyzer_mw']:.2f} MW",
            "capex_for_best_design": float(capex.get("electrolyzer", 0.0)),
            "opex_over_25_years_for_best_design": annual_opex_by_component["electrolyzer"]
            * opex_factor,
        },
        {
            "design_component": "h2_turbine",
            "best_design_(MW/MWh-H2)": f"{best['h2_turbine_mw']:.2f} MW",
            "capex_for_best_design": float(capex.get("h2_turbine", 0.0)),
            "opex_over_25_years_for_best_design": annual_opex_by_component["h2_turbine"]
            * opex_factor,
        },
        {
            "design_component": "storage",
            "best_design_(MW/MWh-H2)": f"{best['storage_mwh_h2']:.2f} MWh-H2",
            "capex_for_best_design": float(capex.get("storage", 0.0)),
            "opex_over_25_years_for_best_design": annual_opex_by_component["storage"]
            * opex_factor,
        },
    ]

    rows.append(
        {
            "design_component": "TOTAL",
            "best_design_(MW/MWh-H2)": "",
            "capex_for_best_design": sum(float(r["capex_for_best_design"]) for r in rows),
            "opex_over_25_years_for_best_design": sum(
                float(r["opex_over_25_years_for_best_design"]) for r in rows
            ),
        }
    )
    return rows


def write_component_cost_table_csv(path, rows):
    fieldnames = [
        "design_component",
        "best_design_(MW/MWh-H2)",
        "capex_for_best_design",
        "opex_over_25_years_for_best_design",
    ]
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(
        description="Optimize solar+H2 system for minimum lifecycle CAPEX+OPEX."
    )
    parser.add_argument(
        "--config",
        default="solar/solar_config_midrange.json",
        help="Path to JSON config file (default: solar/solar_config_midrange.json).",
    )
    parser.add_argument(
        "--output-prefix",
        help=(
            "Optional output prefix override. Default uses "
            "total_expenditure_output_prefix from config."
        ),
    )
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    validate(cfg)

    lifecycle_years = int(cfg["lifecycle_years"])
    lifecycle_discount_rate = float(cfg["lifecycle_discount_rate"])
    configured_indefinite_years = int(cfg["indefinite_check_years"])
    feasibility_years_used = max(configured_indefinite_years, lifecycle_years)

    # Reuse the same feasibility solver but force the required lifecycle horizon.
    cfg_for_feasibility = dict(cfg)
    cfg_for_feasibility["indefinite_check_years"] = feasibility_years_used

    csv_path = Path(cfg["csv"])
    _, solar_raw_mw = hs.load_wind_series(csv_path)
    profile_capacity_mw = hs.extract_installed_capacity_mw_from_csv_metadata(csv_path)
    if profile_capacity_mw is None:
        if cfg.get("current_installed_capacity_mw") is None:
            raise ValueError(
                "CSV metadata capacity not found. Set current_installed_capacity_mw in config."
            )
        profile_capacity_mw = float(cfg["current_installed_capacity_mw"])

    solar_stress = float(cfg["solar_stress_factor"])
    demand_mw = float(cfg["demand_mw"])
    cavern_unit_mwh = hs.tonnes_h2_to_mwh_h2(
        float(cfg["uk_salt_cavern_working_capacity_tonnes_h2"])
    )

    solar_candidates = capex_opt.frange(
        float(cfg["optimize_solar_min_mw"]),
        float(cfg["optimize_solar_max_mw"]),
        float(cfg["optimize_solar_step_mw"]),
    )
    e_min_global = float(cfg["optimize_electrolyzer_min_mw"])
    e_max_global = float(cfg["optimize_electrolyzer_max_mw"])
    e_step = float(cfg["optimize_electrolyzer_step_mw"])

    best = None
    evaluated = 0
    feasible_points = 0

    t0 = time.time()
    for solar_mw in solar_candidates:
        solar_scale = (solar_mw / profile_capacity_mw) * solar_stress
        solar_profile = [w * solar_scale for w in solar_raw_mw]

        max_deficit = max(max(demand_mw - p, 0.0) for p in solar_profile)
        max_surplus = max(max(p - demand_mw, 0.0) for p in solar_profile)
        h2_turbine_mw = max_deficit

        e_max_local = min(e_max_global, max_surplus)
        if e_max_local < e_min_global:
            e_candidates = [e_min_global]
        else:
            e_candidates = capex_opt.frange(e_min_global, e_max_local, e_step)

        for electrolyzer_mw in e_candidates:
            evaluated += 1
            storage_mwh_h2, assessment = capex_opt.find_min_storage(
                wind_mw=solar_profile,
                cfg=cfg_for_feasibility,
                electrolyzer_mw=electrolyzer_mw,
                h2_turbine_mw=h2_turbine_mw,
            )
            if storage_mwh_h2 is None:
                continue

            feasible_points += 1
            capex_total = compute_capex(
                cfg=cfg,
                solar_mw=solar_mw,
                electrolyzer_mw=electrolyzer_mw,
                h2_turbine_mw=h2_turbine_mw,
                storage_mwh_h2=storage_mwh_h2,
            )
            annual_opex = compute_annual_opex(
                cfg=cfg,
                solar_mw=solar_mw,
                electrolyzer_mw=electrolyzer_mw,
                h2_turbine_mw=h2_turbine_mw,
                storage_mwh_h2=storage_mwh_h2,
                assessment=assessment,
            )
            annual_opex_total = float(annual_opex["annual_opex_total"])
            lifetime_opex_undiscounted = annual_opex_total * lifecycle_years
            lifetime_opex_discounted = discounted_opex_total(
                annual_opex=annual_opex_total,
                lifecycle_years=lifecycle_years,
                discount_rate=lifecycle_discount_rate,
            )

            objective_total_expenditure = capex_total + lifetime_opex_discounted

            if (best is None) or (objective_total_expenditure < best["objective_total_expenditure"]):
                best = {
                    "objective_total_expenditure": objective_total_expenditure,
                    "capex_total": capex_total,
                    "annual_opex_total": annual_opex_total,
                    "lifecycle_opex_undiscounted": lifetime_opex_undiscounted,
                    "lifecycle_opex_discounted": lifetime_opex_discounted,
                    "solar_mw": solar_mw,
                    "wind_mw": solar_mw,  # kept for compatibility with existing tooling
                    "electrolyzer_mw": electrolyzer_mw,
                    "h2_turbine_mw": h2_turbine_mw,
                    "storage_mwh_h2": storage_mwh_h2,
                    "storage_twh_h2": storage_mwh_h2 / 1_000_000.0,
                    "storage_tonnes_h2": hs.mwh_h2_to_tonnes_h2(storage_mwh_h2),
                    "uk_caverns": math.ceil(storage_mwh_h2 / cavern_unit_mwh),
                    "capex_breakdown": {
                        "solar": float(cfg["capex_solar_per_mw"]) * solar_mw,
                        "wind": float(cfg["capex_solar_per_mw"]) * solar_mw,
                        "electrolyzer": (
                            float(cfg["capex_electrolyzer_per_mw"]) * electrolyzer_mw
                        ),
                        "h2_turbine": float(cfg["capex_h2_turbine_per_mw"]) * h2_turbine_mw,
                        "storage": float(cfg["capex_storage_per_mwh_h2"]) * storage_mwh_h2,
                    },
                    "annual_opex_breakdown": annual_opex["annual_opex_breakdown"],
                    "simulation_metrics": {
                        "indefinite_feasible": assessment["indefinite_feasible"],
                        "years_requested": assessment["years_requested"],
                        "years_simulated": assessment["years_simulated"],
                        "converged": assessment["converged"],
                        "converged_year": assessment["converged_year"],
                        "all_years_meet_load": assessment["all_years_meet_load"],
                        "all_years_meet_min_end_soc": assessment["all_years_meet_min_end_soc"],
                        "max_abs_yearly_soc_drift_mwh": assessment[
                            "max_abs_yearly_soc_drift_mwh"
                        ],
                        "start_soc_mwh": assessment["start_soc_mwh"],
                        "start_soc_pct": assessment["start_soc_pct"],
                        "final_soc_mwh": assessment["final_soc_mwh"],
                        "final_soc_pct": assessment["final_soc_pct"],
                        "first_year_unmet_electric_mwh": assessment[
                            "first_year_unmet_electric_mwh"
                        ],
                        "first_year_min_soc_pct": assessment["first_year_min_soc_pct"],
                        "first_year_max_soc_pct": assessment["first_year_max_soc_pct"],
                        "first_year_electrolyzer_input_mwh": assessment[
                            "first_year_electrolyzer_input_mwh"
                        ],
                        "first_year_h2_charge_mwh": assessment["first_year_h2_charge_mwh"],
                        "first_year_h2_discharge_mwh": assessment["first_year_h2_discharge_mwh"],
                        "first_year_h2_turbine_output_mwh": assessment[
                            "first_year_h2_turbine_output_mwh"
                        ],
                        "first_year_h2_produced_tonnes": assessment[
                            "first_year_h2_produced_tonnes"
                        ],
                        "first_year_h2_dispatched_tonnes": assessment[
                            "first_year_h2_dispatched_tonnes"
                        ],
                        "first_year_curtailed_surplus_electric_mwh": assessment[
                            "first_year_curtailed_surplus_electric_mwh"
                        ],
                    },
                    "effective_solar_profile_scale": solar_scale,
                    "effective_wind_profile_scale": solar_scale,
                }

    elapsed = time.time() - t0
    output_prefix = args.output_prefix or cfg["total_expenditure_output_prefix"]
    out_path = Path(f"{output_prefix}_summary.json")
    component_table_path = Path(f"{output_prefix}_component_cost_table.csv")

    output = {
        "inputs": {
            "config_file": str(args.config),
            "csv": str(csv_path),
            "profile_installed_capacity_mw": profile_capacity_mw,
            "demand_mw": demand_mw,
            "eta_charge": float(cfg["electricity_to_hydrogen_efficiency"]),
            "eta_discharge": float(cfg["hydrogen_to_electricity_efficiency"]),
            "soc_floor_pct": float(cfg["soc_floor_pct"]),
            "soc_ceiling_pct": float(cfg["soc_ceiling_pct"]),
            "start_fullness_pct": float(cfg["start_fullness_pct"]),
            "min_end_soc_mwh": float(cfg["min_end_soc_mwh"]),
            "solar_stress_factor": solar_stress,
            "wind_stress_factor": float(cfg["wind_stress_factor"]),
            "capex_currency": cfg["capex_currency"],
            "lifecycle_years": lifecycle_years,
            "lifecycle_discount_rate": lifecycle_discount_rate,
            "configured_indefinite_check_years": configured_indefinite_years,
            "feasibility_years_used": feasibility_years_used,
            "capex_solar_per_mw": float(cfg["capex_solar_per_mw"]),
            "capex_wind_per_mw": float(cfg["capex_wind_per_mw"]),
            "capex_electrolyzer_per_mw": float(cfg["capex_electrolyzer_per_mw"]),
            "capex_h2_turbine_per_mw": float(cfg["capex_h2_turbine_per_mw"]),
            "capex_storage_per_mwh_h2": float(cfg["capex_storage_per_mwh_h2"]),
            "solar_fixed_om_per_mw_year": float(cfg["solar_fixed_om_per_mw_year"]),
            "wind_fixed_om_per_mw_year": float(cfg["wind_fixed_om_per_mw_year"]),
            "electrolyzer_fixed_om_per_mw_year": float(
                cfg["electrolyzer_fixed_om_per_mw_year"]
            ),
            "electrolyzer_variable_om_per_mwh_in": float(
                cfg["electrolyzer_variable_om_per_mwh_in"]
            ),
            "h2_turbine_fixed_om_per_mw_year": float(cfg["h2_turbine_fixed_om_per_mw_year"]),
            "h2_turbine_variable_om_per_mwh_out": float(
                cfg["h2_turbine_variable_om_per_mwh_out"]
            ),
            "storage_om_per_mwh_h2_year": float(cfg["storage_om_per_mwh_h2_year"]),
            "electrolyzer_stack_replacement_cost_per_mw": float(
                cfg["electrolyzer_stack_replacement_cost_per_mw"]
            ),
            "electrolyzer_stack_replacement_interval_years": float(
                cfg["electrolyzer_stack_replacement_interval_years"]
            ),
            "water_cost_per_kg_h2": float(cfg["water_cost_per_kg_h2"]),
            "compression_and_purification_cost_per_kg_h2": float(
                cfg["compression_and_purification_cost_per_kg_h2"]
            ),
            "optimize_solar_min_mw": float(cfg["optimize_solar_min_mw"]),
            "optimize_solar_max_mw": float(cfg["optimize_solar_max_mw"]),
            "optimize_solar_step_mw": float(cfg["optimize_solar_step_mw"]),
            "optimize_electrolyzer_min_mw": float(cfg["optimize_electrolyzer_min_mw"]),
            "optimize_electrolyzer_max_mw": float(cfg["optimize_electrolyzer_max_mw"]),
            "optimize_electrolyzer_step_mw": float(cfg["optimize_electrolyzer_step_mw"]),
            "optimize_storage_min_mwh_h2": float(cfg["optimize_storage_min_mwh_h2"]),
            "optimize_storage_max_mwh_h2": float(cfg["optimize_storage_max_mwh_h2"]),
            "optimize_storage_binary_tolerance_mwh_h2": float(
                cfg["optimize_storage_binary_tolerance_mwh_h2"]
            ),
            "optimize_storage_binary_iterations": int(cfg["optimize_storage_binary_iterations"]),
            "optimize_enforce_integer_caverns": bool(cfg["optimize_enforce_integer_caverns"]),
            "indefinite_soc_convergence_tol_mwh": float(
                cfg["indefinite_soc_convergence_tol_mwh"]
            ),
            "indefinite_unmet_tolerance_mwh": float(cfg["indefinite_unmet_tolerance_mwh"]),
        },
        "search_stats": {
            "solar_candidates": len(solar_candidates),
            "evaluated_points": evaluated,
            "feasible_points": feasible_points,
            "elapsed_seconds": elapsed,
        },
        "best_design": best,
    }

    with out_path.open("w") as f:
        json.dump(output, f, indent=2)

    if best is not None:
        component_rows = build_component_cost_table_rows(
            best=best,
            lifecycle_years=lifecycle_years,
            lifecycle_discount_rate=lifecycle_discount_rate,
        )
        write_component_cost_table_csv(component_table_path, component_rows)

    print(f"Optimization summary written: {out_path}")
    if best is not None:
        print(f"Component cost table written: {component_table_path}")
    print(f"Evaluated points: {evaluated}")
    print(f"Feasible points: {feasible_points}")
    print(f"Elapsed seconds: {elapsed:.2f}")
    print(f"Feasibility years used: {feasibility_years_used}")

    if best is None:
        print("No feasible design found in configured search bounds.")
    else:
        currency = cfg["capex_currency"]
        print(
            f"Best objective total expenditure ({currency}, discounted OPEX basis): "
            f"{best['objective_total_expenditure']:.2f}"
        )
        print(f"Best CAPEX ({currency}): {best['capex_total']:.2f}")
        print(f"Best annual OPEX ({currency}/year): {best['annual_opex_total']:.2f}")
        print(
            f"Best lifecycle OPEX ({currency}, discounted): "
            f"{best['lifecycle_opex_discounted']:.2f}"
        )
        print(
            "Best design (MW/MWh-H2): "
            f"solar={best['solar_mw']:.2f}, "
            f"electrolyzer={best['electrolyzer_mw']:.2f}, "
            f"h2_turbine={best['h2_turbine_mw']:.2f}, "
            f"storage={best['storage_mwh_h2']:.2f}"
        )


if __name__ == "__main__":
    main()
