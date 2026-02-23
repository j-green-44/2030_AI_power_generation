#!/usr/bin/env python3
"""
Min-CAPEX optimizer for wind + hydrogen system sizing.

Decision variables:
- wind installed capacity (MW)
- electrolyzer capacity (MW electric input)
- H2 turbine capacity (MW electric output)
- H2 storage capacity (MWh-H2)

Constraints per hourly simulation:
- no unmet load (within tolerance)
- SOC within configured floor/ceiling bounds
- repeated-year operation is feasible under configured convergence tolerance
"""

import argparse
import json
import math
import time
from pathlib import Path

import hydrogen_storage_sizing as hs


DEFAULTS = {
    "capex_currency": "GBP_2026_placeholder",
    "capex_wind_per_mw": 2_500_000.0,
    "capex_electrolyzer_per_mw": 900_000.0,
    "capex_h2_turbine_per_mw": 1_100_000.0,
    "capex_storage_per_mwh_h2": 20_000.0,
    "optimize_wind_min_mw": 16_000.0,
    "optimize_wind_max_mw": 50_000.0,
    "optimize_wind_step_mw": 2_000.0,
    "optimize_electrolyzer_min_mw": 0.0,
    "optimize_electrolyzer_max_mw": 30_000.0,
    "optimize_electrolyzer_step_mw": 2_000.0,
    "optimize_storage_min_mwh_h2": 100_000.0,
    "optimize_storage_max_mwh_h2": 60_000_000.0,
    "optimize_storage_binary_tolerance_mwh_h2": 10_000.0,
    "optimize_storage_binary_iterations": 40,
    "optimize_enforce_integer_caverns": False,
    "optimize_tolerance_unmet_mwh": 1e-6,
    "indefinite_check_years": 20,
    "indefinite_soc_convergence_tol_mwh": 1000.0,
    "indefinite_unmet_tolerance_mwh": 1e-6,
    # Optional additional constraint: require end SOC to be non-depleting
    # relative to initial SOC over the configured repeated-year horizon.
    "require_h2_cyclic_non_depleting": False,
    "h2_cyclic_tolerance_mwh": 1.0,
    # Annual OPEX assumptions (same currency basis as CAPEX inputs).
    "wind_fixed_om_per_mw_year": 0.0,
    "electrolyzer_fixed_om_per_mw_year": 0.0,
    "electrolyzer_variable_om_per_mwh_in": 0.0,
    "h2_turbine_fixed_om_per_mw_year": 0.0,
    "h2_turbine_variable_om_per_mwh_out": 0.0,
    "storage_om_per_mwh_h2_year": 0.0,
    "electrolyzer_stack_replacement_cost_per_mw": 0.0,
    "electrolyzer_stack_replacement_interval_years": 1.0,
    "water_cost_per_kg_h2": 0.0,
    "compression_and_purification_cost_per_kg_h2": 0.0,
    "optimize_output_prefix": "h2_capex_opt",
}


def load_config(path: Path):
    with path.open() as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError("Config file must be a JSON object.")
    merged = DEFAULTS.copy()
    merged.update(cfg)
    alias_map = {
        "wind_fixed_O&M": "wind_fixed_om_per_mw_year",
        "electrolyser_fixed_O&M": "electrolyzer_fixed_om_per_mw_year",
        "electrolyser_variable_O&M": "electrolyzer_variable_om_per_mwh_in",
        "H2_turbine_fixed_O&M": "h2_turbine_fixed_om_per_mw_year",
        "H2_turbine_variable_O&M": "h2_turbine_variable_om_per_mwh_out",
        "storage_O&M_per_MWhH2_year": "storage_om_per_mwh_h2_year",
        "electrolyser_stack_replacement_cost": "electrolyzer_stack_replacement_cost_per_mw",
        "electrolyser_stack_replacement_interval": "electrolyzer_stack_replacement_interval_years",
        "water_cost_per_kgH2": "water_cost_per_kg_h2",
        "compression_and_purification_per_kgH2": (
            "compression_and_purification_cost_per_kg_h2"
        ),
    }
    for alias, canonical in alias_map.items():
        if canonical not in cfg and alias in cfg:
            merged[canonical] = cfg[alias]
    if (
        "indefinite_unmet_tolerance_mwh" not in cfg
        and "optimize_tolerance_unmet_mwh" in cfg
    ):
        merged["indefinite_unmet_tolerance_mwh"] = cfg["optimize_tolerance_unmet_mwh"]
    return merged


