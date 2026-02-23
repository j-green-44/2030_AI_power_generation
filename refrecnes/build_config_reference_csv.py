#!/usr/bin/env python3
"""
Build reference CSVs for all parameters in selected model config files.

Outputs:
- refrecnes/config_parameter_reference_matrix.csv (5-row matrix style)
- refrecnes/config_parameter_reference_long.csv (standard row-per-parameter)
"""

import csv
import json
from pathlib import Path
from typing import Any, Dict, List, Tuple


ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "refrecnes"

CONFIGS = [
    ("wind_h2", ROOT / "wind" / "hydrogen_storage_config.json"),
    ("solar_h2", ROOT / "solar" / "solar_config_midrange.json"),
    ("gas_ccs", ROOT / "gas" / "gas_ccs_config_midrange.json"),
]

LINKS = {
    "study_assumption": "Assumption in this case study (no external source)",
    "json_config": "https://www.json.org/json-en.html",
    "renewables_ninja": "https://www.renewables.ninja/documentation",
    "vestas_v164": "https://www.vestas.com/en/media/mwow-press-releases/2018/Final-Certification-V164-9-5-MW-Offshore-Turbine",
    "green_book": "https://www.gov.uk/government/publications/the-green-book-appraisal-and-evaluation-in-central-governent/the-green-book-2020",
    "irena_h2": "https://www.irena.org/publications/2020/Dec/Green-hydrogen-cost-reduction",
    "iea_h2": "https://www.iea.org/reports/global-hydrogen-review-2025/executive-summary",
    "h2_power_assumptions": "https://www.gov.uk/government/publications/hydrogen-to-power-cost-and-technical-assumptions",
    "h2_power_barriers": "https://assets.publishing.service.gov.uk/media/69663541e8b93f59c3aecd76/hydrogen-to-power-cost-barriers.pdf",
    "arup_uk_costs": "https://www.arup.com/perspectives/publications/research/section/understanding-the-cost-of-uk-energy",
    "fossil_fuel_assumptions": "https://www.gov.uk/government/publications/fossil-fuel-price-assumptions-2025",
    "electricity_generation_costs": "https://www.gov.uk/government/publications/electricity-generation-costs-2023",
    "h2_salt_cavern": "https://assets.publishing.service.gov.uk/media/68aebcef3a052c9c504c8e60/The_geomechanics_of_hydrogen_storage_in_salt_caverns_-_environmental_considerations_-_report.pdf",
    "nps_en3": "https://www.gov.uk/government/publications/deleted-national-policy-statement-for-renewable-energy-infrastructure-en-3/national-policy-statement-for-renewable-energy-infrastructure-en-3",
    "nrel_pv_degradation": "https://www.nrel.gov/docs/fy24osti/87595.pdf",
}

