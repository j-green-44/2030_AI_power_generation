#!/usr/bin/env python3
"""
Dispatch-coupled hybrid optimizer for wind + solar + hydrogen + gas+CCS.

Decision variables:
- wind installed capacity (MW)
- solar installed capacity (MW)
- electrolyzer capacity (MW)
- H2 turbine capacity (MW)
- H2 storage capacity (MWh-H2)

Dispatch model (hourly):
1) Wind + solar generation serves demand directly.
2) Surplus can charge hydrogen via electrolyzer.
3) Deficit can be met by H2 turbine.
4) Remaining deficit is supplied by gas+CCS.

Objective:
- Minimize total expenditure over lifecycle:
  CAPEX + discounted OPEX.
"""

import argparse
import csv
import json
import math
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
WIND_DIR = ROOT_DIR / "wind"

if str(WIND_DIR) not in sys.path:
    sys.path.insert(0, str(WIND_DIR))

import hydrogen_storage_sizing as hs  # noqa: E402


DEFAULTS = {
    "currency": "GBP_2026_placeholder",
    "lifecycle_years": 25,
    "discount_rate": 0.0,
    "require_h2_cyclic_non_depleting": True,
    "h2_cyclic_tolerance_mwh": 1.0,
    "gas_capacity_max_mw": None,
    "skip_structurally_redundant_designs": True,
    "output_prefix": "hybrid/hybrid_dispatch_opt",
}


SHARED_H2_KEYS = [
    "electricity_to_hydrogen_efficiency",
    "hydrogen_to_electricity_efficiency",
    "start_fullness_pct",
    "soc_floor_pct",
    "soc_ceiling_pct",
    "capex_electrolyzer_per_mw",
    "capex_h2_turbine_per_mw",
    "capex_storage_per_mwh_h2",
    "electrolyzer_fixed_om_per_mw_year",
    "electrolyzer_variable_om_per_mwh_in",
    "h2_turbine_fixed_om_per_mw_year",
    "h2_turbine_variable_om_per_mwh_out",
    "storage_om_per_mwh_h2_year",
    "electrolyzer_stack_replacement_cost_per_mw",
    "electrolyzer_stack_replacement_interval_years",
    "water_cost_per_kg_h2",
    "compression_and_purification_cost_per_kg_h2",
]


def resolve_path(path_str, base_dir):
    p = Path(path_str)
    if p.is_absolute():
        return p
    from_cwd = Path.cwd() / p
    if from_cwd.exists():
        return from_cwd
    from_base = base_dir / p
    if from_base.exists():
        return from_base
    return from_cwd


def load_json(path):
    with path.open() as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return obj


def frange(start, stop, step):
    if step <= 0:
        raise ValueError("range step must be > 0.")
    vals = []
    x = float(start)
    stop = float(stop)
    while x <= stop + 1e-9:
        vals.append(float(f"{x:.10f}"))
        x += step
    if not vals or vals[-1] < stop - 1e-9:
        vals.append(stop)
    return vals


def annual_discount_factor(lifecycle_years, discount_rate):
    if discount_rate <= 0:
        return float(lifecycle_years)
    r = float(discount_rate)
    n = int(lifecycle_years)
    return (1.0 - (1.0 + r) ** (-n)) / r


def periodic_discount_sum(event_cost, interval_years, lifecycle_years, discount_rate):
    if interval_years <= 0:
        return 0.0
    years = [y for y in range(1, lifecycle_years + 1) if y % interval_years == 0]
    if discount_rate <= 0:
        return event_cost * len(years)
    total = 0.0
    r = float(discount_rate)
    for year in years:
        total += event_cost / ((1.0 + r) ** year)
    return total


def periodic_undiscounted_sum(event_cost, interval_years, lifecycle_years):
    if interval_years <= 0:
        return 0.0
    events = lifecycle_years // interval_years
    return event_cost * events


def merge_and_validate_h2_inputs(wind_cfg, solar_cfg):
    merged = {}
    for key in SHARED_H2_KEYS:
        if key not in wind_cfg:
            raise ValueError(f"Missing key in wind config: {key}")
        if key not in solar_cfg:
            raise ValueError(f"Missing key in solar config: {key}")
        w = float(wind_cfg[key])
        s = float(solar_cfg[key])
        if abs(w - s) > 1e-9:
            raise ValueError(
                f"Hydrogen-side mismatch for '{key}': wind={w}, solar={s}. "
                "Align these in both configs for a coupled hybrid run."
            )
        merged[key] = w
    return merged