def frange(start, stop, step):
    if step <= 0:
        raise ValueError("Range step must be > 0.")
    values = []
    x = float(start)
    stop = float(stop)
    while x <= stop + 1e-9:
        values.append(float(f"{x:.10f}"))
        x += step
    if not values or values[-1] < stop - 1e-9:
        values.append(stop)
    return values


def validate(cfg):
    numeric_positive = [
        "demand_mw",
        "electricity_to_hydrogen_efficiency",
        "hydrogen_to_electricity_efficiency",
        "wind_stress_factor",
        "capex_wind_per_mw",
        "capex_electrolyzer_per_mw",
        "capex_h2_turbine_per_mw",
        "capex_storage_per_mwh_h2",
        "optimize_wind_step_mw",
        "optimize_electrolyzer_step_mw",
        "optimize_storage_min_mwh_h2",
        "optimize_storage_max_mwh_h2",
        "optimize_storage_binary_tolerance_mwh_h2",
        "optimize_storage_binary_iterations",
        "uk_salt_cavern_working_capacity_tonnes_h2",
    ]
    for key in numeric_positive:
        if float(cfg[key]) <= 0:
            raise ValueError(f"{key} must be > 0.")

    if not (0 < float(cfg["electricity_to_hydrogen_efficiency"]) <= 1):
        raise ValueError("electricity_to_hydrogen_efficiency must be in (0, 1].")
    if not (0 < float(cfg["hydrogen_to_electricity_efficiency"]) <= 1):
        raise ValueError("hydrogen_to_electricity_efficiency must be in (0, 1].")

    if float(cfg["optimize_wind_min_mw"]) > float(cfg["optimize_wind_max_mw"]):
        raise ValueError("optimize_wind_min_mw must be <= optimize_wind_max_mw.")
    if float(cfg["optimize_electrolyzer_min_mw"]) > float(cfg["optimize_electrolyzer_max_mw"]):
        raise ValueError(
            "optimize_electrolyzer_min_mw must be <= optimize_electrolyzer_max_mw."
        )
    if float(cfg["optimize_storage_min_mwh_h2"]) > float(
        cfg["optimize_storage_max_mwh_h2"]
    ):
        raise ValueError(
            "optimize_storage_min_mwh_h2 must be <= optimize_storage_max_mwh_h2."
        )

    floor_pct = float(cfg["soc_floor_pct"])
    ceil_pct = float(cfg["soc_ceiling_pct"])
    start_pct = float(cfg["start_fullness_pct"])
    if floor_pct < 0 or floor_pct > 100:
        raise ValueError("soc_floor_pct must be in [0, 100].")
    if ceil_pct < 0 or ceil_pct > 100:
        raise ValueError("soc_ceiling_pct must be in [0, 100].")
    if floor_pct >= ceil_pct:
        raise ValueError("soc_floor_pct must be < soc_ceiling_pct.")
    if start_pct < floor_pct or start_pct > ceil_pct:
        raise ValueError("start_fullness_pct must be within [soc_floor_pct, soc_ceiling_pct].")
    if int(cfg["indefinite_check_years"]) < 1:
        raise ValueError("indefinite_check_years must be >= 1.")
    if float(cfg["indefinite_soc_convergence_tol_mwh"]) < 0:
        raise ValueError("indefinite_soc_convergence_tol_mwh must be >= 0.")
    if float(cfg["indefinite_unmet_tolerance_mwh"]) < 0:
        raise ValueError("indefinite_unmet_tolerance_mwh must be >= 0.")
    if float(cfg["h2_cyclic_tolerance_mwh"]) < 0:
        raise ValueError("h2_cyclic_tolerance_mwh must be >= 0.")
    non_negative_opex = [
        "wind_fixed_om_per_mw_year",
        "electrolyzer_fixed_om_per_mw_year",
        "electrolyzer_variable_om_per_mwh_in",
        "h2_turbine_fixed_om_per_mw_year",
        "h2_turbine_variable_om_per_mwh_out",
        "storage_om_per_mwh_h2_year",
        "electrolyzer_stack_replacement_cost_per_mw",
        "water_cost_per_kg_h2",
        "compression_and_purification_cost_per_kg_h2",
    ]
    for key in non_negative_opex:
        if float(cfg[key]) < 0:
            raise ValueError(f"{key} must be >= 0.")
    if float(cfg["electrolyzer_stack_replacement_interval_years"]) <= 0:
        raise ValueError("electrolyzer_stack_replacement_interval_years must be > 0.")