SPECIAL_RANGES = {
    "demand_mw": "6,000 to 12,000 MW (scenario-dependent)",
    "electricity_to_hydrogen_efficiency": "0.60 to 0.80",
    "hydrogen_to_electricity_efficiency": "0.45 to 0.65",
    "min_end_soc_mwh": "0 to storage capacity",
    "start_fullness_pct": "10 to 100 %",
    "soc_floor_pct": "0 to 30 %",
    "soc_ceiling_pct": "70 to 100 %",
    "wind_stress_factor": "0.70 to 1.10",
    "solar_stress_factor": "0.70 to 1.10",
    "indefinite_check_years": "1 to 30 years",
    "indefinite_soc_convergence_tol_mwh": "1 to 10,000 MWh",
    "indefinite_unmet_tolerance_mwh": "0 to 1 MWh",
    "h2_cyclic_tolerance_mwh": "0 to 10 MWh",
    "simulation_installed_capacity_mw": "> 0 MW (project-specific)",
    "current_installed_capacity_mw": "> 0 MW (project-specific)",
    "max_wind_scale_search": "1 to 500",
    "uk_salt_cavern_working_capacity_tonnes_h2": "3,000 to 6,000 tonnes H2/cavern",
    "turbine_rating_mw": "8 to 15 MW",
    "capex_wind_per_mw": "1.5m to 4.0m GBP/MW",
    "capex_solar_per_mw": "0.5m to 1.0m GBP/MW",
    "capex_electrolyzer_per_mw": "0.6m to 2.6m GBP/MW",
    "capex_h2_turbine_per_mw": "0.7m to 1.8m GBP/MW",
    "capex_storage_per_mwh_h2": "1,000 to 10,000 GBP/MWh-H2",
    "wind_fixed_om_per_mw_year": "30,000 to 120,000 GBP/MW-yr",
    "solar_fixed_om_per_mw_year": "10,000 to 25,000 GBP/MW-yr",
    "electrolyzer_fixed_om_per_mw_year": "10,000 to 30,000 GBP/MW-yr",
    "electrolyzer_variable_om_per_mwh_in": "1 to 10 GBP/MWh",
    "h2_turbine_fixed_om_per_mw_year": "15,000 to 50,000 GBP/MW-yr",
    "h2_turbine_variable_om_per_mwh_out": "2 to 15 GBP/MWh",
    "storage_om_per_mwh_h2_year": "1 to 10 GBP/MWh-H2-yr",
    "electrolyzer_stack_replacement_cost_per_mw": "100,000 to 400,000 GBP/MW",
    "electrolyzer_stack_replacement_interval_years": "5 to 10 years",
    "water_cost_per_kg_h2": "0.05 to 0.50 GBP/kg-H2",
    "compression_and_purification_cost_per_kg_h2": "1.0 to 3.0 GBP/kg-H2",
    "lifecycle_years": "20 to 30 years",
    "project_lifetime_years": "20 to 30 years",
    "project_lifecycle_years": "20 to 30 years",
    "lifecycle_discount_rate": "0.00 to 0.07",
    "discount_rate": "0.00 to 0.07",
    "hours_per_year": "8,760 (non-leap), 8,784 (leap)",
    "load_factor": "0.50 to 0.90",
    "net_heat_rate_mwh_th_per_mwh_e": "1.8 to 2.5",
    "ccs_capture_rate_pct_mid": "90 to 95 %",
    "ccs_energy_penalty_pct_points": "5 to 10 percentage points",
    "co2_transport_storage_cost_gbp_per_tco2_mid": "8 to 31 GBP/tCO2",
    "residual_emissions_carbon_cost_gbp_per_tco2": "20 to 150 GBP/tCO2",
    "natural_gas_emissions_tco2_per_mwh_th": "0.18 to 0.20 tCO2/MWh_th",
    "fixed_om_gbp_per_mw_year_mid": "20,000 to 35,000 GBP/MW-yr",
    "variable_om_gbp_per_mwh_e": "3 to 10 GBP/MWh",
    "ccs_consumables_gbp_per_mwh_e_mid_assumption": "1 to 6 GBP/MWh",
    "major_maintenance_interval_years_assumption": "5 to 15 years",
    "major_maintenance_cost_gbp_per_mw_mid": "50,000 to 150,000 GBP/MW/event",
    "major_maintenance_cost_gbp_per_gw_mid": "50m to 150m GBP/GW/event",
    "major_maintenance_cost_gbp_per_plant_mid": "50m to 150m GBP/plant/event",
    "fuel_price_gbp_per_mwh_th_base": "DESNZ scenario-dependent; roughly 15 to 50 GBP/MWh_th",
    "solar_degradation_pct_per_year": "0.2 to 0.8 %/year",
}


EXCLUDE_BASE_KEYS = {
    # Internal/formatting fields.
    "scenario_name",
    "currency",
    "capex_currency",
    "output_prefix",
    "total_expenditure_output_prefix",
    "optimize_output_prefix",
    "write_timeseries",
    "write_yearly_csv",
    # Solver and numerical-control fields.
    "indefinite_check_years",
    "indefinite_soc_convergence_tol_mwh",
    "indefinite_unmet_tolerance_mwh",
    "require_h2_cyclic_non_depleting",
    "h2_cyclic_tolerance_mwh",
    "min_end_soc_mwh",
    "start_fullness_pct",
    "soc_floor_pct",
    "soc_ceiling_pct",
    "max_wind_scale_search",
    "reservoir_capacity_mwh_h2",
    "plant_count_fractional_for_capacity_calc",
    "round_plant_count_for_maintenance_up",
}

EXCLUDE_PATH_SNIPPETS = (
    "optimize_",
    "notes",
    "source_links",
    "major_maintenance_source_links",
)


