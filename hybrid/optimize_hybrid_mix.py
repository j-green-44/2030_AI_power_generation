#!/usr/bin/env python3
"""
Optimize hybrid mix of wind+H2, solar+H2, and gas+CCS for minimum total expenditure.

Method:
- Uses existing subsystem optimization outputs as calibrated baselines.
- Runs a share-grid experiment over demand allocation across:
  - wind + H2
  - solar + H2
  - gas + CCS
- Assumes linear scaling of subsystem CAPEX and lifecycle OPEX with allocated demand.
"""

import argparse
import csv
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path


DEFAULTS = {
    "currency": "GBP_2026_placeholder",
    "lifecycle_years": 25,
    "total_demand_mw": 8200.0,
    "share_step": 0.05,
    "wind_h2_min_share": 0.0,
    "wind_h2_max_share": 1.0,
    "solar_h2_min_share": 0.0,
    "solar_h2_max_share": 1.0,
    "gas_ccs_min_share": 0.0,
    "gas_ccs_max_share": 1.0,
    "output_prefix": "hybrid/hybrid_mix_opt",
}


@dataclass
class TechBaseline:
    name: str
    demand_mw_base: float
    lifecycle_years_base: int
    capex_total_base: float
    opex_total_base: float
    components: list
    design_scalars: dict


def load_config(path: Path):
    with path.open() as f:
        loaded = json.load(f)
    if not isinstance(loaded, dict):
        raise ValueError("Config file must contain a JSON object.")
    cfg = DEFAULTS.copy()
    cfg.update(loaded)
    return cfg


def frange(start, stop, step):
    if step <= 0:
        raise ValueError("share_step must be > 0.")
    vals = []
    x = float(start)
    stop = float(stop)
    while x <= stop + 1e-12:
        vals.append(float(f"{x:.10f}"))
        x += step
    if not vals or abs(vals[-1] - stop) > 1e-9:
        vals.append(stop)
    return vals


def parse_amount_and_unit(label):
    m = re.match(r"^\s*([+-]?\d+(?:\.\d+)?)\s*(.*)\s*$", str(label))
    if not m:
        return None, None
    return float(m.group(1)), m.group(2)


def scale_label(label, factor):
    value, unit = parse_amount_and_unit(label)
    if value is None:
        return str(label)
    return f"{value * factor:.2f} {unit}".strip()


def load_component_table(path: Path, tech_name: str):
    rows = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        required = {
            "design_component",
            "best_design_(MW/MWh-H2)",
            "capex_for_best_design",
            "opex_over_25_years_for_best_design",
        }
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"{path} missing columns: {sorted(missing)}")
        for row in reader:
            comp = str(row["design_component"]).strip()
            if comp.upper() == "TOTAL":
                continue
            rows.append(
                {
                    "design_component": f"{tech_name}:{comp}",
                    "base_design_label": str(row["best_design_(MW/MWh-H2)"]).strip(),
                    "capex_base": float(row["capex_for_best_design"]),
                    "opex_base": float(row["opex_over_25_years_for_best_design"]),
                }
            )
    if not rows:
        raise ValueError(f"No non-TOTAL component rows in {path}")
    return rows


def load_wind_or_solar_baseline(
    tech_name: str,
    summary_path: Path,
    component_path: Path,
):
    with summary_path.open() as f:
        summary = json.load(f)

    inputs = summary["inputs"]
    best = summary["best_design"]
    if best is None:
        raise ValueError(f"{summary_path} has no best_design.")

    components = load_component_table(component_path, tech_name)

    demand_base = float(inputs["demand_mw"])
    lifecycle_base = int(inputs["lifecycle_years"])
    capex_total = float(best["capex_total"])
    opex_total = float(best["lifecycle_opex_undiscounted"])

    design_scalars = {}
    for k, unit in [
        ("wind_mw", "MW"),
        ("solar_mw", "MW"),
        ("electrolyzer_mw", "MW"),
        ("h2_turbine_mw", "MW"),
        ("storage_mwh_h2", "MWh-H2"),
    ]:
        if k in best:
            design_scalars[k] = {"value": float(best[k]), "unit": unit}

    return TechBaseline(
        name=tech_name,
        demand_mw_base=demand_base,
        lifecycle_years_base=lifecycle_base,
        capex_total_base=capex_total,
        opex_total_base=opex_total,
        components=components,
        design_scalars=design_scalars,
    )