def simulate_dispatch(
    wind_mw,
    demand_mw,
    eta_charge,
    eta_discharge,
    electrolyzer_mw,
    h2_turbine_mw,
    storage_capacity_mwh_h2,
    start_fullness_pct,
    soc_floor_pct,
    soc_ceiling_pct,
):
    soc_floor = storage_capacity_mwh_h2 * soc_floor_pct / 100.0
    soc_ceiling = storage_capacity_mwh_h2 * soc_ceiling_pct / 100.0
    start_soc = storage_capacity_mwh_h2 * start_fullness_pct / 100.0
    soc = start_soc

    unmet_mwh = 0.0
    curtailed_surplus_mwh = 0.0
    electrolyzer_input_mwh = 0.0
    h2_charge_mwh = 0.0
    h2_discharge_mwh = 0.0
    h2_turbine_output_mwh = 0.0
    min_soc = soc
    max_soc = soc

    for w in wind_mw:
        direct = min(w, demand_mw)
        surplus = max(w - direct, 0.0)
        deficit = max(demand_mw - direct, 0.0)

        charge_electric_mwh = min(surplus, electrolyzer_mw)
        charge_room_electric_mwh = max((soc_ceiling - soc) / eta_charge, 0.0)
        charge_electric_mwh = min(charge_electric_mwh, charge_room_electric_mwh)
        charge_h2_mwh = charge_electric_mwh * eta_charge
        soc += charge_h2_mwh
        electrolyzer_input_mwh += charge_electric_mwh
        h2_charge_mwh += charge_h2_mwh
        curtailed_surplus_mwh += max(surplus - charge_electric_mwh, 0.0)

        turbine_output_mwh = min(deficit, h2_turbine_mw)
        turbine_output_by_soc = max(soc - soc_floor, 0.0) * eta_discharge
        turbine_output_mwh = min(turbine_output_mwh, turbine_output_by_soc)
        discharge_h2_mwh = turbine_output_mwh / eta_discharge
        soc -= discharge_h2_mwh
        h2_turbine_output_mwh += turbine_output_mwh
        h2_discharge_mwh += discharge_h2_mwh
        unmet_mwh += max(deficit - turbine_output_mwh, 0.0)

        min_soc = min(min_soc, soc)
        max_soc = max(max_soc, soc)

    end_soc = soc
    return {
        "unmet_electric_mwh": unmet_mwh,
        "curtailed_surplus_electric_mwh": curtailed_surplus_mwh,
        "electrolyzer_input_mwh": electrolyzer_input_mwh,
        "h2_charge_mwh": h2_charge_mwh,
        "h2_discharge_mwh": h2_discharge_mwh,
        "h2_turbine_output_mwh": h2_turbine_output_mwh,
        "h2_produced_tonnes": hs.mwh_h2_to_tonnes_h2(h2_charge_mwh),
        "h2_dispatched_tonnes": hs.mwh_h2_to_tonnes_h2(h2_discharge_mwh),
        "start_soc_mwh": start_soc,
        "end_soc_mwh": end_soc,
        "min_soc_mwh": min_soc,
        "max_soc_mwh": max_soc,
        "min_soc_pct": (min_soc / storage_capacity_mwh_h2) * 100.0,
        "max_soc_pct": (max_soc / storage_capacity_mwh_h2) * 100.0,
        "end_soc_pct": (end_soc / storage_capacity_mwh_h2) * 100.0,
    }


