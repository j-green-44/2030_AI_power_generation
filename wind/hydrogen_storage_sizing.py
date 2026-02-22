#!/usr/bin/env python3
"""
Hydrogen storage sizing from hourly wind generation against flat demand.

Assumptions:
- Hourly time step.
- Wind serves demand directly first.
- Surplus electricity can be converted to hydrogen (electrolyzer efficiency).
- Deficits are supplied from hydrogen back to electricity (discharge efficiency).
- Gas storage is lossless.
"""

import argparse
import csv
import json
import math
from pathlib import Path

LHV_KWH_PER_KG_H2 = 33.33

DEFAULT_CONFIG = {
    "csv": "ninja_wind_56.4559_-1.3674_corrected.csv",
    "demand_mw": 8200.0,
    # Preferred explicit names:
    "electricity_to_hydrogen_efficiency": 1.0,
    "hydrogen_to_electricity_efficiency": 1.0,
    # Backward-compatible aliases:
    "eta_charge": 1.0,
    "eta_discharge": 1.0,
    "min_end_soc_mwh": 1.0,
    "output_prefix": "h2_storage",
    "write_timeseries": True,
    # Assumption: working H2 capacity per UK-scale salt cavern.
    "uk_salt_cavern_working_capacity_tonnes_h2": 5500.0,
    # Constrained SOC operation settings (all percentages are 0-100).
    "start_fullness_pct": 50.0,
    "soc_floor_pct": 0.0,
    "soc_ceiling_pct": 100.0,
    # Multiplicative stress on wind profile (e.g., 0.9 = 10% lower wind).
    "wind_stress_factor": 1.0,
    # Indefinite feasibility checks over repeated years.
    "indefinite_check_years": 20,
    "indefinite_soc_convergence_tol_mwh": 1000.0,
    "indefinite_unmet_tolerance_mwh": 1e-6,
    # Installed capacity used in simulation (MW). If null, use CSV profile capacity.
    "simulation_installed_capacity_mw": None,
    # Optional fixed reservoir capacity for constrained operation in MWh(H2).
    # If null/omitted, script uses unbounded sizing result for baseline.
    "reservoir_capacity_mwh_h2": None,
    "max_wind_scale_search": 100.0,
    # Optional for build-out reporting (if omitted, script will try CSV metadata).
    "current_installed_capacity_mw": None,
    # Optional for turbine counts.
    "turbine_rating_mw": 9.5,
}


def load_wind_series(csv_path: Path):
    times = []
    wind_mw = []

    with csv_path.open(newline="") as f:
        rows = (line for line in f if not line.startswith("#"))
        reader = csv.DictReader(rows)
        if reader.fieldnames is None:
            raise ValueError(f"No CSV header found in {csv_path}.")

        raw_fieldnames = [f for f in reader.fieldnames if f is not None]
        fieldnames = [f.lstrip("\ufeff").strip() for f in raw_fieldnames]
        lower_map = {name.lower(): name for name in fieldnames}

        time_col = lower_map.get("time") or lower_map.get("local_time")
        power_col = lower_map.get("electricity") or lower_map.get("wind_mw")
        if not time_col or not power_col:
            raise ValueError(
                f"Unsupported CSV columns in {csv_path}. "
                f"Found: {fieldnames}. Need time/local_time and electricity/wind_mw."
            )

        for row in reader:
            norm = {}
            for key, value in row.items():
                if key is None:
                    continue
                norm[key.lstrip("\ufeff").strip()] = value

            times.append(norm[time_col])
            if power_col.lower() == "electricity":
                wind_mw.append(float(norm[power_col]) / 1000.0)  # kW -> MW
            else:
                wind_mw.append(float(norm[power_col]))  # already MW

    if not wind_mw:
        raise ValueError("No data rows found in CSV after metadata lines.")

    return times, wind_mw


def extract_installed_capacity_mw_from_csv_metadata(csv_path: Path):
    with csv_path.open() as f:
        for line in f:
            if not line.startswith("#"):
                break
            stripped = line[1:].strip()
            if not stripped.startswith("{"):
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            try:
                capacity_kw = float(payload["params"]["capacity"])
                return capacity_kw / 1000.0
            except (KeyError, ValueError, TypeError):
                continue
    return None


def simulate(times, wind_mw, demand_mw, eta_charge, eta_discharge):
    if eta_charge <= 0 or eta_charge > 1:
        raise ValueError("eta_charge must be in (0, 1].")
    if eta_discharge <= 0 or eta_discharge > 1:
        raise ValueError("eta_discharge must be in (0, 1].")

    rows = []
    cumulative_h2_mwh = 0.0
    min_cum = 0.0
    max_cum = 0.0

    total_wind_mwh = 0.0
    demand_total_mwh = demand_mw * len(wind_mw)
    below_hours = 0

    surplus_electric_mwh = 0.0
    deficit_electric_mwh = 0.0
    max_surplus_mw = 0.0
    max_deficit_mw = 0.0

    for t, w in zip(times, wind_mw):
        total_wind_mwh += w

        surplus_mw = max(w - demand_mw, 0.0)
        deficit_mw = max(demand_mw - w, 0.0)
        if deficit_mw > 0:
            below_hours += 1

        surplus_electric_mwh += surplus_mw
        deficit_electric_mwh += deficit_mw
        max_surplus_mw = max(max_surplus_mw, surplus_mw)
        max_deficit_mw = max(max_deficit_mw, deficit_mw)

        charge_h2_mwh = surplus_mw * eta_charge
        discharge_h2_mwh = deficit_mw / eta_discharge
        delta_h2_mwh = charge_h2_mwh - discharge_h2_mwh

        cumulative_h2_mwh += delta_h2_mwh
        min_cum = min(min_cum, cumulative_h2_mwh)
        max_cum = max(max_cum, cumulative_h2_mwh)

        rows.append(
            {
                "time": t,
                "wind_mw": w,
                "demand_mw": demand_mw,
                "surplus_mw": surplus_mw,
                "deficit_mw": deficit_mw,
                "charge_h2_mwh": charge_h2_mwh,
                "discharge_h2_mwh": discharge_h2_mwh,
                "delta_h2_mwh": delta_h2_mwh,
                "cum_delta_h2_mwh": cumulative_h2_mwh,
            }
        )

    net_h2_balance_mwh = cumulative_h2_mwh
    min_start_soc_for_no_unmet_mwh = -min_cum
    working_storage_needed_mwh = max_cum - min_cum
    end_soc_if_start_min_mwh = min_start_soc_for_no_unmet_mwh + net_h2_balance_mwh

    cyclic_feasible = net_h2_balance_mwh >= -1e-9

    return {
        "rows": rows,
        "hours": len(wind_mw),
        "demand_total_mwh": demand_total_mwh,
        "total_wind_mwh": total_wind_mwh,
        "below_hours": below_hours,
        "below_pct": 100.0 * below_hours / len(wind_mw),
        "surplus_electric_mwh": surplus_electric_mwh,
        "deficit_electric_mwh": deficit_electric_mwh,
        "max_surplus_mw": max_surplus_mw,
        "max_deficit_mw": max_deficit_mw,
        "net_h2_balance_mwh": net_h2_balance_mwh,
        "min_start_soc_for_no_unmet_mwh": min_start_soc_for_no_unmet_mwh,
        "end_soc_if_start_min_mwh": end_soc_if_start_min_mwh,
        "working_storage_needed_mwh": working_storage_needed_mwh,
        "cyclic_feasible": cyclic_feasible,
        "h2_topup_needed_for_cyclic_mwh": max(-net_h2_balance_mwh, 0.0),
    }