def load_gas_baseline(summary_path: Path):
    with summary_path.open() as f:
        summary = json.load(f)

    inputs = summary["inputs"]
    selected = summary["selected_scenario_results"]
    annual = selected["annual_base_components"]

    demand_base = float(inputs["demand_mw"])
    lifecycle_base = int(inputs["project_lifecycle_years"])
    capex_total = float(selected["capex_gbp"])
    opex_total = float(selected["total_opex_gbp"])

    maintenance_interval = int(inputs["major_maintenance_interval_years_assumption"])
    maintenance_events = lifecycle_base // maintenance_interval
    maintenance_event_cost = float(inputs["maintenance_event_cost_gbp"])

    components = [
        {
            "design_component": "gas_ccs:fleet_capex",
            "base_design_label": f"{float(inputs['required_installed_capacity_mw']):.2f} MW",
            "capex_base": capex_total,
            "opex_base": 0.0,
        },
        {
            "design_component": "gas_ccs:fuel",
            "base_design_label": "",
            "capex_base": 0.0,
            "opex_base": float(annual["fuel_cost_gbp"]) * lifecycle_base,
        },
        {
            "design_component": "gas_ccs:fixed_om",
            "base_design_label": "",
            "capex_base": 0.0,
            "opex_base": float(annual["fixed_om_gbp"]) * lifecycle_base,
        },
        {
            "design_component": "gas_ccs:variable_om",
            "base_design_label": "",
            "capex_base": 0.0,
            "opex_base": float(annual["variable_om_gbp"]) * lifecycle_base,
        },
        {
            "design_component": "gas_ccs:ccs_consumables",
            "base_design_label": "",
            "capex_base": 0.0,
            "opex_base": float(annual["ccs_consumables_gbp"]) * lifecycle_base,
        },
        {
            "design_component": "gas_ccs:co2_transport_storage",
            "base_design_label": "",
            "capex_base": 0.0,
            "opex_base": float(annual["co2_t_and_s_cost_gbp"]) * lifecycle_base,
        },
        {
            "design_component": "gas_ccs:residual_carbon",
            "base_design_label": "",
            "capex_base": 0.0,
            "opex_base": float(annual["residual_carbon_cost_gbp"]) * lifecycle_base,
        },
        {
            "design_component": "gas_ccs:major_maintenance",
            "base_design_label": "",
            "capex_base": 0.0,
            "opex_base": maintenance_event_cost * maintenance_events,
        },
    ]

    design_scalars = {
        "required_installed_capacity_mw": {
            "value": float(inputs["required_installed_capacity_mw"]),
            "unit": "MW",
        }
    }

    return TechBaseline(
        name="gas_ccs",
        demand_mw_base=demand_base,
        lifecycle_years_base=lifecycle_base,
        capex_total_base=capex_total,
        opex_total_base=opex_total,
        components=components,
        design_scalars=design_scalars,
    )


def validate(cfg, wind_base, solar_base, gas_base):
    step = float(cfg["share_step"])
    if step <= 0 or step > 1:
        raise ValueError("share_step must be in (0, 1].")

    total_demand = float(cfg["total_demand_mw"])
    if total_demand <= 0:
        raise ValueError("total_demand_mw must be > 0.")

    lifecycle_years = int(cfg["lifecycle_years"])
    if lifecycle_years < 1:
        raise ValueError("lifecycle_years must be >= 1.")

    for label, mn_key, mx_key in [
        ("wind_h2", "wind_h2_min_share", "wind_h2_max_share"),
        ("solar_h2", "solar_h2_min_share", "solar_h2_max_share"),
        ("gas_ccs", "gas_ccs_min_share", "gas_ccs_max_share"),
    ]:
        mn = float(cfg[mn_key])
        mx = float(cfg[mx_key])
        if mn < 0 or mn > 1 or mx < 0 or mx > 1:
            raise ValueError(f"{label} share bounds must be within [0, 1].")
        if mn > mx:
            raise ValueError(f"{label} min share must be <= max share.")

    if lifecycle_years != int(wind_base.lifecycle_years_base):
        raise ValueError("lifecycle_years must match wind summary lifecycle_years.")
    if lifecycle_years != int(solar_base.lifecycle_years_base):
        raise ValueError("lifecycle_years must match solar summary lifecycle_years.")
    if lifecycle_years != int(gas_base.lifecycle_years_base):
        raise ValueError("lifecycle_years must match gas summary project_lifecycle_years.")