def parse_gas_inputs(gas_cfg):
    required_capacity = float(gas_cfg["required_installed_capacity_mw"])
    if required_capacity <= 0:
        raise ValueError("gas required_installed_capacity_mw must be > 0.")

    if "capital_cost_gbp_per_gw_mid" in gas_cfg:
        gas_capex_per_mw = float(gas_cfg["capital_cost_gbp_per_gw_mid"]) / 1000.0
    else:
        gas_capex_per_mw = float(gas_cfg["capital_cost_total_gbp_mid"]) / required_capacity

    fuel_scenario = gas_cfg.get("use_fuel_price_scenario", "base")
    fuel_price_map = gas_cfg["fuel_price_scenarios_gbp_per_mwh_th"]
    if fuel_scenario not in fuel_price_map:
        raise ValueError(
            f"Gas fuel scenario '{fuel_scenario}' not found in fuel_price_scenarios_gbp_per_mwh_th."
        )
    fuel_price = float(fuel_price_map[fuel_scenario])

    return {
        "gas_capex_per_mw": gas_capex_per_mw,
        "fuel_price_gbp_per_mwh_th": fuel_price,
        "net_heat_rate_mwh_th_per_mwh_e": float(gas_cfg["net_heat_rate_mwh_th_per_mwh_e"]),
        "fixed_om_gbp_per_mw_year_mid": float(gas_cfg["fixed_om_gbp_per_mw_year_mid"]),
        "variable_om_gbp_per_mwh_e": float(gas_cfg["variable_om_gbp_per_mwh_e"]),
        "ccs_consumables_gbp_per_mwh_e_mid_assumption": float(
            gas_cfg["ccs_consumables_gbp_per_mwh_e_mid_assumption"]
        ),
        "co2_transport_storage_cost_gbp_per_tco2_mid": float(
            gas_cfg["co2_transport_storage_cost_gbp_per_tco2_mid"]
        ),
        "residual_emissions_carbon_cost_gbp_per_tco2": float(
            gas_cfg["residual_emissions_carbon_cost_gbp_per_tco2"]
        ),
        "ccs_capture_rate_pct_mid": float(gas_cfg["ccs_capture_rate_pct_mid"]),
        "natural_gas_emissions_tco2_per_mwh_th": float(
            gas_cfg.get("natural_gas_emissions_tco2_per_mwh_th", 0.184)
        ),
        "major_maintenance_cost_basis": str(
            gas_cfg.get("major_maintenance_cost_basis", "per_plant")
        ).lower(),
        "major_maintenance_cost_gbp_per_plant_mid": float(
            gas_cfg["major_maintenance_cost_gbp_per_plant_mid"]
        ),
        "major_maintenance_cost_gbp_per_mw_mid": float(
            gas_cfg.get("major_maintenance_cost_gbp_per_mw_mid", 0.0)
        ),
        "major_maintenance_cost_gbp_per_gw_mid": float(
            gas_cfg.get("major_maintenance_cost_gbp_per_gw_mid", 0.0)
        ),
        "major_maintenance_interval_years_assumption": int(
            gas_cfg["major_maintenance_interval_years_assumption"]
        ),
        "plant_unit_capacity_mw": float(gas_cfg["plant_unit_capacity_mw"]),
        "round_plant_count_for_maintenance_up": bool(
            gas_cfg.get("round_plant_count_for_maintenance_up", False)
        ),
        "fuel_scenario_selected": fuel_scenario,
    }


def load_and_align_profiles(wind_csv_path, solar_csv_path):
    wind_times, wind_mw = hs.load_wind_series(wind_csv_path)
    solar_times, solar_mw = hs.load_wind_series(solar_csv_path)

    solar_lookup = {t: v for t, v in zip(solar_times, solar_mw)}
    aligned_times = []
    aligned_wind = []
    aligned_solar = []

    for t, w in zip(wind_times, wind_mw):
        if t not in solar_lookup:
            continue
        aligned_times.append(t)
        aligned_wind.append(w)
        aligned_solar.append(solar_lookup[t])

    if not aligned_times:
        raise ValueError("No overlapping timestamps between wind and solar series.")
    if len(aligned_times) < min(len(wind_times), len(solar_times)):
        print(
            f"Warning: aligned profile length={len(aligned_times)} (wind={len(wind_times)}, solar={len(solar_times)})."
        )
    return aligned_times, aligned_wind, aligned_solar