def assess_indefinite_operation(
    wind_mw,
    demand_mw,
    eta_charge,
    eta_discharge,
    electrolyzer_mw,
    h2_turbine_mw,
    storage_capacity_mwh_h2,
    start_fullness_pct,
    soc_floor_pct,
    soc_ceiling_pct,
    min_end_soc_mwh,
    indefinite_check_years,
    indefinite_soc_convergence_tol_mwh,
    indefinite_unmet_tolerance_mwh,
):
    yearly = []
    start_soc_mwh = storage_capacity_mwh_h2 * start_fullness_pct / 100.0
    year_start_soc_mwh = start_soc_mwh
    previous_end_soc_mwh = None
    converged_year = None
    all_years_meet_load = True
    all_years_meet_min_end = True
    max_abs_drift_mwh = 0.0
    first_year_dispatch = None

    for year in range(1, indefinite_check_years + 1):
        year_start_pct = (year_start_soc_mwh / storage_capacity_mwh_h2) * 100.0
        sim_year = simulate_dispatch(
            wind_mw=wind_mw,
            demand_mw=demand_mw,
            eta_charge=eta_charge,
            eta_discharge=eta_discharge,
            electrolyzer_mw=electrolyzer_mw,
            h2_turbine_mw=h2_turbine_mw,
            storage_capacity_mwh_h2=storage_capacity_mwh_h2,
            start_fullness_pct=year_start_pct,
            soc_floor_pct=soc_floor_pct,
            soc_ceiling_pct=soc_ceiling_pct,
        )
        if first_year_dispatch is None:
            first_year_dispatch = sim_year

        year_end_soc_mwh = sim_year["end_soc_mwh"]
        delta_soc_mwh = year_end_soc_mwh - year_start_soc_mwh
        max_abs_drift_mwh = max(max_abs_drift_mwh, abs(delta_soc_mwh))

        meets_load = sim_year["unmet_electric_mwh"] <= indefinite_unmet_tolerance_mwh
        meets_min_end = year_end_soc_mwh >= float(min_end_soc_mwh) - 1e-6
        all_years_meet_load = all_years_meet_load and meets_load
        all_years_meet_min_end = all_years_meet_min_end and meets_min_end

        if previous_end_soc_mwh is not None and converged_year is None:
            if (
                abs(year_end_soc_mwh - previous_end_soc_mwh)
                <= indefinite_soc_convergence_tol_mwh
            ):
                converged_year = year

        yearly.append(
            {
                "year": year,
                "start_soc_mwh": year_start_soc_mwh,
                "end_soc_mwh": year_end_soc_mwh,
                "delta_soc_mwh": delta_soc_mwh,
                "unmet_electric_mwh": sim_year["unmet_electric_mwh"],
                "min_soc_pct": sim_year["min_soc_pct"],
                "max_soc_pct": sim_year["max_soc_pct"],
                "meets_load": meets_load,
                "meets_min_end_soc": meets_min_end,
            }
        )

        if not meets_load:
            break

        previous_end_soc_mwh = year_end_soc_mwh
        year_start_soc_mwh = year_end_soc_mwh

    years_simulated = len(yearly)
    converged = converged_year is not None
    final_soc_mwh = yearly[-1]["end_soc_mwh"] if yearly else start_soc_mwh
    final_soc_pct = (final_soc_mwh / storage_capacity_mwh_h2) * 100.0
    first_year = yearly[0] if yearly else None

    indefinite_feasible = (
        all_years_meet_load
        and all_years_meet_min_end
        and converged
        and years_simulated >= indefinite_check_years
    )

    return {
        "indefinite_feasible": indefinite_feasible,
        "years_requested": indefinite_check_years,
        "years_simulated": years_simulated,
        "converged": converged,
        "converged_year": converged_year,
        "all_years_meet_load": all_years_meet_load,
        "all_years_meet_min_end_soc": all_years_meet_min_end,
        "max_abs_yearly_soc_drift_mwh": max_abs_drift_mwh,
        "start_soc_mwh": start_soc_mwh,
        "start_soc_pct": start_fullness_pct,
        "final_soc_mwh": final_soc_mwh,
        "final_soc_pct": final_soc_pct,
        "first_year_unmet_electric_mwh": first_year["unmet_electric_mwh"] if first_year else 0.0,
        "first_year_min_soc_pct": first_year["min_soc_pct"] if first_year else start_fullness_pct,
        "first_year_max_soc_pct": first_year["max_soc_pct"] if first_year else start_fullness_pct,
        "first_year_electrolyzer_input_mwh": (
            first_year_dispatch["electrolyzer_input_mwh"] if first_year_dispatch else 0.0
        ),
        "first_year_h2_charge_mwh": (
            first_year_dispatch["h2_charge_mwh"] if first_year_dispatch else 0.0
        ),
        "first_year_h2_discharge_mwh": (
            first_year_dispatch["h2_discharge_mwh"] if first_year_dispatch else 0.0
        ),
        "first_year_h2_turbine_output_mwh": (
            first_year_dispatch["h2_turbine_output_mwh"] if first_year_dispatch else 0.0
        ),
        "first_year_h2_produced_tonnes": (
            first_year_dispatch["h2_produced_tonnes"] if first_year_dispatch else 0.0
        ),
        "first_year_h2_dispatched_tonnes": (
            first_year_dispatch["h2_dispatched_tonnes"] if first_year_dispatch else 0.0
        ),
        "first_year_curtailed_surplus_electric_mwh": (
            first_year_dispatch["curtailed_surplus_electric_mwh"] if first_year_dispatch else 0.0
        ),
        "yearly_results": yearly,
    }