def apply_wind_stress(wind_mw, wind_stress_factor):
    if wind_stress_factor <= 0:
        raise ValueError("wind_stress_factor must be > 0.")
    return [w * wind_stress_factor for w in wind_mw]


def simulate_with_soc_limits(
    times,
    wind_mw,
    demand_mw,
    eta_charge,
    eta_discharge,
    storage_capacity_mwh_h2,
    start_fullness_pct,
    soc_floor_pct,
    soc_ceiling_pct,
    collect_rows=True,
):
    if eta_charge <= 0 or eta_charge > 1:
        raise ValueError("eta_charge must be in (0, 1].")
    if eta_discharge <= 0 or eta_discharge > 1:
        raise ValueError("eta_discharge must be in (0, 1].")
    if storage_capacity_mwh_h2 <= 0:
        raise ValueError("storage_capacity_mwh_h2 must be > 0.")
    if soc_floor_pct < 0 or soc_floor_pct > 100:
        raise ValueError("soc_floor_pct must be in [0, 100].")
    if soc_ceiling_pct < 0 or soc_ceiling_pct > 100:
        raise ValueError("soc_ceiling_pct must be in [0, 100].")
    if soc_floor_pct >= soc_ceiling_pct:
        raise ValueError("soc_floor_pct must be < soc_ceiling_pct.")
    if start_fullness_pct < soc_floor_pct or start_fullness_pct > soc_ceiling_pct:
        raise ValueError("start_fullness_pct must be within [soc_floor_pct, soc_ceiling_pct].")

    soc_floor_mwh = storage_capacity_mwh_h2 * soc_floor_pct / 100.0
    soc_ceiling_mwh = storage_capacity_mwh_h2 * soc_ceiling_pct / 100.0
    soc = storage_capacity_mwh_h2 * start_fullness_pct / 100.0
    start_soc_mwh = soc

    rows = [] if collect_rows else None
    min_soc_mwh = soc
    max_soc_mwh = soc
    floor_hits = 0
    ceiling_hits = 0

    total_wind_mwh = 0.0
    demand_total_mwh = demand_mw * len(wind_mw)
    below_hours = 0
    surplus_electric_mwh = 0.0
    deficit_electric_mwh = 0.0
    max_surplus_mw = 0.0
    max_deficit_mw = 0.0

    total_charge_h2_mwh = 0.0
    total_discharge_h2_mwh = 0.0
    curtailed_h2_charge_mwh = 0.0
    curtailed_surplus_electric_mwh = 0.0
    unmet_electric_mwh = 0.0
    unmet_hours = 0

    eps = 1e-9

    for t, w in zip(times, wind_mw):
        total_wind_mwh += w
        surplus_mw = max(w - demand_mw, 0.0)
        deficit_mw = max(demand_mw - w, 0.0)
        if deficit_mw > 0:
            below_hours += 1

        surplus_electric_mwh += surplus_mw
        deficit_electric_mwh += deficit_mw
        max_surplus_mw = max(max_surplus_mw, surplus_mw)
        max_deficit_mw = max(max_deficit_mw, deficit_mw)

        soc_start = soc
        charge_h2_potential = surplus_mw * eta_charge
        available_charge_room = max(soc_ceiling_mwh - soc, 0.0)
        charge_h2_actual = min(charge_h2_potential, available_charge_room)
        charge_h2_curtailed = charge_h2_potential - charge_h2_actual

        discharge_h2_need = deficit_mw / eta_discharge
        available_discharge = max(soc - soc_floor_mwh, 0.0)
        discharge_h2_actual = min(discharge_h2_need, available_discharge)
        unmet_mw = deficit_mw - (discharge_h2_actual * eta_discharge)

        soc = soc + charge_h2_actual - discharge_h2_actual
        if soc < soc_floor_mwh and soc_floor_mwh - soc < 1e-6:
            soc = soc_floor_mwh
        if soc > soc_ceiling_mwh and soc - soc_ceiling_mwh < 1e-6:
            soc = soc_ceiling_mwh

        min_soc_mwh = min(min_soc_mwh, soc)
        max_soc_mwh = max(max_soc_mwh, soc)
        if soc <= soc_floor_mwh + eps:
            floor_hits += 1
        if soc >= soc_ceiling_mwh - eps:
            ceiling_hits += 1

        total_charge_h2_mwh += charge_h2_actual
        total_discharge_h2_mwh += discharge_h2_actual
        curtailed_h2_charge_mwh += charge_h2_curtailed
        curtailed_surplus_electric_mwh += charge_h2_curtailed / eta_charge
        unmet_electric_mwh += max(unmet_mw, 0.0)
        if unmet_mw > eps:
            unmet_hours += 1

        if collect_rows:
            rows.append(
                {
                    "time": t,
                    "wind_mw": w,
                    "demand_mw": demand_mw,
                    "surplus_mw": surplus_mw,
                    "deficit_mw": deficit_mw,
                    "charge_h2_mwh_potential": charge_h2_potential,
                    "charge_h2_mwh_actual": charge_h2_actual,
                    "discharge_h2_mwh_needed": discharge_h2_need,
                    "discharge_h2_mwh_actual": discharge_h2_actual,
                    "curtailed_surplus_electric_mwh": charge_h2_curtailed / eta_charge,
                    "unmet_electric_mwh": max(unmet_mw, 0.0),
                    "soc_start_mwh": soc_start,
                    "soc_end_mwh": soc,
                    "soc_end_pct": (soc / storage_capacity_mwh_h2) * 100.0,
                }
            )

    end_soc_mwh = soc
    net_h2_balance_mwh = total_charge_h2_mwh - total_discharge_h2_mwh
    indefinite_feasible = (unmet_electric_mwh <= 1e-6) and (end_soc_mwh >= start_soc_mwh - 1e-6)

    return {
        "rows": rows if collect_rows else [],
        "hours": len(wind_mw),
        "demand_total_mwh": demand_total_mwh,
        "total_wind_mwh": total_wind_mwh,
        "below_hours": below_hours,
        "below_pct": 100.0 * below_hours / len(wind_mw),
        "surplus_electric_mwh": surplus_electric_mwh,
        "deficit_electric_mwh": deficit_electric_mwh,
        "max_surplus_mw": max_surplus_mw,
        "max_deficit_mw": max_deficit_mw,
        "total_charge_h2_mwh_actual": total_charge_h2_mwh,
        "total_discharge_h2_mwh_actual": total_discharge_h2_mwh,
        "curtailed_h2_charge_mwh": curtailed_h2_charge_mwh,
        "curtailed_surplus_electric_mwh": curtailed_surplus_electric_mwh,
        "unmet_electric_mwh": unmet_electric_mwh,
        "unmet_hours": unmet_hours,
        "start_soc_mwh": start_soc_mwh,
        "end_soc_mwh": end_soc_mwh,
        "min_soc_mwh": min_soc_mwh,
        "max_soc_mwh": max_soc_mwh,
        "min_soc_pct": (min_soc_mwh / storage_capacity_mwh_h2) * 100.0,
        "max_soc_pct": (max_soc_mwh / storage_capacity_mwh_h2) * 100.0,
        "floor_hits_hours": floor_hits,
        "ceiling_hits_hours": ceiling_hits,
        "net_h2_balance_mwh": net_h2_balance_mwh,
        "indefinite_feasible": indefinite_feasible,
    }