def scale_tech_costs(tech: TechBaseline, allocated_demand_mw: float):
    factor = allocated_demand_mw / tech.demand_mw_base
    capex = tech.capex_total_base * factor
    opex = tech.opex_total_base * factor
    return factor, capex, opex


def build_best_component_rows(best, baselines):
    rows = []
    scale_map = {
        "wind_h2": best["wind_h2_scale"],
        "solar_h2": best["solar_h2_scale"],
        "gas_ccs": best["gas_ccs_scale"],
    }

    for tech_name, baseline in baselines.items():
        factor = scale_map[tech_name]
        for comp in baseline.components:
            rows.append(
                {
                    "design_component": comp["design_component"],
                    "best_design_(MW/MWh-H2)": scale_label(comp["base_design_label"], factor),
                    "capex_for_best_design": comp["capex_base"] * factor,
                    "opex_over_25_years_for_best_design": comp["opex_base"] * factor,
                }
            )

    rows.append(
        {
            "design_component": "TOTAL",
            "best_design_(MW/MWh-H2)": "",
            "capex_for_best_design": sum(r["capex_for_best_design"] for r in rows),
            "opex_over_25_years_for_best_design": sum(
                r["opex_over_25_years_for_best_design"] for r in rows
            ),
        }
    )
    return rows


def write_component_csv(path: Path, rows):
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