def find_min_storage(
    wind_mw,
    cfg,
    electrolyzer_mw,
    h2_turbine_mw,
):
    demand_mw = float(cfg["demand_mw"])
    eta_charge = float(cfg["electricity_to_hydrogen_efficiency"])
    eta_discharge = float(cfg["hydrogen_to_electricity_efficiency"])
    start_pct = float(cfg["start_fullness_pct"])
    floor_pct = float(cfg["soc_floor_pct"])
    ceiling_pct = float(cfg["soc_ceiling_pct"])
    min_end_soc_mwh = float(cfg["min_end_soc_mwh"])
    tol_unmet_mwh = float(cfg["indefinite_unmet_tolerance_mwh"])
    indefinite_check_years = int(cfg["indefinite_check_years"])
    indefinite_soc_convergence_tol_mwh = float(cfg["indefinite_soc_convergence_tol_mwh"])
    require_h2_cyclic_non_depleting = bool(cfg["require_h2_cyclic_non_depleting"])
    h2_cyclic_tolerance_mwh = float(cfg["h2_cyclic_tolerance_mwh"])
    s_min = float(cfg["optimize_storage_min_mwh_h2"])
    s_max = float(cfg["optimize_storage_max_mwh_h2"])
    iters = int(cfg["optimize_storage_binary_iterations"])
    tol_storage = float(cfg["optimize_storage_binary_tolerance_mwh_h2"])
    enforce_integer = bool(cfg["optimize_enforce_integer_caverns"])
    cavern_unit_mwh = hs.tonnes_h2_to_mwh_h2(float(cfg["uk_salt_cavern_working_capacity_tonnes_h2"]))

    def feasible_for_storage(storage_mwh_h2):
        assessment = assess_indefinite_operation(
            wind_mw=wind_mw,
            demand_mw=demand_mw,
            eta_charge=eta_charge,
            eta_discharge=eta_discharge,
            electrolyzer_mw=electrolyzer_mw,
            h2_turbine_mw=h2_turbine_mw,
            storage_capacity_mwh_h2=storage_mwh_h2,
            start_fullness_pct=start_pct,
            soc_floor_pct=floor_pct,
            soc_ceiling_pct=ceiling_pct,
            min_end_soc_mwh=min_end_soc_mwh,
            indefinite_check_years=indefinite_check_years,
            indefinite_soc_convergence_tol_mwh=indefinite_soc_convergence_tol_mwh,
            indefinite_unmet_tolerance_mwh=tol_unmet_mwh,
        )
        cyclic_ok = (
            (not require_h2_cyclic_non_depleting)
            or (
                assessment["final_soc_mwh"] + h2_cyclic_tolerance_mwh
                >= assessment["start_soc_mwh"]
            )
        )
        return assessment["indefinite_feasible"] and cyclic_ok, assessment

    ok_max, sim_max = feasible_for_storage(s_max)
    if not ok_max:
        return None, sim_max

    if enforce_integer:
        n_min = max(1, math.ceil(s_min / cavern_unit_mwh))
        n_max = max(n_min, math.ceil(s_max / cavern_unit_mwh))

        def feasible_n(n):
            return feasible_for_storage(n * cavern_unit_mwh)

        ok_nmin, sim_nmin = feasible_n(n_min)
        if ok_nmin:
            return n_min * cavern_unit_mwh, sim_nmin

        lo, hi = n_min, n_max
        while lo + 1 < hi:
            mid = (lo + hi) // 2
            ok_mid, _ = feasible_n(mid)
            if ok_mid:
                hi = mid
            else:
                lo = mid
        ok_hi, sim_hi = feasible_n(hi)
        if ok_hi:
            return hi * cavern_unit_mwh, sim_hi
        return None, sim_hi

    ok_min, sim_min = feasible_for_storage(s_min)
    if ok_min:
        return s_min, sim_min

    lo, hi = s_min, s_max
    best_sim = sim_max
    for _ in range(iters):
        if (hi - lo) <= tol_storage:
            break
        mid = 0.5 * (lo + hi)
        ok_mid, sim_mid = feasible_for_storage(mid)
        if ok_mid:
            hi = mid
            best_sim = sim_mid
        else:
            lo = mid

    ok_hi, sim_hi = feasible_for_storage(hi)
    if ok_hi:
        return hi, sim_hi
    return None, best_sim