def net_h2_balance_for_scale(wind_mw, demand_mw, eta_charge, eta_discharge, scale):
    balance = 0.0
    for w in wind_mw:
        ws = w * scale
        surplus_mw = max(ws - demand_mw, 0.0)
        deficit_mw = max(demand_mw - ws, 0.0)
        balance += surplus_mw * eta_charge - deficit_mw / eta_discharge
    return balance


def find_min_scale_for_cyclic_independence(
    wind_mw, demand_mw, eta_charge, eta_discharge, max_scale=100.0
):
    base = net_h2_balance_for_scale(wind_mw, demand_mw, eta_charge, eta_discharge, 1.0)
    if base >= 0:
        return 1.0

    lo = 1.0
    hi = 1.0
    hi_balance = base
    while hi_balance < 0 and hi < max_scale:
        hi *= 2.0
        hi_balance = net_h2_balance_for_scale(
            wind_mw, demand_mw, eta_charge, eta_discharge, hi
        )

    if hi_balance < 0:
        raise ValueError(
            f"Could not find feasible wind scale <= {max_scale} for cyclic independence."
        )

    for _ in range(80):
        mid = 0.5 * (lo + hi)
        mid_balance = net_h2_balance_for_scale(
            wind_mw, demand_mw, eta_charge, eta_discharge, mid
        )
        if mid_balance >= 0:
            hi = mid
        else:
            lo = mid

    return hi


def find_min_scale_for_indefinite_operation(
    times,
    wind_mw,
    demand_mw,
    eta_charge,
    eta_discharge,
    storage_capacity_mwh_h2,
    start_fullness_pct,
    soc_floor_pct,
    soc_ceiling_pct,
    min_end_soc_mwh,
    indefinite_check_years,
    indefinite_soc_convergence_tol_mwh,
    indefinite_unmet_tolerance_mwh,
    max_scale=100.0,
):
    def assess_for_scale(scale):
        return assess_indefinite_operation_repeated_years(
            times=times,
            wind_mw=[w * scale for w in wind_mw],
            demand_mw=demand_mw,
            eta_charge=eta_charge,
            eta_discharge=eta_discharge,
            storage_capacity_mwh_h2=storage_capacity_mwh_h2,
            start_fullness_pct=start_fullness_pct,
            soc_floor_pct=soc_floor_pct,
            soc_ceiling_pct=soc_ceiling_pct,
            min_end_soc_mwh=min_end_soc_mwh,
            indefinite_check_years=indefinite_check_years,
            indefinite_soc_convergence_tol_mwh=indefinite_soc_convergence_tol_mwh,
            indefinite_unmet_tolerance_mwh=indefinite_unmet_tolerance_mwh,
        )

    def feasible(scale):
        return assess_for_scale(scale)["indefinite_feasible"]

    if feasible(1.0):
        return 1.0

    lo = 1.0
    hi = 1.0
    while hi < max_scale and not feasible(hi):
        hi *= 2.0

    if hi > max_scale:
        hi = max_scale
    if not feasible(hi):
        raise ValueError(
            f"Could not find feasible wind scale <= {max_scale} "
            "for indefinite operation with SOC constraints."
        )

    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if feasible(mid):
            hi = mid
        else:
            lo = mid

    return hi


def assess_indefinite_operation_repeated_years(
    times,
    wind_mw,
    demand_mw,
    eta_charge,
    eta_discharge,
    storage_capacity_mwh_h2,
    start_fullness_pct,
    soc_floor_pct,
    soc_ceiling_pct,
    min_end_soc_mwh,
    indefinite_check_years,
    indefinite_soc_convergence_tol_mwh,
    indefinite_unmet_tolerance_mwh,
):
    if indefinite_check_years < 1:
        raise ValueError("indefinite_check_years must be >= 1.")
    if indefinite_soc_convergence_tol_mwh < 0:
        raise ValueError("indefinite_soc_convergence_tol_mwh must be >= 0.")
    if indefinite_unmet_tolerance_mwh < 0:
        raise ValueError("indefinite_unmet_tolerance_mwh must be >= 0.")

    yearly = []
    start_soc_mwh = storage_capacity_mwh_h2 * start_fullness_pct / 100.0
    year_start_soc_mwh = start_soc_mwh
    previous_end_soc_mwh = None
    converged_year = None
    all_years_meet_load = True
    all_years_meet_min_end = True
    max_abs_drift_mwh = 0.0

    for year in range(1, indefinite_check_years + 1):
        year_start_pct = (year_start_soc_mwh / storage_capacity_mwh_h2) * 100.0
        sim_year = simulate_with_soc_limits(
            times=times,
            wind_mw=wind_mw,
            demand_mw=demand_mw,
            eta_charge=eta_charge,
            eta_discharge=eta_discharge,
            storage_capacity_mwh_h2=storage_capacity_mwh_h2,
            start_fullness_pct=year_start_pct,
            soc_floor_pct=soc_floor_pct,
            soc_ceiling_pct=soc_ceiling_pct,
            collect_rows=False,
        )

        year_end_soc_mwh = sim_year["end_soc_mwh"]
        delta_soc_mwh = year_end_soc_mwh - year_start_soc_mwh
        abs_drift_mwh = abs(delta_soc_mwh)
        max_abs_drift_mwh = max(max_abs_drift_mwh, abs_drift_mwh)

        meets_load = sim_year["unmet_electric_mwh"] <= indefinite_unmet_tolerance_mwh
        meets_min_end = year_end_soc_mwh >= float(min_end_soc_mwh) - 1e-6
        all_years_meet_load = all_years_meet_load and meets_load
        all_years_meet_min_end = all_years_meet_min_end and meets_min_end

        if previous_end_soc_mwh is not None and converged_year is None:
            if abs(year_end_soc_mwh - previous_end_soc_mwh) <= indefinite_soc_convergence_tol_mwh:
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

    indefinite_feasible = (
        all_years_meet_load
        and all_years_meet_min_end
        and converged
        and years_simulated >= indefinite_check_years
    )

    return {
        "years_requested": indefinite_check_years,
        "years_simulated": years_simulated,
        "convergence_tol_mwh": indefinite_soc_convergence_tol_mwh,
        "unmet_tolerance_mwh": indefinite_unmet_tolerance_mwh,
        "min_end_soc_mwh_requirement": float(min_end_soc_mwh),
        "start_soc_mwh": start_soc_mwh,
        "start_soc_pct": start_fullness_pct,
        "final_soc_mwh": final_soc_mwh,
        "final_soc_pct": final_soc_pct,
        "all_years_meet_load": all_years_meet_load,
        "all_years_meet_min_end_soc": all_years_meet_min_end,
        "converged": converged,
        "converged_year": converged_year,
        "max_abs_yearly_soc_drift_mwh": max_abs_drift_mwh,
        "indefinite_feasible": indefinite_feasible,
        "yearly_results": yearly,
    }


def mwh_h2_to_tonnes_h2(mwh_h2):
    return (mwh_h2 * 1000.0 / LHV_KWH_PER_KG_H2) / 1000.0


def tonnes_h2_to_mwh_h2(tonnes_h2):
    return tonnes_h2 * LHV_KWH_PER_KG_H2