def write_experiments_csv(path: Path, rows):
    if not rows:
        with path.open("w", newline="") as f:
            f.write("")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(
        description="Optimize demand-share mix across wind+H2, solar+H2, and gas+CCS."
    )
    parser.add_argument(
        "--config",
        default="hybrid/hybrid_mix_config.json",
        help="Path to hybrid mix config JSON.",
    )
    parser.add_argument(
        "--output-prefix",
        help="Optional override for output prefix.",
    )
    args = parser.parse_args()

    cfg_path = Path(args.config)
    cfg = load_config(cfg_path)

    wind_base = load_wind_or_solar_baseline(
        tech_name="wind_h2",
        summary_path=Path(cfg["wind_h2_summary_json"]),
        component_path=Path(cfg["wind_h2_component_csv"]),
    )
    solar_base = load_wind_or_solar_baseline(
        tech_name="solar_h2",
        summary_path=Path(cfg["solar_h2_summary_json"]),
        component_path=Path(cfg["solar_h2_component_csv"]),
    )
    gas_base = load_gas_baseline(Path(cfg["gas_ccs_summary_json"]))

    validate(cfg, wind_base, solar_base, gas_base)

    total_demand = float(cfg["total_demand_mw"])
    share_step = float(cfg["share_step"])

    wind_vals = frange(float(cfg["wind_h2_min_share"]), float(cfg["wind_h2_max_share"]), share_step)
    solar_vals = frange(float(cfg["solar_h2_min_share"]), float(cfg["solar_h2_max_share"]), share_step)

    gas_min = float(cfg["gas_ccs_min_share"])
    gas_max = float(cfg["gas_ccs_max_share"])

    experiments = []
    best = None
    eps = 1e-9

    for wind_share in wind_vals:
        for solar_share in solar_vals:
            gas_share = 1.0 - wind_share - solar_share
            if gas_share < -eps:
                continue
            gas_share = max(0.0, gas_share)

            if gas_share < gas_min - eps or gas_share > gas_max + eps:
                continue

            wind_demand = total_demand * wind_share
            solar_demand = total_demand * solar_share
            gas_demand = total_demand * gas_share

            wind_scale, wind_capex, wind_opex = scale_tech_costs(wind_base, wind_demand)
            solar_scale, solar_capex, solar_opex = scale_tech_costs(solar_base, solar_demand)
            gas_scale, gas_capex, gas_opex = scale_tech_costs(gas_base, gas_demand)

            total_capex = wind_capex + solar_capex + gas_capex
            total_opex = wind_opex + solar_opex + gas_opex
            total_expenditure = total_capex + total_opex

            row = {
                "wind_h2_share": wind_share,
                "solar_h2_share": solar_share,
                "gas_ccs_share": gas_share,
                "wind_h2_demand_mw": wind_demand,
                "solar_h2_demand_mw": solar_demand,
                "gas_ccs_demand_mw": gas_demand,
                "wind_h2_capex_gbp": wind_capex,
                "wind_h2_opex_25y_gbp": wind_opex,
                "solar_h2_capex_gbp": solar_capex,
                "solar_h2_opex_25y_gbp": solar_opex,
                "gas_ccs_capex_gbp": gas_capex,
                "gas_ccs_opex_25y_gbp": gas_opex,
                "total_capex_gbp": total_capex,
                "total_opex_25y_gbp": total_opex,
                "total_expenditure_25y_gbp": total_expenditure,
            }
            experiments.append(row)

            if best is None or total_expenditure < best["total_expenditure_25y_gbp"]:
                best = dict(row)
                best["wind_h2_scale"] = wind_scale
                best["solar_h2_scale"] = solar_scale
                best["gas_ccs_scale"] = gas_scale

    if best is None:
        raise ValueError(
            "No valid experiments were generated. "
            "Check share bounds and share_step in hybrid config."
        )

    output_prefix = Path(args.output_prefix) if args.output_prefix else Path(cfg["output_prefix"])
    summary_path = Path(f"{output_prefix}_summary.json")
    experiments_path = Path(f"{output_prefix}_experiments.csv")
    components_path = Path(f"{output_prefix}_component_cost_table.csv")

    write_experiments_csv(experiments_path, experiments)

    baselines = {
        "wind_h2": wind_base,
        "solar_h2": solar_base,
        "gas_ccs": gas_base,
    }
    component_rows = build_best_component_rows(best, baselines)
    write_component_csv(components_path, component_rows)

    summary = {
        "inputs": {
            "config_file": str(cfg_path),
            "scenario_name": cfg.get("scenario_name"),
            "currency": cfg["currency"],
            "lifecycle_years": int(cfg["lifecycle_years"]),
            "total_demand_mw": total_demand,
            "share_step": share_step,
            "wind_h2_min_share": float(cfg["wind_h2_min_share"]),
            "wind_h2_max_share": float(cfg["wind_h2_max_share"]),
            "solar_h2_min_share": float(cfg["solar_h2_min_share"]),
            "solar_h2_max_share": float(cfg["solar_h2_max_share"]),
            "gas_ccs_min_share": float(cfg["gas_ccs_min_share"]),
            "gas_ccs_max_share": float(cfg["gas_ccs_max_share"]),
            "wind_h2_summary_json": cfg["wind_h2_summary_json"],
            "solar_h2_summary_json": cfg["solar_h2_summary_json"],
            "gas_ccs_summary_json": cfg["gas_ccs_summary_json"],
            "method": (
                "share-grid search with linear scaling of calibrated subsystem "
                "CAPEX and lifecycle OPEX versus allocated demand"
            ),
        },
        "search_stats": {
            "evaluated_experiments": len(experiments),
        },
        "best_mix": {
            "wind_h2_share": best["wind_h2_share"],
            "solar_h2_share": best["solar_h2_share"],
            "gas_ccs_share": best["gas_ccs_share"],
            "wind_h2_demand_mw": best["wind_h2_demand_mw"],
            "solar_h2_demand_mw": best["solar_h2_demand_mw"],
            "gas_ccs_demand_mw": best["gas_ccs_demand_mw"],
            "total_capex_gbp": best["total_capex_gbp"],
            "total_opex_25y_gbp": best["total_opex_25y_gbp"],
            "total_expenditure_25y_gbp": best["total_expenditure_25y_gbp"],
        },
        "outputs": {
            "experiments_csv": str(experiments_path),
            "component_cost_table_csv": str(components_path),
        },
    }

    with summary_path.open("w") as f:
        json.dump(summary, f, indent=2)

    print(f"Summary written: {summary_path}")
    print(f"Experiments written: {experiments_path}")
    print(f"Component cost table written: {components_path}")
    print(f"Evaluated experiments: {len(experiments)}")
    print(
        "Best mix shares (wind_h2, solar_h2, gas_ccs): "
        f"{best['wind_h2_share']:.3f}, {best['solar_h2_share']:.3f}, {best['gas_ccs_share']:.3f}"
    )
    print(f"Best total CAPEX (GBP): {best['total_capex_gbp']:.2f}")
    print(f"Best total OPEX over 25y (GBP): {best['total_opex_25y_gbp']:.2f}")
    print(f"Best total expenditure over 25y (GBP): {best['total_expenditure_25y_gbp']:.2f}")


if __name__ == "__main__":
    main()