def compute_capex(cfg, wind_mw, electrolyzer_mw, h2_turbine_mw, storage_mwh_h2):
    return (
        float(cfg["capex_wind_per_mw"]) * wind_mw
        + float(cfg["capex_electrolyzer_per_mw"]) * electrolyzer_mw
        + float(cfg["capex_h2_turbine_per_mw"]) * h2_turbine_mw
        + float(cfg["capex_storage_per_mwh_h2"]) * storage_mwh_h2
    )


def compute_annual_opex(cfg, wind_mw, electrolyzer_mw, h2_turbine_mw, storage_mwh_h2, assessment):
    h2_produced_kg = float(assessment["first_year_h2_produced_tonnes"]) * 1000.0

    breakdown = {
        "wind_fixed_om": float(cfg["wind_fixed_om_per_mw_year"]) * wind_mw,
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


def main():
    parser = argparse.ArgumentParser(description="Optimize wind+H2 system for minimum CAPEX.")
    parser.add_argument(
        "--config",
        default="hydrogen_storage_config.json",
        help="Path to JSON config file (default: hydrogen_storage_config.json).",
    )
    parser.add_argument(
        "--output-prefix",
        help="Optional output prefix override. Default uses optimize_output_prefix from config.",
    )
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    validate(cfg)

    csv_path = Path(cfg["csv"])
    times, wind_raw_mw = hs.load_wind_series(csv_path)
    profile_capacity_mw = hs.extract_installed_capacity_mw_from_csv_metadata(csv_path)
    if profile_capacity_mw is None:
        if cfg.get("current_installed_capacity_mw") is None:
            raise ValueError(
                "CSV metadata capacity not found. Set current_installed_capacity_mw in config."
            )
        profile_capacity_mw = float(cfg["current_installed_capacity_mw"])

    wind_stress = float(cfg["wind_stress_factor"])
    demand_mw = float(cfg["demand_mw"])
    cavern_unit_mwh = hs.tonnes_h2_to_mwh_h2(float(cfg["uk_salt_cavern_working_capacity_tonnes_h2"]))

    wind_candidates = frange(
        float(cfg["optimize_wind_min_mw"]),
        float(cfg["optimize_wind_max_mw"]),
        float(cfg["optimize_wind_step_mw"]),
    )
    e_min_global = float(cfg["optimize_electrolyzer_min_mw"])
    e_max_global = float(cfg["optimize_electrolyzer_max_mw"])
    e_step = float(cfg["optimize_electrolyzer_step_mw"])

    best = None
    evaluated = 0
    feasible_points = 0

    t0 = time.time()
    for wind_mw in wind_candidates:
        wind_scale = (wind_mw / profile_capacity_mw) * wind_stress
        wind_profile = [w * wind_scale for w in wind_raw_mw]

        max_deficit = max(max(demand_mw - w, 0.0) for w in wind_profile)
        max_surplus = max(max(w - demand_mw, 0.0) for w in wind_profile)
        h2_turbine_mw = max_deficit  # cost-optimal lower bound with no unmet-load target

        e_max_local = min(e_max_global, max_surplus)
        if e_max_local < e_min_global:
            e_candidates = [e_min_global]
        else:
            e_candidates = frange(e_min_global, e_max_local, e_step)

        for electrolyzer_mw in e_candidates:
            evaluated += 1
            storage_mwh_h2, assessment = find_min_storage(
                wind_mw=wind_profile,
                cfg=cfg,
                electrolyzer_mw=electrolyzer_mw,
                h2_turbine_mw=h2_turbine_mw,
            )
            if storage_mwh_h2 is None:
                continue

            feasible_points += 1
            total_capex = compute_capex(
                cfg=cfg,
                wind_mw=wind_mw,
                electrolyzer_mw=electrolyzer_mw,
                h2_turbine_mw=h2_turbine_mw,
                storage_mwh_h2=storage_mwh_h2,
            )
            annual_opex = compute_annual_opex(
                cfg=cfg,
                wind_mw=wind_mw,
                electrolyzer_mw=electrolyzer_mw,
                h2_turbine_mw=h2_turbine_mw,
                storage_mwh_h2=storage_mwh_h2,
                assessment=assessment,
            )

            if (best is None) or (total_capex < best["capex_total"]):
                best = {
                    "capex_total": total_capex,
                    "annual_opex_total": annual_opex["annual_opex_total"],
                    "wind_mw": wind_mw,
                    "electrolyzer_mw": electrolyzer_mw,
                    "h2_turbine_mw": h2_turbine_mw,
                    "storage_mwh_h2": storage_mwh_h2,
                    "storage_twh_h2": storage_mwh_h2 / 1_000_000.0,
                    "storage_tonnes_h2": hs.mwh_h2_to_tonnes_h2(storage_mwh_h2),
                    "uk_caverns": math.ceil(storage_mwh_h2 / cavern_unit_mwh),
                    "capex_breakdown": {
                        "wind": float(cfg["capex_wind_per_mw"]) * wind_mw,
                        "electrolyzer": float(cfg["capex_electrolyzer_per_mw"]) * electrolyzer_mw,
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
                    "effective_wind_profile_scale": wind_scale,
                }

    elapsed = time.time() - t0
    output_prefix = args.output_prefix or cfg["optimize_output_prefix"]
    out_path = Path(f"{output_prefix}_summary.json")

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
            "wind_stress_factor": wind_stress,
            "capex_currency": cfg["capex_currency"],
            "capex_wind_per_mw": float(cfg["capex_wind_per_mw"]),
            "capex_electrolyzer_per_mw": float(cfg["capex_electrolyzer_per_mw"]),
            "capex_h2_turbine_per_mw": float(cfg["capex_h2_turbine_per_mw"]),
            "capex_storage_per_mwh_h2": float(cfg["capex_storage_per_mwh_h2"]),
            "optimize_wind_min_mw": float(cfg["optimize_wind_min_mw"]),
            "optimize_wind_max_mw": float(cfg["optimize_wind_max_mw"]),
            "optimize_wind_step_mw": float(cfg["optimize_wind_step_mw"]),
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
            "indefinite_check_years": int(cfg["indefinite_check_years"]),
            "indefinite_soc_convergence_tol_mwh": float(
                cfg["indefinite_soc_convergence_tol_mwh"]
            ),
            "indefinite_unmet_tolerance_mwh": float(cfg["indefinite_unmet_tolerance_mwh"]),
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
        },
        "search_stats": {
            "wind_candidates": len(wind_candidates),
            "evaluated_points": evaluated,
            "feasible_points": feasible_points,
            "elapsed_seconds": elapsed,
        },
        "best_design": best,
    }

    with out_path.open("w") as f:
        json.dump(output, f, indent=2)

    print(f"Optimization summary written: {out_path}")
    print(f"Evaluated points: {evaluated}")
    print(f"Feasible points: {feasible_points}")
    print(f"Elapsed seconds: {elapsed:.2f}")

    if best is None:
        print("No feasible design found in configured search bounds.")
    else:
        print(f"Best CAPEX ({cfg['capex_currency']}): {best['capex_total']:.2f}")
        print(
            f"Best annual OPEX ({cfg['capex_currency']}/year): "
            f"{best['annual_opex_total']:.2f}"
        )
        print(
            "Best design (MW/MWh-H2): "
            f"wind={best['wind_mw']:.2f}, "
            f"electrolyzer={best['electrolyzer_mw']:.2f}, "
            f"h2_turbine={best['h2_turbine_mw']:.2f}, "
            f"storage={best['storage_mwh_h2']:.2f}"
        )


if __name__ == "__main__":
    main()