def load_config(path: Path):
    with path.open() as f:
        loaded = json.load(f)
    if not isinstance(loaded, dict):
        raise ValueError("Config file must contain a JSON object.")
    cfg = DEFAULT_CONFIG.copy()
    cfg.update(loaded)
    return cfg


def resolve_inputs(args):
    config_path = Path(args.config)
    cfg = load_config(config_path)

    if args.csv is not None:
        cfg["csv"] = args.csv
    if args.demand_mw is not None:
        cfg["demand_mw"] = args.demand_mw
    if args.eta_charge is not None:
        cfg["eta_charge"] = args.eta_charge
    if args.eta_discharge is not None:
        cfg["eta_discharge"] = args.eta_discharge
    if args.electricity_to_hydrogen_efficiency is not None:
        cfg["electricity_to_hydrogen_efficiency"] = args.electricity_to_hydrogen_efficiency
    if args.hydrogen_to_electricity_efficiency is not None:
        cfg["hydrogen_to_electricity_efficiency"] = args.hydrogen_to_electricity_efficiency
    if args.min_end_soc_mwh is not None:
        cfg["min_end_soc_mwh"] = args.min_end_soc_mwh
    if args.output_prefix is not None:
        cfg["output_prefix"] = args.output_prefix
    if args.write_timeseries is not None:
        cfg["write_timeseries"] = args.write_timeseries
    if args.start_fullness_pct is not None:
        cfg["start_fullness_pct"] = args.start_fullness_pct
    if args.soc_floor_pct is not None:
        cfg["soc_floor_pct"] = args.soc_floor_pct
    if args.soc_ceiling_pct is not None:
        cfg["soc_ceiling_pct"] = args.soc_ceiling_pct
    if args.wind_stress_factor is not None:
        cfg["wind_stress_factor"] = args.wind_stress_factor
    if args.indefinite_check_years is not None:
        cfg["indefinite_check_years"] = args.indefinite_check_years
    if args.indefinite_soc_convergence_tol_mwh is not None:
        cfg["indefinite_soc_convergence_tol_mwh"] = args.indefinite_soc_convergence_tol_mwh
    if args.indefinite_unmet_tolerance_mwh is not None:
        cfg["indefinite_unmet_tolerance_mwh"] = args.indefinite_unmet_tolerance_mwh
    if args.simulation_installed_capacity_mw is not None:
        cfg["simulation_installed_capacity_mw"] = args.simulation_installed_capacity_mw
    if args.reservoir_capacity_mwh_h2 is not None:
        cfg["reservoir_capacity_mwh_h2"] = args.reservoir_capacity_mwh_h2
    if args.max_wind_scale_search is not None:
        cfg["max_wind_scale_search"] = args.max_wind_scale_search
    if args.uk_salt_cavern_working_capacity_tonnes_h2 is not None:
        cfg["uk_salt_cavern_working_capacity_tonnes_h2"] = (
            args.uk_salt_cavern_working_capacity_tonnes_h2
        )
    if args.current_installed_capacity_mw is not None:
        cfg["current_installed_capacity_mw"] = args.current_installed_capacity_mw
    if args.turbine_rating_mw is not None:
        cfg["turbine_rating_mw"] = args.turbine_rating_mw

    cavern_tonnes = float(cfg["uk_salt_cavern_working_capacity_tonnes_h2"])
    if cavern_tonnes <= 0:
        raise ValueError("uk_salt_cavern_working_capacity_tonnes_h2 must be > 0.")
    if cfg.get("turbine_rating_mw") is not None and float(cfg["turbine_rating_mw"]) <= 0:
        raise ValueError("turbine_rating_mw must be > 0 when provided.")
    if float(cfg["wind_stress_factor"]) <= 0:
        raise ValueError("wind_stress_factor must be > 0.")
    if float(cfg["max_wind_scale_search"]) <= 1.0:
        raise ValueError("max_wind_scale_search must be > 1.0.")
    if int(cfg["indefinite_check_years"]) < 1:
        raise ValueError("indefinite_check_years must be >= 1.")
    if float(cfg["indefinite_soc_convergence_tol_mwh"]) < 0:
        raise ValueError("indefinite_soc_convergence_tol_mwh must be >= 0.")
    if float(cfg["indefinite_unmet_tolerance_mwh"]) < 0:
        raise ValueError("indefinite_unmet_tolerance_mwh must be >= 0.")
    if (
        cfg.get("simulation_installed_capacity_mw") is not None
        and float(cfg["simulation_installed_capacity_mw"]) <= 0
    ):
        raise ValueError("simulation_installed_capacity_mw must be > 0 when provided.")
    if cfg.get("reservoir_capacity_mwh_h2") is not None and float(cfg["reservoir_capacity_mwh_h2"]) <= 0:
        raise ValueError("reservoir_capacity_mwh_h2 must be > 0 when provided.")
    soc_floor_pct = float(cfg["soc_floor_pct"])
    soc_ceiling_pct = float(cfg["soc_ceiling_pct"])
    start_fullness_pct = float(cfg["start_fullness_pct"])
    if soc_floor_pct < 0 or soc_floor_pct > 100:
        raise ValueError("soc_floor_pct must be in [0, 100].")
    if soc_ceiling_pct < 0 or soc_ceiling_pct > 100:
        raise ValueError("soc_ceiling_pct must be in [0, 100].")
    if soc_floor_pct >= soc_ceiling_pct:
        raise ValueError("soc_floor_pct must be < soc_ceiling_pct.")
    if start_fullness_pct < soc_floor_pct or start_fullness_pct > soc_ceiling_pct:
        raise ValueError("start_fullness_pct must be within [soc_floor_pct, soc_ceiling_pct].")

    # Normalize efficiency keys with precedence:
    # explicit names > eta aliases.
    eta_charge = cfg.get("electricity_to_hydrogen_efficiency", cfg.get("eta_charge"))
    eta_discharge = cfg.get("hydrogen_to_electricity_efficiency", cfg.get("eta_discharge"))
    if eta_charge is None or eta_discharge is None:
        raise ValueError(
            "Set efficiencies in config using either explicit names "
            "(electricity_to_hydrogen_efficiency, hydrogen_to_electricity_efficiency) "
            "or eta aliases (eta_charge, eta_discharge)."
        )

    cfg["eta_charge"] = float(eta_charge)
    cfg["eta_discharge"] = float(eta_discharge)
    cfg["electricity_to_hydrogen_efficiency"] = cfg["eta_charge"]
    cfg["hydrogen_to_electricity_efficiency"] = cfg["eta_discharge"]

    if cfg["eta_charge"] <= 0 or cfg["eta_charge"] > 1:
        raise ValueError("electricity_to_hydrogen_efficiency must be in (0, 1].")
    if cfg["eta_discharge"] <= 0 or cfg["eta_discharge"] > 1:
        raise ValueError("hydrogen_to_electricity_efficiency must be in (0, 1].")

    return cfg, config_path