def simulate_dispatch(
    renewable_mw,
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
    if storage_capacity_mwh_h2 < 0:
        raise ValueError("storage_capacity_mwh_h2 must be >= 0.")
    if not (0 < eta_charge <= 1):
        raise ValueError("eta_charge must be in (0, 1].")
    if not (0 < eta_discharge <= 1):
        raise ValueError("eta_discharge must be in (0, 1].")

    if storage_capacity_mwh_h2 == 0:
        soc_floor_mwh = 0.0
        soc_ceiling_mwh = 0.0
        soc = 0.0
    else:
        soc_floor_mwh = storage_capacity_mwh_h2 * soc_floor_pct / 100.0
        soc_ceiling_mwh = storage_capacity_mwh_h2 * soc_ceiling_pct / 100.0
        soc = storage_capacity_mwh_h2 * start_fullness_pct / 100.0
        soc = max(min(soc, soc_ceiling_mwh), soc_floor_mwh)

    start_soc_mwh = soc
    min_soc_mwh = soc
    max_soc_mwh = soc

    demand_total_mwh = demand_mw * len(renewable_mw)
    direct_renewable_mwh = 0.0
    curtailed_surplus_electric_mwh = 0.0
    electrolyzer_input_mwh = 0.0
    h2_charge_mwh = 0.0
    h2_discharge_mwh = 0.0
    h2_turbine_output_mwh = 0.0
    gas_generation_mwh = 0.0
    max_gas_dispatch_mw = 0.0

    for p in renewable_mw:
        direct = min(p, demand_mw)
        surplus = max(p - demand_mw, 0.0)
        deficit = max(demand_mw - p, 0.0)
        direct_renewable_mwh += direct

        charge_input = 0.0
        if storage_capacity_mwh_h2 > 0 and electrolyzer_mw > 0:
            charge_input = min(surplus, electrolyzer_mw)
            charge_room_electric = max((soc_ceiling_mwh - soc) / eta_charge, 0.0)
            charge_input = min(charge_input, charge_room_electric)

        charge_h2 = charge_input * eta_charge
        soc += charge_h2
        electrolyzer_input_mwh += charge_input
        h2_charge_mwh += charge_h2
        curtailed_surplus_electric_mwh += max(surplus - charge_input, 0.0)

        h2_output = 0.0
        if storage_capacity_mwh_h2 > 0 and h2_turbine_mw > 0:
            h2_output = min(deficit, h2_turbine_mw)
            h2_output_by_soc = max(soc - soc_floor_mwh, 0.0) * eta_discharge
            h2_output = min(h2_output, h2_output_by_soc)

        discharge_h2 = h2_output / eta_discharge if h2_output > 0 else 0.0
        soc -= discharge_h2
        h2_turbine_output_mwh += h2_output
        h2_discharge_mwh += discharge_h2

        gas_out = max(deficit - h2_output, 0.0)
        gas_generation_mwh += gas_out
        max_gas_dispatch_mw = max(max_gas_dispatch_mw, gas_out)

        min_soc_mwh = min(min_soc_mwh, soc)
        max_soc_mwh = max(max_soc_mwh, soc)

    end_soc_mwh = soc
    h2_produced_tonnes = hs.mwh_h2_to_tonnes_h2(h2_charge_mwh)
    h2_dispatched_tonnes = hs.mwh_h2_to_tonnes_h2(h2_discharge_mwh)

    return {
        "hours": len(renewable_mw),
        "demand_total_mwh": demand_total_mwh,
        "direct_renewable_mwh": direct_renewable_mwh,
        "curtailed_surplus_electric_mwh": curtailed_surplus_electric_mwh,
        "electrolyzer_input_mwh": electrolyzer_input_mwh,
        "h2_charge_mwh": h2_charge_mwh,
        "h2_discharge_mwh": h2_discharge_mwh,
        "h2_turbine_output_mwh": h2_turbine_output_mwh,
        "h2_produced_tonnes": h2_produced_tonnes,
        "h2_dispatched_tonnes": h2_dispatched_tonnes,
        "gas_generation_mwh": gas_generation_mwh,
        "max_gas_dispatch_mw": max_gas_dispatch_mw,
        "start_soc_mwh": start_soc_mwh,
        "end_soc_mwh": end_soc_mwh,
        "min_soc_mwh": min_soc_mwh,
        "max_soc_mwh": max_soc_mwh,
        "start_soc_pct": (
            0.0 if storage_capacity_mwh_h2 == 0 else 100.0 * start_soc_mwh / storage_capacity_mwh_h2
        ),
        "end_soc_pct": (
            0.0 if storage_capacity_mwh_h2 == 0 else 100.0 * end_soc_mwh / storage_capacity_mwh_h2
        ),
        "min_soc_pct": (
            0.0 if storage_capacity_mwh_h2 == 0 else 100.0 * min_soc_mwh / storage_capacity_mwh_h2
        ),
        "max_soc_pct": (
            0.0 if storage_capacity_mwh_h2 == 0 else 100.0 * max_soc_mwh / storage_capacity_mwh_h2
        ),
    }


def compute_gas_maintenance_event_cost(gas_capacity_mw, gas_inputs):
    basis = gas_inputs["major_maintenance_cost_basis"]
    if gas_capacity_mw <= 0:
        return 0.0
    if basis == "per_mw":
        return gas_inputs["major_maintenance_cost_gbp_per_mw_mid"] * gas_capacity_mw
    if basis == "per_gw":
        return gas_inputs["major_maintenance_cost_gbp_per_gw_mid"] * (gas_capacity_mw / 1000.0)
    if basis == "per_plant":
        unit = gas_inputs["plant_unit_capacity_mw"]
        if unit <= 0:
            raise ValueError("plant_unit_capacity_mw must be > 0 for per_plant maintenance.")
        count = gas_capacity_mw / unit
        if gas_inputs["round_plant_count_for_maintenance_up"]:
            count = math.ceil(count)
        return gas_inputs["major_maintenance_cost_gbp_per_plant_mid"] * count
    raise ValueError("Unsupported major_maintenance_cost_basis.")


def evaluate_design(
    renewable_mw,
    wind_mw,
    solar_mw,
    electrolyzer_mw,
    h2_turbine_mw,
    storage_mwh_h2,
    shared_h2,
    wind_cfg,
    solar_cfg,
    gas_inputs,
    demand_mw,
    lifecycle_years,
    discount_rate,
    require_h2_cyclic_non_depleting,
    h2_cyclic_tolerance_mwh,
    gas_capacity_max_mw,
):
    sim = simulate_dispatch(
        renewable_mw=renewable_mw,
        demand_mw=demand_mw,
        eta_charge=float(shared_h2["electricity_to_hydrogen_efficiency"]),
        eta_discharge=float(shared_h2["hydrogen_to_electricity_efficiency"]),
        electrolyzer_mw=electrolyzer_mw,
        h2_turbine_mw=h2_turbine_mw,
        storage_capacity_mwh_h2=storage_mwh_h2,
        start_fullness_pct=float(shared_h2["start_fullness_pct"]),
        soc_floor_pct=float(shared_h2["soc_floor_pct"]),
        soc_ceiling_pct=float(shared_h2["soc_ceiling_pct"]),
    )

    if require_h2_cyclic_non_depleting:
        if sim["end_soc_mwh"] + h2_cyclic_tolerance_mwh < sim["start_soc_mwh"]:
            return None

    gas_capacity_mw = sim["max_gas_dispatch_mw"]
    if gas_capacity_max_mw is not None and gas_capacity_mw > gas_capacity_max_mw + 1e-9:
        return None

    heat_rate = gas_inputs["net_heat_rate_mwh_th_per_mwh_e"]
    thermal_input_mwh_th = sim["gas_generation_mwh"] * heat_rate
    gross_co2_tonnes = thermal_input_mwh_th * gas_inputs["natural_gas_emissions_tco2_per_mwh_th"]
    capture_rate = gas_inputs["ccs_capture_rate_pct_mid"] / 100.0
    captured_co2_tonnes = gross_co2_tonnes * capture_rate
    residual_co2_tonnes = gross_co2_tonnes - captured_co2_tonnes

    annual_opex = {
        "wind_fixed_om": float(wind_cfg["wind_fixed_om_per_mw_year"]) * wind_mw,
        "solar_fixed_om": float(solar_cfg["solar_fixed_om_per_mw_year"]) * solar_mw,
        "electrolyzer_fixed_om": float(shared_h2["electrolyzer_fixed_om_per_mw_year"])
        * electrolyzer_mw,
        "electrolyzer_variable_om": float(shared_h2["electrolyzer_variable_om_per_mwh_in"])
        * sim["electrolyzer_input_mwh"],
        "h2_turbine_fixed_om": float(shared_h2["h2_turbine_fixed_om_per_mw_year"]) * h2_turbine_mw,
        "h2_turbine_variable_om": float(shared_h2["h2_turbine_variable_om_per_mwh_out"])
        * sim["h2_turbine_output_mwh"],
        "storage_fixed_om": float(shared_h2["storage_om_per_mwh_h2_year"]) * storage_mwh_h2,
        "electrolyzer_stack_replacement_annualized": (
            float(shared_h2["electrolyzer_stack_replacement_cost_per_mw"])
            * electrolyzer_mw
            / float(shared_h2["electrolyzer_stack_replacement_interval_years"])
        ),
        "water": float(shared_h2["water_cost_per_kg_h2"]) * sim["h2_produced_tonnes"] * 1000.0,
        "compression_and_purification": float(shared_h2["compression_and_purification_cost_per_kg_h2"])
        * sim["h2_produced_tonnes"]
        * 1000.0,
        "gas_fixed_om": gas_inputs["fixed_om_gbp_per_mw_year_mid"] * gas_capacity_mw,
        "gas_variable_om": gas_inputs["variable_om_gbp_per_mwh_e"] * sim["gas_generation_mwh"],
        "gas_fuel": gas_inputs["fuel_price_gbp_per_mwh_th"] * thermal_input_mwh_th,
        "gas_ccs_consumables": gas_inputs["ccs_consumables_gbp_per_mwh_e_mid_assumption"]
        * sim["gas_generation_mwh"],
        "gas_co2_transport_storage": gas_inputs["co2_transport_storage_cost_gbp_per_tco2_mid"]
        * captured_co2_tonnes,
        "gas_residual_carbon": gas_inputs["residual_emissions_carbon_cost_gbp_per_tco2"]
        * residual_co2_tonnes,
    }
    annual_opex_total = sum(annual_opex.values())

    annual_factor = annual_discount_factor(lifecycle_years, discount_rate)
    recurring_opex_undiscounted = annual_opex_total * lifecycle_years
    recurring_opex_discounted = annual_opex_total * annual_factor

    maintenance_event_cost = compute_gas_maintenance_event_cost(gas_capacity_mw, gas_inputs)
    maintenance_interval = gas_inputs["major_maintenance_interval_years_assumption"]
    maintenance_undiscounted = periodic_undiscounted_sum(
        maintenance_event_cost,
        maintenance_interval,
        lifecycle_years,
    )
    maintenance_discounted = periodic_discount_sum(
        maintenance_event_cost,
        maintenance_interval,
        lifecycle_years,
        discount_rate,
    )

    capex = {
        "wind": float(wind_cfg["capex_wind_per_mw"]) * wind_mw,
        "solar": float(solar_cfg["capex_solar_per_mw"]) * solar_mw,
        "electrolyzer": float(shared_h2["capex_electrolyzer_per_mw"]) * electrolyzer_mw,
        "h2_turbine": float(shared_h2["capex_h2_turbine_per_mw"]) * h2_turbine_mw,
        "storage": float(shared_h2["capex_storage_per_mwh_h2"]) * storage_mwh_h2,
        "gas_ccs_capacity": gas_inputs["gas_capex_per_mw"] * gas_capacity_mw,
    }
    capex_total = sum(capex.values())

    opex_lifecycle_undiscounted = recurring_opex_undiscounted + maintenance_undiscounted
    opex_lifecycle_discounted = recurring_opex_discounted + maintenance_discounted
    objective_total_expenditure = capex_total + opex_lifecycle_discounted

    return {
        "wind_mw": wind_mw,
        "solar_mw": solar_mw,
        "electrolyzer_mw": electrolyzer_mw,
        "h2_turbine_mw": h2_turbine_mw,
        "storage_mwh_h2": storage_mwh_h2,
        "gas_ccs_capacity_mw": gas_capacity_mw,
        "capex_breakdown": capex,
        "capex_total": capex_total,
        "annual_opex_breakdown": annual_opex,
        "annual_opex_total": annual_opex_total,
        "maintenance_event_cost": maintenance_event_cost,
        "maintenance_lifecycle_undiscounted": maintenance_undiscounted,
        "maintenance_lifecycle_discounted": maintenance_discounted,
        "opex_lifecycle_undiscounted": opex_lifecycle_undiscounted,
        "opex_lifecycle_discounted": opex_lifecycle_discounted,
        "objective_total_expenditure": objective_total_expenditure,
        "dispatch_metrics": {
            "demand_total_mwh": sim["demand_total_mwh"],
            "direct_renewable_mwh": sim["direct_renewable_mwh"],
            "curtailed_surplus_electric_mwh": sim["curtailed_surplus_electric_mwh"],
            "electrolyzer_input_mwh": sim["electrolyzer_input_mwh"],
            "h2_charge_mwh": sim["h2_charge_mwh"],
            "h2_discharge_mwh": sim["h2_discharge_mwh"],
            "h2_turbine_output_mwh": sim["h2_turbine_output_mwh"],
            "h2_produced_tonnes": sim["h2_produced_tonnes"],
            "h2_dispatched_tonnes": sim["h2_dispatched_tonnes"],
            "gas_generation_mwh": sim["gas_generation_mwh"],
            "max_gas_dispatch_mw": sim["max_gas_dispatch_mw"],
            "start_soc_mwh": sim["start_soc_mwh"],
            "end_soc_mwh": sim["end_soc_mwh"],
            "min_soc_mwh": sim["min_soc_mwh"],
            "max_soc_mwh": sim["max_soc_mwh"],
            "start_soc_pct": sim["start_soc_pct"],
            "end_soc_pct": sim["end_soc_pct"],
            "min_soc_pct": sim["min_soc_pct"],
            "max_soc_pct": sim["max_soc_pct"],
            "thermal_input_mwh_th": thermal_input_mwh_th,
            "gross_co2_tonnes": gross_co2_tonnes,
            "captured_co2_tonnes": captured_co2_tonnes,
            "residual_co2_tonnes": residual_co2_tonnes,
        },
    }


def build_component_rows(best, lifecycle_years, discount_rate):
    annual_factor = annual_discount_factor(lifecycle_years, discount_rate)
    a = best["annual_opex_breakdown"]
    c = best["capex_breakdown"]

    rows = [
        {
            "design_component": "wind",
            "best_design_(MW/MWh-H2)": f"{best['wind_mw']:.2f} MW",
            "capex_for_best_design": c["wind"],
            "opex_over_25_years_for_best_design": a["wind_fixed_om"] * annual_factor,
        },
        {
            "design_component": "solar",
            "best_design_(MW/MWh-H2)": f"{best['solar_mw']:.2f} MW",
            "capex_for_best_design": c["solar"],
            "opex_over_25_years_for_best_design": a["solar_fixed_om"] * annual_factor,
        },
        {
            "design_component": "electrolyzer",
            "best_design_(MW/MWh-H2)": f"{best['electrolyzer_mw']:.2f} MW",
            "capex_for_best_design": c["electrolyzer"],
            "opex_over_25_years_for_best_design": (
                a["electrolyzer_fixed_om"]
                + a["electrolyzer_variable_om"]
                + a["electrolyzer_stack_replacement_annualized"]
                + a["water"]
                + a["compression_and_purification"]
            )
            * annual_factor,
        },
        {
            "design_component": "h2_turbine",
            "best_design_(MW/MWh-H2)": f"{best['h2_turbine_mw']:.2f} MW",
            "capex_for_best_design": c["h2_turbine"],
            "opex_over_25_years_for_best_design": (
                a["h2_turbine_fixed_om"] + a["h2_turbine_variable_om"]
            )
            * annual_factor,
        },
        {
            "design_component": "h2_storage",
            "best_design_(MW/MWh-H2)": f"{best['storage_mwh_h2']:.2f} MWh-H2",
            "capex_for_best_design": c["storage"],
            "opex_over_25_years_for_best_design": a["storage_fixed_om"] * annual_factor,
        },
        {
            "design_component": "gas_ccs_capacity",
            "best_design_(MW/MWh-H2)": f"{best['gas_ccs_capacity_mw']:.2f} MW",
            "capex_for_best_design": c["gas_ccs_capacity"],
            "opex_over_25_years_for_best_design": 0.0,
        },
        {
            "design_component": "gas_fixed_om",
            "best_design_(MW/MWh-H2)": "",
            "capex_for_best_design": 0.0,
            "opex_over_25_years_for_best_design": a["gas_fixed_om"] * annual_factor,
        },
        {
            "design_component": "gas_variable_om",
            "best_design_(MW/MWh-H2)": "",
            "capex_for_best_design": 0.0,
            "opex_over_25_years_for_best_design": a["gas_variable_om"] * annual_factor,
        },
        {
            "design_component": "gas_fuel",
            "best_design_(MW/MWh-H2)": "",
            "capex_for_best_design": 0.0,
            "opex_over_25_years_for_best_design": a["gas_fuel"] * annual_factor,
        },
        {
            "design_component": "gas_ccs_consumables",
            "best_design_(MW/MWh-H2)": "",
            "capex_for_best_design": 0.0,
            "opex_over_25_years_for_best_design": a["gas_ccs_consumables"] * annual_factor,
        },
        {
            "design_component": "gas_co2_transport_storage",
            "best_design_(MW/MWh-H2)": "",
            "capex_for_best_design": 0.0,
            "opex_over_25_years_for_best_design": a["gas_co2_transport_storage"] * annual_factor,
        },
        {
            "design_component": "gas_residual_carbon",
            "best_design_(MW/MWh-H2)": "",
            "capex_for_best_design": 0.0,
            "opex_over_25_years_for_best_design": a["gas_residual_carbon"] * annual_factor,
        },
        {
            "design_component": "gas_major_maintenance",
            "best_design_(MW/MWh-H2)": "",
            "capex_for_best_design": 0.0,
            "opex_over_25_years_for_best_design": best["maintenance_lifecycle_discounted"],
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


def write_csv(path, rows):
    if not rows:
        with path.open("w", newline="") as f:
            f.write("")
        return
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(
        description="Dispatch-coupled optimizer for hybrid wind+solar+H2+gasCCS."
    )
    parser.add_argument(
        "--config",
        default="hybrid/hybrid_dispatch_config.json",
        help="Path to hybrid dispatch config JSON.",
    )
    parser.add_argument(
        "--output-prefix",
        help="Optional output prefix override.",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    cfg = DEFAULTS.copy()
    cfg.update(load_json(config_path))

    base_dir = config_path.parent
    wind_cfg_path = resolve_path(cfg["wind_config"], base_dir)
    solar_cfg_path = resolve_path(cfg["solar_config"], base_dir)
    gas_cfg_path = resolve_path(cfg["gas_config"], base_dir)

    wind_cfg = load_json(wind_cfg_path)
    solar_cfg = load_json(solar_cfg_path)
    gas_cfg = load_json(gas_cfg_path)

    shared_h2 = merge_and_validate_h2_inputs(wind_cfg, solar_cfg)
    gas_inputs = parse_gas_inputs(gas_cfg)

    demand_mw = float(cfg.get("demand_mw", wind_cfg["demand_mw"]))
    lifecycle_years = int(cfg["lifecycle_years"])
    discount_rate = float(cfg["discount_rate"])

    wind_stress = float(wind_cfg.get("wind_stress_factor", 1.0))
    solar_stress = float(solar_cfg.get("solar_stress_factor", solar_cfg.get("wind_stress_factor", 1.0)))
    if wind_stress <= 0 or solar_stress <= 0:
        raise ValueError("wind_stress_factor and solar_stress_factor must be > 0.")

    wind_csv = resolve_path(wind_cfg["csv"], wind_cfg_path.parent)
    solar_csv = resolve_path(solar_cfg["csv"], solar_cfg_path.parent)
    times, wind_profile_raw, solar_profile_raw = load_and_align_profiles(wind_csv, solar_csv)

    wind_profile_capacity_mw = hs.extract_installed_capacity_mw_from_csv_metadata(wind_csv)
    if wind_profile_capacity_mw is None:
        wind_profile_capacity_mw = float(wind_cfg["current_installed_capacity_mw"])

    solar_profile_capacity_mw = hs.extract_installed_capacity_mw_from_csv_metadata(solar_csv)
    if solar_profile_capacity_mw is None:
        solar_profile_capacity_mw = float(solar_cfg["current_installed_capacity_mw"])

    wind_candidates = frange(
        float(cfg["optimize_wind_min_mw"]),
        float(cfg["optimize_wind_max_mw"]),
        float(cfg["optimize_wind_step_mw"]),
    )
    solar_candidates = frange(
        float(cfg["optimize_solar_min_mw"]),
        float(cfg["optimize_solar_max_mw"]),
        float(cfg["optimize_solar_step_mw"]),
    )
    electrolyzer_candidates = frange(
        float(cfg["optimize_electrolyzer_min_mw"]),
        float(cfg["optimize_electrolyzer_max_mw"]),
        float(cfg["optimize_electrolyzer_step_mw"]),
    )
    h2_turbine_candidates = frange(
        float(cfg["optimize_h2_turbine_min_mw"]),
        float(cfg["optimize_h2_turbine_max_mw"]),
        float(cfg["optimize_h2_turbine_step_mw"]),
    )
    storage_candidates = frange(
        float(cfg["optimize_storage_min_mwh_h2"]),
        float(cfg["optimize_storage_max_mwh_h2"]),
        float(cfg["optimize_storage_step_mwh_h2"]),
    )

    gas_capacity_max_mw = cfg.get("gas_capacity_max_mw")
    gas_capacity_max_mw = None if gas_capacity_max_mw is None else float(gas_capacity_max_mw)

    wind_profiles = {}
    for wind_mw in wind_candidates:
        scale = (wind_mw / wind_profile_capacity_mw) * wind_stress
        wind_profiles[wind_mw] = [w * scale for w in wind_profile_raw]

    solar_profiles = {}
    for solar_mw in solar_candidates:
        scale = (solar_mw / solar_profile_capacity_mw) * solar_stress
        solar_profiles[solar_mw] = [s * scale for s in solar_profile_raw]

    total_candidates = (
        len(wind_candidates)
        * len(solar_candidates)
        * len(electrolyzer_candidates)
        * len(h2_turbine_candidates)
        * len(storage_candidates)
    )
    skipped_structural = 0
    simulated = 0
    feasible = 0

    experiments = []
    best = None

    for wind_mw in wind_candidates:
        wind_scaled = wind_profiles[wind_mw]
        for solar_mw in solar_candidates:
            solar_scaled = solar_profiles[solar_mw]
            renewable = [w + s for w, s in zip(wind_scaled, solar_scaled)]

            for electrolyzer_mw in electrolyzer_candidates:
                for h2_turbine_mw in h2_turbine_candidates:
                    for storage_mwh_h2 in storage_candidates:
                        if bool(cfg["skip_structurally_redundant_designs"]):
                            h2_any = (
                                electrolyzer_mw > 0
                                or h2_turbine_mw > 0
                                or storage_mwh_h2 > 0
                            )
                            if (storage_mwh_h2 <= 0) and h2_any:
                                skipped_structural += 1
                                continue
                            if (electrolyzer_mw <= 0 or h2_turbine_mw <= 0) and (storage_mwh_h2 > 0):
                                skipped_structural += 1
                                continue

                        simulated += 1
                        result = evaluate_design(
                            renewable_mw=renewable,
                            wind_mw=wind_mw,
                            solar_mw=solar_mw,
                            electrolyzer_mw=electrolyzer_mw,
                            h2_turbine_mw=h2_turbine_mw,
                            storage_mwh_h2=storage_mwh_h2,
                            shared_h2=shared_h2,
                            wind_cfg=wind_cfg,
                            solar_cfg=solar_cfg,
                            gas_inputs=gas_inputs,
                            demand_mw=demand_mw,
                            lifecycle_years=lifecycle_years,
                            discount_rate=discount_rate,
                            require_h2_cyclic_non_depleting=bool(
                                cfg["require_h2_cyclic_non_depleting"]
                            ),
                            h2_cyclic_tolerance_mwh=float(cfg["h2_cyclic_tolerance_mwh"]),
                            gas_capacity_max_mw=gas_capacity_max_mw,
                        )
                        if result is None:
                            continue

                        feasible += 1
                        experiments.append(
                            {
                                "wind_mw": wind_mw,
                                "solar_mw": solar_mw,
                                "electrolyzer_mw": electrolyzer_mw,
                                "h2_turbine_mw": h2_turbine_mw,
                                "storage_mwh_h2": storage_mwh_h2,
                                "gas_ccs_capacity_mw": result["gas_ccs_capacity_mw"],
                                "annual_gas_generation_mwh": result["dispatch_metrics"][
                                    "gas_generation_mwh"
                                ],
                                "annual_direct_renewable_mwh": result["dispatch_metrics"][
                                    "direct_renewable_mwh"
                                ],
                                "annual_h2_turbine_output_mwh": result["dispatch_metrics"][
                                    "h2_turbine_output_mwh"
                                ],
                                "annual_curtailed_surplus_mwh": result["dispatch_metrics"][
                                    "curtailed_surplus_electric_mwh"
                                ],
                                "end_soc_pct": result["dispatch_metrics"]["end_soc_pct"],
                                "min_soc_pct": result["dispatch_metrics"]["min_soc_pct"],
                                "capex_total_gbp": result["capex_total"],
                                "opex_lifecycle_undiscounted_gbp": result[
                                    "opex_lifecycle_undiscounted"
                                ],
                                "opex_lifecycle_discounted_gbp": result[
                                    "opex_lifecycle_discounted"
                                ],
                                "objective_total_expenditure_gbp": result[
                                    "objective_total_expenditure"
                                ],
                            }
                        )

                        if (best is None) or (
                            result["objective_total_expenditure"]
                            < best["objective_total_expenditure"]
                        ):
                            best = result

    if best is None:
        raise ValueError(
            "No feasible design found. "
            "Expand search ranges or relax cyclic/gas-capacity constraints."
        )

    output_prefix = Path(args.output_prefix) if args.output_prefix else Path(cfg["output_prefix"])
    summary_path = Path(f"{output_prefix}_summary.json")
    experiments_path = Path(f"{output_prefix}_experiments.csv")
    component_csv_path = Path(f"{output_prefix}_component_cost_table.csv")

    write_csv(experiments_path, experiments)
    component_rows = build_component_rows(best, lifecycle_years, discount_rate)
    write_csv(component_csv_path, component_rows)

    summary = {
        "inputs": {
            "config_file": str(config_path),
            "scenario_name": cfg.get("scenario_name"),
            "currency": cfg.get("currency"),
            "lifecycle_years": lifecycle_years,
            "discount_rate": discount_rate,
            "demand_mw": demand_mw,
            "wind_config": str(wind_cfg_path),
            "solar_config": str(solar_cfg_path),
            "gas_config": str(gas_cfg_path),
            "wind_profile_capacity_mw": wind_profile_capacity_mw,
            "solar_profile_capacity_mw": solar_profile_capacity_mw,
            "wind_stress_factor": wind_stress,
            "solar_stress_factor": solar_stress,
            "eta_charge": float(shared_h2["electricity_to_hydrogen_efficiency"]),
            "eta_discharge": float(shared_h2["hydrogen_to_electricity_efficiency"]),
            "soc_floor_pct": float(shared_h2["soc_floor_pct"]),
            "soc_ceiling_pct": float(shared_h2["soc_ceiling_pct"]),
            "start_fullness_pct": float(shared_h2["start_fullness_pct"]),
            "require_h2_cyclic_non_depleting": bool(cfg["require_h2_cyclic_non_depleting"]),
            "h2_cyclic_tolerance_mwh": float(cfg["h2_cyclic_tolerance_mwh"]),
            "gas_capacity_max_mw": gas_capacity_max_mw,
            "gas_fuel_scenario_selected": gas_inputs["fuel_scenario_selected"],
            "gas_fuel_price_gbp_per_mwh_th": gas_inputs["fuel_price_gbp_per_mwh_th"],
        },
        "search_stats": {
            "total_candidate_points": total_candidates,
            "skipped_structural_points": skipped_structural,
            "simulated_points": simulated,
            "feasible_points": feasible,
        },
        "best_design": best,
        "outputs": {
            "experiments_csv": str(experiments_path),
            "component_cost_table_csv": str(component_csv_path),
        },
    }

    with summary_path.open("w") as f:
        json.dump(summary, f, indent=2)

    print(f"Summary written: {summary_path}")
    print(f"Experiments written: {experiments_path}")
    print(f"Component cost table written: {component_csv_path}")
    print(f"Total candidate points: {total_candidates}")
    print(f"Skipped structural points: {skipped_structural}")
    print(f"Simulated points: {simulated}")
    print(f"Feasible points: {feasible}")
    print(f"Best objective total expenditure (GBP): {best['objective_total_expenditure']:.2f}")
    print(
        "Best design (MW/MWh-H2): "
        f"wind={best['wind_mw']:.2f}, "
        f"solar={best['solar_mw']:.2f}, "
        f"electrolyzer={best['electrolyzer_mw']:.2f}, "
        f"h2_turbine={best['h2_turbine_mw']:.2f}, "
        f"storage={best['storage_mwh_h2']:.2f}, "
        f"gas_ccs_capacity={best['gas_ccs_capacity_mw']:.2f}"
    )


if __name__ == "__main__":
    main()