def flatten(obj: Any, prefix: str = "") -> List[Tuple[str, Any]]:
    out: List[Tuple[str, Any]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else k
            out.extend(flatten(v, p))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            p = f"{prefix}[{i}]"
            out.extend(flatten(v, p))
    else:
        out.append((prefix, obj))
    return out


def base_key(path: str) -> str:
    key = path.split(".")[-1]
    if "[" in key:
        return key.split("[")[0]
    return key


def value_str(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if v is None:
        return "null"
    if isinstance(v, (int, float)):
        return str(v)
    return str(v)


def numeric(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def include_parameter(key_path: str) -> bool:
    low_path = key_path.lower()
    bkey = base_key(key_path).lower()

    if bkey in EXCLUDE_BASE_KEYS:
        return False
    if any(snippet in low_path for snippet in EXCLUDE_PATH_SNIPPETS):
        return False
    return True


SPECIAL_DESCRIPTIONS = {
    "csv": "Input hourly generation profile CSV path",
    "scenario_name": "Scenario label used in outputs",
    "currency": "Currency label for cost reporting",
    "capex_currency": "Currency label for CAPEX reporting",
    "demand_mw": "Flat electricity demand target",
    "electricity_to_hydrogen_efficiency": "Electrolyzer conversion efficiency (electricity to H2)",
    "hydrogen_to_electricity_efficiency": "Power-block conversion efficiency (H2 to electricity)",
    "min_end_soc_mwh": "Minimum end-of-year storage energy requirement",
    "start_fullness_pct": "Initial storage state-of-charge percentage",
    "soc_floor_pct": "Minimum allowed state-of-charge percentage",
    "soc_ceiling_pct": "Maximum allowed state-of-charge percentage",
    "wind_stress_factor": "Scaling factor applied to wind profile output",
    "solar_stress_factor": "Scaling factor applied to solar profile output",
    "indefinite_check_years": "Years simulated for repeated-year feasibility check",
    "indefinite_soc_convergence_tol_mwh": "SOC convergence tolerance across repeated years",
    "indefinite_unmet_tolerance_mwh": "Allowed unmet load tolerance in feasibility checks",
    "require_h2_cyclic_non_depleting": "Require final SOC to be non-depleting vs initial SOC",
    "h2_cyclic_tolerance_mwh": "Tolerance for cyclic non-depleting SOC check",
    "simulation_installed_capacity_mw": "Installed capacity represented by profile scaling basis",
    "current_installed_capacity_mw": "Current installed capacity assumption",
    "reservoir_capacity_mwh_h2": "Fixed reservoir capacity override (if provided)",
    "max_wind_scale_search": "Upper bound for wind scaling during sizing searches",
    "uk_salt_cavern_working_capacity_tonnes_h2": "Assumed working hydrogen per UK salt cavern",
    "turbine_rating_mw": "Single turbine nameplate rating",
    "output_prefix": "Output filename prefix",
    "write_timeseries": "Write hourly dispatch/storage timeseries output",
    "lifecycle_years": "Economic analysis horizon in years",
    "project_lifecycle_years": "Economic analysis horizon in years",
    "project_lifetime_years": "Project lifetime assumption in years",
    "lifecycle_discount_rate": "Discount rate for lifecycle costing",
    "discount_rate": "Discount rate for lifecycle costing",
    "hours_per_year": "Operating hours assumed per year",
    "load_factor": "Plant utilization/load factor assumption",
    "required_installed_capacity_mw": "Installed capacity required to meet target load factor",
    "plant_unit_capacity_mw": "Nominal capacity of one plant unit",
    "plant_count_nominal": "Nominal number of plant units",
    "plant_count_fractional_for_capacity_calc": "Fractional plant count used for capacity arithmetic",
    "capex_wind_per_mw": "Wind CAPEX intensity per MW",
    "capex_solar_per_mw": "Solar CAPEX intensity per MW",
    "capex_electrolyzer_per_mw": "Electrolyzer CAPEX intensity per MW",
    "capex_h2_turbine_per_mw": "Hydrogen turbine CAPEX intensity per MW",
    "capex_storage_per_mwh_h2": "Hydrogen storage CAPEX intensity per MWh-H2",
    "capital_cost_total_gbp_mid": "Total gas+CCS CAPEX (mid assumption)",
    "capital_cost_total_gbp_low": "Total gas+CCS CAPEX (low assumption)",
    "capital_cost_total_gbp_high": "Total gas+CCS CAPEX (high assumption)",
    "capital_cost_gbp_per_gw_mid": "Gas+CCS CAPEX intensity per GW (mid assumption)",
    "capital_cost_gbp_per_1p8gw_plant_mid": "Gas+CCS CAPEX per 1.8 GW plant (mid assumption)",
    "wind_fixed_om_per_mw_year": "Wind fixed O&M intensity per MW-year",
    "solar_fixed_om_per_mw_year": "Solar fixed O&M intensity per MW-year",
    "electrolyzer_fixed_om_per_mw_year": "Electrolyzer fixed O&M intensity per MW-year",
    "electrolyzer_variable_om_per_mwh_in": "Electrolyzer variable O&M per MWh input",
    "h2_turbine_fixed_om_per_mw_year": "Hydrogen turbine fixed O&M intensity per MW-year",
    "h2_turbine_variable_om_per_mwh_out": "Hydrogen turbine variable O&M per MWh output",
    "storage_om_per_mwh_h2_year": "Hydrogen storage O&M per MWh-H2-year",
    "electrolyzer_stack_replacement_cost_per_mw": "Electrolyzer stack replacement cost per MW",
    "electrolyzer_stack_replacement_interval_years": "Electrolyzer stack replacement interval",
    "water_cost_per_kg_h2": "Water cost per kg of hydrogen produced",
    "compression_and_purification_cost_per_kg_h2": "Compression and purification cost per kg H2",
    "fuel_price_gbp_per_mwh_th_base": "Base gas fuel price assumption",
    "net_heat_rate_mwh_th_per_mwh_e": "Thermal energy input per electric output",
    "ccs_capture_rate_pct_mid": "CO2 capture rate (mid assumption)",
    "ccs_capture_rate_pct_low": "CO2 capture rate (low assumption)",
    "ccs_capture_rate_pct_high": "CO2 capture rate (high assumption)",
    "ccs_energy_penalty_pct_points": "CCS efficiency penalty in percentage points",
    "co2_transport_storage_cost_gbp_per_tco2_mid": "CO2 transport and storage cost (mid)",
    "co2_transport_storage_cost_gbp_per_tco2_low": "CO2 transport and storage cost (low)",
    "co2_transport_storage_cost_gbp_per_tco2_high": "CO2 transport and storage cost (high)",
    "residual_emissions_carbon_cost_gbp_per_tco2": "Carbon cost on residual uncaptured emissions",
    "natural_gas_emissions_tco2_per_mwh_th": "Gas combustion emissions factor",
    "fixed_om_gbp_per_mw_year_mid": "Gas+CCS fixed O&M intensity (mid)",
    "fixed_om_gbp_per_mw_year_low": "Gas+CCS fixed O&M intensity (low)",
    "fixed_om_gbp_per_mw_year_high": "Gas+CCS fixed O&M intensity (high)",
    "variable_om_gbp_per_mwh_e": "Gas+CCS variable O&M per MWh",
    "ccs_consumables_gbp_per_mwh_e_mid_assumption": "CCS consumables cost per MWh (mid)",
    "major_maintenance_interval_years_assumption": "Years between major maintenance events",
    "major_maintenance_cost_basis": "Basis for major maintenance costing",
    "major_maintenance_cost_gbp_per_mw_mid": "Major maintenance cost per MW event (mid)",
    "major_maintenance_cost_gbp_per_gw_mid": "Major maintenance cost per GW event (mid)",
    "major_maintenance_cost_gbp_per_plant_mid": "Major maintenance cost per plant event (mid)",
    "round_plant_count_for_maintenance_up": "Round plant count upward for maintenance costing",
    "solar_degradation_pct_per_year": "Annual solar output degradation rate",
}


def infer_description(label: str, key_path: str, value: Any) -> str:
    bkey = base_key(key_path)
    low_path = key_path.lower()

    if bkey in SPECIAL_DESCRIPTIONS:
        return SPECIAL_DESCRIPTIONS[bkey]

    if "fuel_price_scenarios_gbp_per_mwh_th." in low_path:
        return f"Gas fuel price scenario value ({bkey})"

    if low_path.startswith("source_links") or "major_maintenance_source_links" in low_path:
        return "Source URL for a specific assumption"
    if low_path.startswith("notes"):
        return "Free-text modelling note"
    if low_path.startswith("optimize_"):
        if "_min_" in bkey:
            return "Minimum bound for parameter search"
        if "_max_" in bkey:
            return "Maximum bound for parameter search"
        if "_step_" in bkey:
            return "Step size for parameter search"
        return "Optimization control parameter"
    if "tolerance" in low_path:
        return "Numerical tolerance used by the solver/feasibility checks"
    if "output_prefix" in low_path:
        return "Output filename prefix"
    if isinstance(value, bool):
        return "Boolean model switch"
    if value is None:
        return "Optional override; null means use computed/default value"
    if numeric(value):
        return "Numeric model input parameter"
    return "Text/path model input parameter"


def infer_range(label: str, key_path: str, value: Any, root: Dict[str, Any]) -> str:
    bkey = base_key(key_path)

    # Direct special map.
    if bkey in SPECIAL_RANGES:
        return SPECIAL_RANGES[bkey]

    # Scenario dict values (e.g. fuel low/base/high).
    if "fuel_price_scenarios_gbp_per_mwh_th." in key_path and isinstance(root.get("fuel_price_scenarios_gbp_per_mwh_th"), dict):
        vals = [float(v) for v in root["fuel_price_scenarios_gbp_per_mwh_th"].values()]
        return f"{min(vals)} to {max(vals)} GBP/MWh_th"

    # Mid values with low/high siblings in same root.
    if bkey.endswith("_mid"):
        stem = bkey[:-4]
        low_k = f"{stem}_low"
        high_k = f"{stem}_high"
        if low_k in root and high_k in root and numeric(root[low_k]) and numeric(root[high_k]):
            return f"{root[low_k]} to {root[high_k]}"

    # Base with sibling low/high.
    if bkey.endswith("_base"):
        stem = bkey[:-5]
        low_k = f"{stem}_low"
        high_k = f"{stem}_high"
        if low_k in root and high_k in root and numeric(root[low_k]) and numeric(root[high_k]):
            return f"{root[low_k]} to {root[high_k]}"

    # Config/control keys.
    low_path = key_path.lower()
    if any(s in low_path for s in ["output_prefix", "scenario_name", "currency", "write_", "csv"]):
        return "Model/config input (project-specific)"
    if low_path.startswith("notes"):
        return "Free-text assumption"
    if "source_links" in low_path:
        return "Valid HTTPS URL"
    if low_path.startswith("optimize_"):
        return "User-defined search bound/step"
    if "tolerance" in low_path:
        return ">= 0 (solver setting)"

    # Type-based fallback.
    if isinstance(value, bool):
        return "{true,false}"
    if value is None:
        return "null or numeric override"
    if numeric(value):
        return "Project-specific numeric assumption"
    return "Project-specific text/path assumption"


def infer_citation(label: str, key_path: str, value: Any) -> str:
    bkey = base_key(key_path)
    low_path = key_path.lower()

    if "source_links" in low_path or "major_maintenance_source_links" in low_path:
        return str(value)

    if "fuel_price_scenarios_gbp_per_mwh_th." in low_path:
        return LINKS["fossil_fuel_assumptions"]
    if "major_maintenance_reference_costs" in low_path:
        return LINKS["h2_power_assumptions"]

    if bkey == "csv":
        return LINKS["renewables_ninja"]
    if bkey == "turbine_rating_mw":
        return LINKS["vestas_v164"]
    if bkey in {
        "demand_mw",
        "wind_stress_factor",
        "solar_stress_factor",
        "simulation_installed_capacity_mw",
        "current_installed_capacity_mw",
        "required_installed_capacity_mw",
        "plant_unit_capacity_mw",
        "plant_count_nominal",
        "hours_per_year",
    }:
        return LINKS["study_assumption"]
    if bkey in {"electricity_to_hydrogen_efficiency", "hydrogen_to_electricity_efficiency"}:
        return LINKS["irena_h2"] if bkey.startswith("electricity") else LINKS["h2_power_assumptions"]
    if bkey in {
        "capex_electrolyzer_per_mw",
        "electrolyzer_fixed_om_per_mw_year",
        "electrolyzer_variable_om_per_mwh_in",
        "electrolyzer_stack_replacement_cost_per_mw",
        "electrolyzer_stack_replacement_interval_years",
    }:
        return LINKS["iea_h2"]
    if bkey in {
        "capex_h2_turbine_per_mw",
        "h2_turbine_fixed_om_per_mw_year",
        "h2_turbine_variable_om_per_mwh_out",
        "compression_and_purification_cost_per_kg_h2",
        "water_cost_per_kg_h2",
        "ccs_consumables_gbp_per_mwh_e_mid_assumption",
        "major_maintenance_cost_gbp_per_mw_mid",
        "major_maintenance_cost_gbp_per_gw_mid",
        "major_maintenance_cost_gbp_per_plant_mid",
    }:
        return LINKS["h2_power_assumptions"]
    if bkey in {
        "uk_salt_cavern_working_capacity_tonnes_h2",
        "capex_storage_per_mwh_h2",
        "storage_om_per_mwh_h2_year",
        "reservoir_capacity_mwh_h2",
    }:
        return LINKS["h2_salt_cavern"]
    if bkey in {
        "capex_wind_per_mw",
        "wind_fixed_om_per_mw_year",
        "capex_solar_per_mw",
        "solar_fixed_om_per_mw_year",
    }:
        return LINKS["arup_uk_costs"]
    if bkey == "solar_degradation_pct_per_year":
        return LINKS["nrel_pv_degradation"]
    if "fuel_price" in bkey:
        return LINKS["fossil_fuel_assumptions"]
    if bkey in {
        "net_heat_rate_mwh_th_per_mwh_e",
        "fixed_om_gbp_per_mw_year_mid",
        "fixed_om_gbp_per_mw_year_low",
        "fixed_om_gbp_per_mw_year_high",
        "variable_om_gbp_per_mwh_e",
        "capital_cost_total_gbp_mid",
        "capital_cost_total_gbp_low",
        "capital_cost_total_gbp_high",
        "capital_cost_gbp_per_gw_mid",
        "capital_cost_gbp_per_1p8gw_plant_mid",
        "load_factor",
    }:
        return LINKS["electricity_generation_costs"]
    if bkey.startswith("ccs_") or bkey.startswith("co2_") or bkey in {
        "natural_gas_emissions_tco2_per_mwh_th",
        "residual_emissions_carbon_cost_gbp_per_tco2",
    }:
        return LINKS["h2_power_barriers"]
    if bkey in {
        "major_maintenance_interval_years_assumption",
        "major_maintenance_cost_basis",
    }:
        return LINKS["h2_power_assumptions"]
    if bkey in {"discount_rate", "lifecycle_discount_rate"}:
        return LINKS["green_book"]
    if bkey in {"start_fullness_pct", "soc_floor_pct", "soc_ceiling_pct", "min_end_soc_mwh"}:
        return LINKS["study_assumption"]
    if bkey in {"project_lifecycle_years", "lifecycle_years", "project_lifetime_years"}:
        return LINKS["green_book"]
    if bkey in {"indefinite_check_years"}:
        return LINKS["study_assumption"]

    if "optimize_" in low_path or "tolerance" in low_path or "output_prefix" in low_path:
        return LINKS["study_assumption"]
    if "notes" in low_path:
        return LINKS["json_config"]

    return LINKS["study_assumption"]


def build_rows() -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []

    for label, path in CONFIGS:
        payload = json.loads(path.read_text())
        leaves = flatten(payload)
        for kp, val in leaves:
            if not include_parameter(kp):
                continue
            pname = f"{label}.{kp}"
            rows.append(
                {
                    "parameter_name": pname,
                    "parameter_description": infer_description(label, kp, val),
                    "value_used": value_str(val),
                    "likely_range": infer_range(label, kp, val, payload),
                    "citation": infer_citation(label, kp, val),
                }
            )

    return rows


def write_long(path: Path, rows: List[Dict[str, str]]) -> None:
    fieldnames = [
        "parameter_name",
        "parameter_description",
        "value_used",
        "likely_range",
        "citation",
    ]
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)


def write_matrix(path: Path, rows: List[Dict[str, str]]) -> None:
    params = [r["parameter_name"] for r in rows]
    descriptions = [r["parameter_description"] for r in rows]
    values = [r["value_used"] for r in rows]
    ranges = [r["likely_range"] for r in rows]
    cites = [r["citation"] for r in rows]

    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["parameter_name", *params])
        w.writerow(["parameter_description", *descriptions])
        w.writerow(["value_used", *values])
        w.writerow(["likely_range", *ranges])
        w.writerow(["citation", *cites])


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows = build_rows()

    long_path = OUT_DIR / "config_parameter_reference_long.csv"
    matrix_path = OUT_DIR / "config_parameter_reference_matrix.csv"

    write_long(long_path, rows)
    write_matrix(matrix_path, rows)

    print(f"rows={len(rows)}")
    print(f"long_csv={long_path}")
    print(f"matrix_csv={matrix_path}")


if __name__ == "__main__":
    main()