def write_timeseries(path: Path, rows):
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
    parser = argparse.ArgumentParser(
        description=(
            "Size hydrogen storage for hourly wind + flat demand, "
            "using a local JSON config file."
        )
    )
    parser.add_argument(
        "--config",
        default="hydrogen_storage_config.json",
        help="Path to JSON config file (default: hydrogen_storage_config.json).",
    )
    parser.add_argument("--csv", help="Path to wind CSV file.")
    parser.add_argument(
        "--demand-mw",
        type=float,
        help="Flat demand in MW.",
    )
    parser.add_argument(
        "--eta-charge",
        type=float,
        help="Electrolyzer efficiency, 0-1.",
    )
    parser.add_argument(
        "--eta-discharge",
        type=float,
        help="Hydrogen-to-power efficiency, 0-1.",
    )
    parser.add_argument(
        "--electricity-to-hydrogen-efficiency",
        type=float,
        help="Electricity -> hydrogen efficiency, 0-1 (preferred key).",
    )
    parser.add_argument(
        "--hydrogen-to-electricity-efficiency",
        type=float,
        help="Hydrogen -> electricity efficiency, 0-1 (preferred key).",
    )
    parser.add_argument(
        "--min-end-soc-mwh",
        type=float,
        help="Required minimum end-of-year SOC in MWh(H2).",
    )
    parser.add_argument(
        "--start-fullness-pct",
        type=float,
        help="Initial storage fullness percentage for constrained operation.",
    )
    parser.add_argument(
        "--soc-floor-pct",
        type=float,
        help="Minimum allowable storage fullness percentage.",
    )
    parser.add_argument(
        "--soc-ceiling-pct",
        type=float,
        help="Maximum allowable storage fullness percentage.",
    )
    parser.add_argument(
        "--wind-stress-factor",
        type=float,
        help="Multiplicative stress factor on wind profile (e.g., 0.9 for -10%).",
    )
    parser.add_argument(
        "--indefinite-check-years",
        type=int,
        help="Number of repeated years to test indefinite feasibility.",
    )
    parser.add_argument(
        "--indefinite-soc-convergence-tol-mwh",
        type=float,
        help="SOC convergence tolerance between consecutive years in MWh(H2).",
    )
    parser.add_argument(
        "--indefinite-unmet-tolerance-mwh",
        type=float,
        help="Allowed unmet load tolerance per year in MWh.",
    )
    parser.add_argument(
        "--simulation-installed-capacity-mw",
        type=float,
        help=(
            "Installed wind capacity used for simulation (MW). "
            "If omitted, uses profile capacity from CSV metadata."
        ),
    )
    parser.add_argument(
        "--reservoir-capacity-mwh-h2",
        type=float,
        help=(
            "Fixed reservoir capacity for constrained run in MWh(H2). "
            "If omitted, uses unbounded baseline sizing."
        ),
    )
    parser.add_argument(
        "--max-wind-scale-search",
        type=float,
        help="Upper bound for wind scale search in constrained solver.",
    )
    parser.add_argument(
        "--output-prefix",
        help="Prefix for output files.",
    )
    parser.add_argument(
        "--uk-salt-cavern-working-capacity-tonnes-h2",
        type=float,
        help=(
            "Working H2 capacity per UK-scale salt cavern in tonnes H2 "
            "(overrides config)."
        ),
    )
    parser.add_argument(
        "--current-installed-capacity-mw",
        type=float,
        help="Current installed wind capacity in MW (overrides config).",
    )
    parser.add_argument(
        "--turbine-rating-mw",
        type=float,
        help="Single turbine rating in MW for turbine count outputs (overrides config).",
    )
    parser.add_argument(
        "--write-timeseries",
        dest="write_timeseries",
        action="store_true",
        help="Write per-hour calculation CSV (overrides config).",
    )
    parser.add_argument(
        "--no-write-timeseries",
        dest="write_timeseries",
        action="store_false",
        help="Do not write per-hour calculation CSV (overrides config).",
    )
    parser.set_defaults(write_timeseries=None)
    args = parser.parse_args()

    cfg, config_path = resolve_inputs(args)

    csv_path = Path(cfg["csv"])
    out_prefix = Path(cfg["output_prefix"])
    summary_path = Path(f"{out_prefix}_summary.json")
    timeseries_path = Path(f"{out_prefix}_timeseries.csv")

    times, wind_mw_raw = load_wind_series(csv_path)

    profile_installed_capacity_mw = extract_installed_capacity_mw_from_csv_metadata(csv_path)
    simulation_installed_capacity_mw = cfg.get("simulation_installed_capacity_mw")
    if simulation_installed_capacity_mw is None:
        simulation_installed_capacity_mw = profile_installed_capacity_mw
    if simulation_installed_capacity_mw is not None:
        simulation_installed_capacity_mw = float(simulation_installed_capacity_mw)

    if simulation_installed_capacity_mw is not None and profile_installed_capacity_mw is not None:
        capacity_scale = simulation_installed_capacity_mw / float(profile_installed_capacity_mw)
        installed_capacity_scaling_applied = True
    elif simulation_installed_capacity_mw is not None and profile_installed_capacity_mw is None:
        # No profile-capacity anchor available (e.g. generated timeseries CSV).
        # Treat provided series as already at desired capacity.
        capacity_scale = 1.0
        installed_capacity_scaling_applied = False
    else:
        capacity_scale = 1.0
        installed_capacity_scaling_applied = False

    wind_mw_capacity_scaled = [w * capacity_scale for w in wind_mw_raw]
    wind_stress_factor = float(cfg["wind_stress_factor"])
    wind_mw = apply_wind_stress(wind_mw_capacity_scaled, wind_stress_factor)

    sim = simulate(
        times=times,
        wind_mw=wind_mw,
        demand_mw=float(cfg["demand_mw"]),
        eta_charge=float(cfg["eta_charge"]),
        eta_discharge=float(cfg["eta_discharge"]),
    )

    required_scale_for_cyclic = find_min_scale_for_cyclic_independence(
        wind_mw=wind_mw,
        demand_mw=float(cfg["demand_mw"]),
        eta_charge=float(cfg["eta_charge"]),
        eta_discharge=float(cfg["eta_discharge"]),
        max_scale=float(cfg["max_wind_scale_search"]),
    )
    wind_mw_required = [w * required_scale_for_cyclic for w in wind_mw]
    sim_required = simulate(
        times=times,
        wind_mw=wind_mw_required,
        demand_mw=float(cfg["demand_mw"]),
        eta_charge=float(cfg["eta_charge"]),
        eta_discharge=float(cfg["eta_discharge"]),
    )

    # Initial SOC needed to both avoid unmet load and finish with reserve.
    min_initial_soc_for_reserve_mwh = max(
        sim["min_start_soc_for_no_unmet_mwh"],
        float(cfg["min_end_soc_mwh"]) - sim["net_h2_balance_mwh"],
    )
    end_soc_with_min_initial_mwh = min_initial_soc_for_reserve_mwh + sim["net_h2_balance_mwh"]
    total_reservoir_capacity_for_reserve_mwh = min_initial_soc_for_reserve_mwh + (
        sim["working_storage_needed_mwh"] - sim["min_start_soc_for_no_unmet_mwh"]
    )

    reservoir_capacity_constrained_mwh = cfg.get("reservoir_capacity_mwh_h2")
    if reservoir_capacity_constrained_mwh is None:
        reservoir_capacity_constrained_mwh = total_reservoir_capacity_for_reserve_mwh
    reservoir_capacity_constrained_mwh = float(reservoir_capacity_constrained_mwh)

    start_fullness_pct = float(cfg["start_fullness_pct"])
    soc_floor_pct = float(cfg["soc_floor_pct"])
    soc_ceiling_pct = float(cfg["soc_ceiling_pct"])
    indefinite_check_years = int(cfg["indefinite_check_years"])
    indefinite_soc_convergence_tol_mwh = float(cfg["indefinite_soc_convergence_tol_mwh"])
    indefinite_unmet_tolerance_mwh = float(cfg["indefinite_unmet_tolerance_mwh"])

    sim_constrained = simulate_with_soc_limits(
        times=times,
        wind_mw=wind_mw,
        demand_mw=float(cfg["demand_mw"]),
        eta_charge=float(cfg["eta_charge"]),
        eta_discharge=float(cfg["eta_discharge"]),
        storage_capacity_mwh_h2=reservoir_capacity_constrained_mwh,
        start_fullness_pct=start_fullness_pct,
        soc_floor_pct=soc_floor_pct,
        soc_ceiling_pct=soc_ceiling_pct,
        collect_rows=True,
    )
    indefinite_assessment_current = assess_indefinite_operation_repeated_years(
        times=times,
        wind_mw=wind_mw,
        demand_mw=float(cfg["demand_mw"]),
        eta_charge=float(cfg["eta_charge"]),
        eta_discharge=float(cfg["eta_discharge"]),
        storage_capacity_mwh_h2=reservoir_capacity_constrained_mwh,
        start_fullness_pct=start_fullness_pct,
        soc_floor_pct=soc_floor_pct,
        soc_ceiling_pct=soc_ceiling_pct,
        min_end_soc_mwh=float(cfg["min_end_soc_mwh"]),
        indefinite_check_years=indefinite_check_years,
        indefinite_soc_convergence_tol_mwh=indefinite_soc_convergence_tol_mwh,
        indefinite_unmet_tolerance_mwh=indefinite_unmet_tolerance_mwh,
    )

    required_scale_for_indefinite = find_min_scale_for_indefinite_operation(
        times=times,
        wind_mw=wind_mw,
        demand_mw=float(cfg["demand_mw"]),
        eta_charge=float(cfg["eta_charge"]),
        eta_discharge=float(cfg["eta_discharge"]),
        storage_capacity_mwh_h2=reservoir_capacity_constrained_mwh,
        start_fullness_pct=start_fullness_pct,
        soc_floor_pct=soc_floor_pct,
        soc_ceiling_pct=soc_ceiling_pct,
        min_end_soc_mwh=float(cfg["min_end_soc_mwh"]),
        indefinite_check_years=indefinite_check_years,
        indefinite_soc_convergence_tol_mwh=indefinite_soc_convergence_tol_mwh,
        indefinite_unmet_tolerance_mwh=indefinite_unmet_tolerance_mwh,
        max_scale=float(cfg["max_wind_scale_search"]),
    )
    wind_mw_required_indefinite = [w * required_scale_for_indefinite for w in wind_mw]
    sim_constrained_required_one_year = simulate_with_soc_limits(
        times=times,
        wind_mw=wind_mw_required_indefinite,
        demand_mw=float(cfg["demand_mw"]),
        eta_charge=float(cfg["eta_charge"]),
        eta_discharge=float(cfg["eta_discharge"]),
        storage_capacity_mwh_h2=reservoir_capacity_constrained_mwh,
        start_fullness_pct=start_fullness_pct,
        soc_floor_pct=soc_floor_pct,
        soc_ceiling_pct=soc_ceiling_pct,
        collect_rows=False,
    )
    indefinite_assessment_required = assess_indefinite_operation_repeated_years(
        times=times,
        wind_mw=wind_mw_required_indefinite,
        demand_mw=float(cfg["demand_mw"]),
        eta_charge=float(cfg["eta_charge"]),
        eta_discharge=float(cfg["eta_discharge"]),
        storage_capacity_mwh_h2=reservoir_capacity_constrained_mwh,
        start_fullness_pct=start_fullness_pct,
        soc_floor_pct=soc_floor_pct,
        soc_ceiling_pct=soc_ceiling_pct,
        min_end_soc_mwh=float(cfg["min_end_soc_mwh"]),
        indefinite_check_years=indefinite_check_years,
        indefinite_soc_convergence_tol_mwh=indefinite_soc_convergence_tol_mwh,
        indefinite_unmet_tolerance_mwh=indefinite_unmet_tolerance_mwh,
    )

    cavern_working_tonnes_h2 = float(cfg["uk_salt_cavern_working_capacity_tonnes_h2"])
    cavern_working_mwh_h2 = tonnes_h2_to_mwh_h2(cavern_working_tonnes_h2)
    working_storage_tonnes_h2 = mwh_h2_to_tonnes_h2(sim["working_storage_needed_mwh"])
    total_reservoir_tonnes_h2 = mwh_h2_to_tonnes_h2(total_reservoir_capacity_for_reserve_mwh)
    min_initial_tonnes_h2 = mwh_h2_to_tonnes_h2(min_initial_soc_for_reserve_mwh)
    constrained_reservoir_tonnes_h2 = mwh_h2_to_tonnes_h2(reservoir_capacity_constrained_mwh)

    caverns_for_working_storage = math.ceil(working_storage_tonnes_h2 / cavern_working_tonnes_h2)
    caverns_for_total_reservoir = math.ceil(total_reservoir_tonnes_h2 / cavern_working_tonnes_h2)
    caverns_for_min_initial = math.ceil(min_initial_tonnes_h2 / cavern_working_tonnes_h2)
    caverns_for_constrained_reservoir = math.ceil(
        constrained_reservoir_tonnes_h2 / cavern_working_tonnes_h2
    )

    min_initial_soc_for_reserve_mwh_required = max(
        sim_required["min_start_soc_for_no_unmet_mwh"],
        float(cfg["min_end_soc_mwh"]) - sim_required["net_h2_balance_mwh"],
    )
    total_reservoir_capacity_for_reserve_mwh_required = (
        min_initial_soc_for_reserve_mwh_required
        + (
            sim_required["working_storage_needed_mwh"]
            - sim_required["min_start_soc_for_no_unmet_mwh"]
        )
    )
    total_reservoir_tonnes_h2_required = mwh_h2_to_tonnes_h2(
        total_reservoir_capacity_for_reserve_mwh_required
    )
    caverns_for_total_reservoir_required = math.ceil(
        total_reservoir_tonnes_h2_required / cavern_working_tonnes_h2
    )

    reference_installed_capacity_mw = cfg.get("current_installed_capacity_mw")
    if reference_installed_capacity_mw is None:
        reference_installed_capacity_mw = profile_installed_capacity_mw
    if reference_installed_capacity_mw is not None:
        reference_installed_capacity_mw = float(reference_installed_capacity_mw)
        required_installed_capacity_mw = reference_installed_capacity_mw * required_scale_for_cyclic
        additional_installed_capacity_mw = (
            required_installed_capacity_mw - reference_installed_capacity_mw
        )
        required_installed_capacity_mw_indefinite = (
            reference_installed_capacity_mw * required_scale_for_indefinite
        )
        additional_installed_capacity_mw_indefinite = (
            required_installed_capacity_mw_indefinite - reference_installed_capacity_mw
        )
    else:
        required_installed_capacity_mw = None
        additional_installed_capacity_mw = None
        required_installed_capacity_mw_indefinite = None
        additional_installed_capacity_mw_indefinite = None

    turbine_rating_mw = cfg.get("turbine_rating_mw")
    if turbine_rating_mw is not None:
        turbine_rating_mw = float(turbine_rating_mw)
    if turbine_rating_mw is not None and required_installed_capacity_mw is not None:
        total_turbines_required = required_installed_capacity_mw / turbine_rating_mw
        additional_turbines_required = additional_installed_capacity_mw / turbine_rating_mw
        total_turbines_required_indefinite = (
            required_installed_capacity_mw_indefinite / turbine_rating_mw
        )
        additional_turbines_required_indefinite = (
            additional_installed_capacity_mw_indefinite / turbine_rating_mw
        )
    else:
        total_turbines_required = None
        additional_turbines_required = None
        total_turbines_required_indefinite = None
        additional_turbines_required_indefinite = None

    summary = {
        "inputs": {
            "config_file": str(config_path),
            "csv": str(csv_path),
            "demand_mw": float(cfg["demand_mw"]),
            "electricity_to_hydrogen_efficiency": float(
                cfg["electricity_to_hydrogen_efficiency"]
            ),
            "hydrogen_to_electricity_efficiency": float(
                cfg["hydrogen_to_electricity_efficiency"]
            ),
            "eta_charge": float(cfg["eta_charge"]),
            "eta_discharge": float(cfg["eta_discharge"]),
            "round_trip_efficiency": float(cfg["eta_charge"]) * float(cfg["eta_discharge"]),
            "min_end_soc_mwh": float(cfg["min_end_soc_mwh"]),
            "start_fullness_pct": start_fullness_pct,
            "soc_floor_pct": soc_floor_pct,
            "soc_ceiling_pct": soc_ceiling_pct,
            "wind_stress_factor": wind_stress_factor,
            "profile_installed_capacity_mw": profile_installed_capacity_mw,
            "simulation_installed_capacity_mw": simulation_installed_capacity_mw,
            "capacity_scale_from_installed_capacity": capacity_scale,
            "installed_capacity_scaling_applied": installed_capacity_scaling_applied,
            "effective_total_wind_scale_on_profile": capacity_scale * wind_stress_factor,
            "reservoir_capacity_mwh_h2_for_constrained_run": reservoir_capacity_constrained_mwh,
            "max_wind_scale_search": float(cfg["max_wind_scale_search"]),
            "indefinite_check_years": indefinite_check_years,
            "indefinite_soc_convergence_tol_mwh": indefinite_soc_convergence_tol_mwh,
            "indefinite_unmet_tolerance_mwh": indefinite_unmet_tolerance_mwh,
            "write_timeseries": bool(cfg["write_timeseries"]),
            "hours": sim["hours"],
            "uk_salt_cavern_working_capacity_tonnes_h2": cavern_working_tonnes_h2,
            "uk_salt_cavern_working_capacity_mwh_h2": cavern_working_mwh_h2,
            "current_installed_capacity_mw": reference_installed_capacity_mw,
            "turbine_rating_mw": turbine_rating_mw,
        },
        "wind_and_demand": {
            "total_wind_mwh_raw": sum(wind_mw_raw),
            "total_wind_twh_raw": sum(wind_mw_raw) / 1_000_000.0,
            "total_wind_mwh_stressed": sim["total_wind_mwh"],
            "total_wind_twh_stressed": sim["total_wind_mwh"] / 1_000_000.0,
            "total_wind_mwh": sim["total_wind_mwh"],
            "total_wind_twh": sim["total_wind_mwh"] / 1_000_000.0,
            "total_demand_mwh": sim["demand_total_mwh"],
            "total_demand_twh": sim["demand_total_mwh"] / 1_000_000.0,
            "hours_below_demand": sim["below_hours"],
            "percent_hours_below_demand": sim["below_pct"],
            "deficit_electric_mwh": sim["deficit_electric_mwh"],
            "surplus_electric_mwh": sim["surplus_electric_mwh"],
            "max_hourly_deficit_mw": sim["max_deficit_mw"],
            "max_hourly_surplus_mw": sim["max_surplus_mw"],
        },
        "hydrogen_sizing": {
            "working_storage_needed_mwh_h2": sim["working_storage_needed_mwh"],
            "working_storage_needed_twh_h2": sim["working_storage_needed_mwh"] / 1_000_000.0,
            "working_storage_needed_tonnes_h2": working_storage_tonnes_h2,
            "electrolyzer_power_needed_mw_to_absorb_all_surplus": sim["max_surplus_mw"],
            "h2_to_power_output_needed_mw_to_cover_all_deficits": sim["max_deficit_mw"],
            "max_h2_charge_rate_mwh_per_h": sim["max_surplus_mw"] * float(cfg["eta_charge"]),
            "max_h2_discharge_rate_mwh_per_h": sim["max_deficit_mw"]
            / float(cfg["eta_discharge"]),
            "min_start_soc_for_no_unmet_mwh_h2": sim["min_start_soc_for_no_unmet_mwh"],
            "end_soc_if_start_min_mwh_h2": sim["end_soc_if_start_min_mwh"],
            "net_h2_balance_mwh_h2": sim["net_h2_balance_mwh"],
            "cyclic_feasible_without_external_energy": sim["cyclic_feasible"],
            "h2_topup_needed_for_cyclic_mwh_h2": sim["h2_topup_needed_for_cyclic_mwh"],
            "h2_topup_needed_for_cyclic_tonnes_h2": mwh_h2_to_tonnes_h2(
                sim["h2_topup_needed_for_cyclic_mwh"]
            ),
            "extra_surplus_electricity_needed_for_cyclic_mwh": (
                sim["h2_topup_needed_for_cyclic_mwh"] / float(cfg["eta_charge"])
            ),
            "extra_surplus_electricity_needed_for_cyclic_twh": (
                sim["h2_topup_needed_for_cyclic_mwh"]
                / float(cfg["eta_charge"])
                / 1_000_000.0
            ),
            "extra_average_wind_power_needed_for_cyclic_mw": (
                sim["h2_topup_needed_for_cyclic_mwh"] / float(cfg["eta_charge"]) / sim["hours"]
            ),
            "min_initial_soc_for_end_reserve_mwh_h2": min_initial_soc_for_reserve_mwh,
            "end_soc_with_min_initial_mwh_h2": end_soc_with_min_initial_mwh,
            "total_reservoir_capacity_for_end_reserve_mwh_h2": total_reservoir_capacity_for_reserve_mwh,
            "total_reservoir_capacity_for_end_reserve_twh_h2": (
                total_reservoir_capacity_for_reserve_mwh / 1_000_000.0
            ),
            "total_reservoir_capacity_for_end_reserve_tonnes_h2": mwh_h2_to_tonnes_h2(
                total_reservoir_capacity_for_reserve_mwh
            ),
            "uk_caverns_needed_for_working_storage": caverns_for_working_storage,
            "uk_caverns_needed_for_total_reservoir_capacity": caverns_for_total_reservoir,
            "uk_caverns_needed_for_min_initial_inventory": caverns_for_min_initial,
        },
        "constrained_operation": {
            "reservoir_capacity_mwh_h2": reservoir_capacity_constrained_mwh,
            "reservoir_capacity_twh_h2": reservoir_capacity_constrained_mwh / 1_000_000.0,
            "reservoir_capacity_tonnes_h2": constrained_reservoir_tonnes_h2,
            "uk_caverns_for_reservoir_capacity": caverns_for_constrained_reservoir,
            "start_soc_mwh_h2": sim_constrained["start_soc_mwh"],
            "end_soc_mwh_h2": sim_constrained["end_soc_mwh"],
            "start_soc_pct": start_fullness_pct,
            "end_soc_pct": (sim_constrained["end_soc_mwh"] / reservoir_capacity_constrained_mwh)
            * 100.0,
            "min_soc_pct": sim_constrained["min_soc_pct"],
            "max_soc_pct": sim_constrained["max_soc_pct"],
            "floor_hits_hours": sim_constrained["floor_hits_hours"],
            "ceiling_hits_hours": sim_constrained["ceiling_hits_hours"],
            "unmet_electric_mwh": sim_constrained["unmet_electric_mwh"],
            "unmet_hours": sim_constrained["unmet_hours"],
            "curtailed_surplus_electric_mwh": sim_constrained["curtailed_surplus_electric_mwh"],
            "indefinite_feasible_with_current_wind": indefinite_assessment_current[
                "indefinite_feasible"
            ],
            "indefinite_all_years_meet_load_with_current_wind": indefinite_assessment_current[
                "all_years_meet_load"
            ],
            "indefinite_all_years_meet_min_end_soc_with_current_wind": (
                indefinite_assessment_current["all_years_meet_min_end_soc"]
            ),
            "indefinite_converged_with_current_wind": indefinite_assessment_current[
                "converged"
            ],
            "indefinite_converged_year_with_current_wind": indefinite_assessment_current[
                "converged_year"
            ],
            "indefinite_years_simulated_with_current_wind": indefinite_assessment_current[
                "years_simulated"
            ],
            "indefinite_max_abs_yearly_soc_drift_mwh_with_current_wind": (
                indefinite_assessment_current["max_abs_yearly_soc_drift_mwh"]
            ),
            "indefinite_final_soc_pct_with_current_wind": indefinite_assessment_current[
                "final_soc_pct"
            ],
            "indefinite_assessment_with_current_wind": indefinite_assessment_current,
        },
        "wind_buildout_for_cyclic_independence": {
            "required_wind_scale_factor": required_scale_for_cyclic,
            "additional_wind_scale_factor": required_scale_for_cyclic - 1.0,
            "percent_overbuild_vs_current": (required_scale_for_cyclic - 1.0) * 100.0,
            "required_installed_capacity_mw": required_installed_capacity_mw,
            "additional_installed_capacity_mw": additional_installed_capacity_mw,
            "total_turbines_required": total_turbines_required,
            "additional_turbines_required": additional_turbines_required,
            "net_h2_balance_mwh_h2_at_required_scale": sim_required["net_h2_balance_mwh"],
            "working_storage_needed_twh_h2_at_required_scale": (
                sim_required["working_storage_needed_mwh"] / 1_000_000.0
            ),
            "total_reservoir_capacity_twh_h2_at_required_scale": (
                total_reservoir_capacity_for_reserve_mwh_required / 1_000_000.0
            ),
            "uk_caverns_needed_for_total_reservoir_at_required_scale": (
                caverns_for_total_reservoir_required
            ),
        },
        "wind_buildout_for_indefinite_operation_with_soc_constraints": {
            "required_wind_scale_factor": required_scale_for_indefinite,
            "additional_wind_scale_factor": required_scale_for_indefinite - 1.0,
            "percent_overbuild_vs_current": (required_scale_for_indefinite - 1.0) * 100.0,
            "required_installed_capacity_mw": required_installed_capacity_mw_indefinite,
            "additional_installed_capacity_mw": additional_installed_capacity_mw_indefinite,
            "total_turbines_required": total_turbines_required_indefinite,
            "additional_turbines_required": additional_turbines_required_indefinite,
            "unmet_electric_mwh_at_required_scale": sim_constrained_required_one_year[
                "unmet_electric_mwh"
            ],
            "end_soc_pct_at_required_scale": (
                sim_constrained_required_one_year["end_soc_mwh"] / reservoir_capacity_constrained_mwh
            )
            * 100.0,
            "min_soc_pct_at_required_scale": sim_constrained_required_one_year["min_soc_pct"],
            "max_soc_pct_at_required_scale": sim_constrained_required_one_year["max_soc_pct"],
            "floor_hits_hours_at_required_scale": sim_constrained_required_one_year[
                "floor_hits_hours"
            ],
            "ceiling_hits_hours_at_required_scale": sim_constrained_required_one_year[
                "ceiling_hits_hours"
            ],
            "indefinite_feasible_at_required_scale": indefinite_assessment_required[
                "indefinite_feasible"
            ],
            "indefinite_all_years_meet_load_at_required_scale": indefinite_assessment_required[
                "all_years_meet_load"
            ],
            "indefinite_all_years_meet_min_end_soc_at_required_scale": (
                indefinite_assessment_required["all_years_meet_min_end_soc"]
            ),
            "indefinite_converged_at_required_scale": indefinite_assessment_required[
                "converged"
            ],
            "indefinite_converged_year_at_required_scale": indefinite_assessment_required[
                "converged_year"
            ],
            "indefinite_years_simulated_at_required_scale": indefinite_assessment_required[
                "years_simulated"
            ],
            "indefinite_max_abs_yearly_soc_drift_mwh_at_required_scale": (
                indefinite_assessment_required["max_abs_yearly_soc_drift_mwh"]
            ),
            "indefinite_final_soc_pct_at_required_scale": indefinite_assessment_required[
                "final_soc_pct"
            ],
            "indefinite_assessment_at_required_scale": indefinite_assessment_required,
        },
    }

    with summary_path.open("w") as f:
        json.dump(summary, f, indent=2)

    if cfg["write_timeseries"]:
        write_timeseries(timeseries_path, sim_constrained["rows"])

    print(f"Summary written: {summary_path}")
    if cfg["write_timeseries"]:
        print(f"Timeseries written: {timeseries_path}")
    print(f"Cyclic feasible without external energy: {sim['cyclic_feasible']}")
    print(
        "Indefinite feasible with current wind under SOC bounds: "
        f"{indefinite_assessment_current['indefinite_feasible']}"
    )
    print(
        "Working storage needed (TWh H2): "
        f"{summary['hydrogen_sizing']['working_storage_needed_twh_h2']:.6f}"
    )
    print(
        "Total reservoir capacity for end reserve (TWh H2): "
        f"{summary['hydrogen_sizing']['total_reservoir_capacity_for_end_reserve_twh_h2']:.6f}"
    )
    print(
        "UK caverns needed for total reservoir capacity: "
        f"{summary['hydrogen_sizing']['uk_caverns_needed_for_total_reservoir_capacity']}"
    )
    print(
        "Required wind scale for cyclic independence: "
        f"{summary['wind_buildout_for_cyclic_independence']['required_wind_scale_factor']:.6f}"
    )
    print(
        "Required wind scale for indefinite SOC-bounded operation: "
        f"{summary['wind_buildout_for_indefinite_operation_with_soc_constraints']['required_wind_scale_factor']:.6f}"
    )
    if summary["wind_buildout_for_cyclic_independence"]["additional_installed_capacity_mw"] is not None:
        print(
            "Additional installed wind capacity needed (MW): "
            f"{summary['wind_buildout_for_cyclic_independence']['additional_installed_capacity_mw']:.2f}"
        )
    if (
        summary["wind_buildout_for_indefinite_operation_with_soc_constraints"][
            "additional_installed_capacity_mw"
        ]
        is not None
    ):
        print(
            "Additional installed wind capacity needed for indefinite SOC-bounded operation (MW): "
            f"{summary['wind_buildout_for_indefinite_operation_with_soc_constraints']['additional_installed_capacity_mw']:.2f}"
        )


if __name__ == "__main__":
    main()
