#!/usr/bin/env python3
"""
Create an SVG plot for:
- Wind farm production (MW)
- Flat AI load (MW)
- Electrolyzer power draw (MW) when wind > load
- Hydrogen turbine output (MW) when wind < load
"""

import argparse
import csv
import json
from datetime import datetime
from pathlib import Path


def load_config(path: Path):
    with path.open() as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict):
        raise ValueError("Config file must be a JSON object.")
    return cfg


def parse_float(value):
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    return float(text)


def resolve_csv_path(input_path: Path):
    if input_path.suffix.lower() != ".json":
        return input_path

    with input_path.open() as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"{input_path} is JSON but not an object.")

    csv_from_summary = (
        payload.get("inputs", {}).get("csv")
        if isinstance(payload.get("inputs"), dict)
        else None
    )
    if not csv_from_summary:
        raise ValueError(
            f"{input_path} looks like a summary JSON; cannot find inputs.csv to resolve source data."
        )

    resolved = input_path.parent / csv_from_summary
    return resolved


def load_wind_series(csv_path: Path):
    times = []
    wind_mw = []
    demand_series = []
    charge_actual_h2_mwh = []
    discharge_actual_h2_mwh = []
    soc_end_pct = []

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
        demand_col = lower_map.get("demand_mw")
        charge_actual_col = lower_map.get("charge_h2_mwh_actual")
        discharge_actual_col = lower_map.get("discharge_h2_mwh_actual")
        soc_end_pct_col = lower_map.get("soc_end_pct")
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

            times.append(datetime.strptime(norm[time_col], "%Y-%m-%d %H:%M"))
            if power_col.lower() == "electricity":
                wind_mw.append(float(norm[power_col]) / 1000.0)  # kW -> MW
            else:
                wind_mw.append(float(norm[power_col]))  # already MW

            demand_series.append(parse_float(norm.get(demand_col)) if demand_col else None)
            charge_actual_h2_mwh.append(
                parse_float(norm.get(charge_actual_col)) if charge_actual_col else None
            )
            discharge_actual_h2_mwh.append(
                parse_float(norm.get(discharge_actual_col)) if discharge_actual_col else None
            )
            soc_end_pct.append(parse_float(norm.get(soc_end_pct_col)) if soc_end_pct_col else None)

    if not wind_mw:
        raise ValueError("No data rows found in CSV.")
    return {
        "times": times,
        "wind_mw": wind_mw,
        "demand_mw": demand_series,
        "charge_h2_mwh_actual": charge_actual_h2_mwh,
        "discharge_h2_mwh_actual": discharge_actual_h2_mwh,
        "soc_end_pct": soc_end_pct,
    }


def resolve_efficiencies(cfg):
    eta_charge = cfg.get("electricity_to_hydrogen_efficiency", cfg.get("eta_charge", 1.0))
    eta_discharge = cfg.get("hydrogen_to_electricity_efficiency", cfg.get("eta_discharge", 1.0))
    eta_charge = float(eta_charge)
    eta_discharge = float(eta_discharge)
    if eta_charge <= 0 or eta_charge > 1:
        raise ValueError("electricity_to_hydrogen_efficiency must be in (0, 1].")
    if eta_discharge <= 0 or eta_discharge > 1:
        raise ValueError("hydrogen_to_electricity_efficiency must be in (0, 1].")
    return eta_charge, eta_discharge


def load_reservoir_capacity_from_summary(summary_path: Path):
    if not summary_path.exists():
        return None
    with summary_path.open() as f:
        payload = json.load(f)
    return float(payload["hydrogen_sizing"]["total_reservoir_capacity_for_end_reserve_mwh_h2"])


def make_polyline(x_vals, y_vals):
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(x_vals, y_vals))


def is_complete_series(values):
    return bool(values) and all(v is not None for v in values)


def main():
    parser = argparse.ArgumentParser(
        description="Plot wind/load/electrolyzer/hydrogen-turbine power as an SVG."
    )
    parser.add_argument(
        "--config",
        default="hydrogen_storage_config.json",
        help="Path to JSON config file (default: hydrogen_storage_config.json).",
    )
    parser.add_argument("--csv", help="Optional CSV override.")
    parser.add_argument("--demand-mw", type=float, help="Optional demand override.")
    parser.add_argument(
        "--output",
        default="wind_h2_dispatch_vs_flat_load.svg",
        help="Output SVG path.",
    )
    parser.add_argument(
        "--summary",
        help=(
            "Optional summary JSON path to read reservoir capacity from. "
            "If omitted, defaults to <output_prefix>_summary.json from config."
        ),
    )
    parser.add_argument(
        "--reservoir-capacity-mwh-h2",
        type=float,
        help=(
            "Optional reservoir capacity override in MWh(H2). "
            "If not set, script reads from summary or falls back to yearly working range."
        ),
    )
    parser.add_argument(
        "--start-fullness-pct",
        type=float,
        help="Initial cavern fullness in percent (overrides config).",
    )
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    csv_path = resolve_csv_path(Path(args.csv if args.csv else cfg["csv"]))
    demand_mw = float(args.demand_mw if args.demand_mw is not None else cfg["demand_mw"])
    out_path = Path(args.output)
    eta_charge, eta_discharge = resolve_efficiencies(cfg)

    data = load_wind_series(csv_path)
    times = data["times"]
    wind_mw = data["wind_mw"]
    n = len(wind_mw)

    if is_complete_series(data["demand_mw"]):
        demand_series = [float(v) for v in data["demand_mw"]]
    else:
        demand_series = [demand_mw] * n

    if is_complete_series(data["charge_h2_mwh_actual"]):
        electrolyzer_draw = [float(v) / eta_charge for v in data["charge_h2_mwh_actual"]]
    else:
        electrolyzer_draw = [max(w - d, 0.0) for w, d in zip(wind_mw, demand_series)]

    if is_complete_series(data["discharge_h2_mwh_actual"]):
        h2_turbine_output = [float(v) * eta_discharge for v in data["discharge_h2_mwh_actual"]]
    else:
        h2_turbine_output = [max(d - w, 0.0) for w, d in zip(wind_mw, demand_series)]

    summary_path = (
        Path(args.summary)
        if args.summary
        else Path(f"{cfg.get('output_prefix', 'h2_storage')}_summary.json")
    )
    reservoir_capacity_mwh_h2 = (
        float(args.reservoir_capacity_mwh_h2)
        if args.reservoir_capacity_mwh_h2 is not None
        else cfg.get("reservoir_capacity_mwh_h2")
    )
    if reservoir_capacity_mwh_h2 is None:
        reservoir_capacity_mwh_h2 = load_reservoir_capacity_from_summary(summary_path)

    # Fallback: if no configured/summary capacity exists, use the simulated working range.
    if reservoir_capacity_mwh_h2 is None:
        cum = 0.0
        min_cum = 0.0
        max_cum = 0.0
        for e, h in zip(electrolyzer_draw, h2_turbine_output):
            cum += e * eta_charge - h / eta_discharge
            min_cum = min(min_cum, cum)
            max_cum = max(max_cum, cum)
        reservoir_capacity_mwh_h2 = max_cum - min_cum

    reservoir_capacity_mwh_h2 = float(reservoir_capacity_mwh_h2)
    if reservoir_capacity_mwh_h2 <= 0:
        raise ValueError("Reservoir capacity must be > 0.")

    configured_start_fullness_pct = float(
        args.start_fullness_pct
        if args.start_fullness_pct is not None
        else cfg.get("start_fullness_pct", 50.0)
    )
    if is_complete_series(data["soc_end_pct"]):
        fullness_pct = [float(v) for v in data["soc_end_pct"]]
        start_fullness_pct = fullness_pct[0]
        fullness_source = "csv_soc_end_pct"
    else:
        start_fullness_pct = configured_start_fullness_pct
        start_soc_mwh = reservoir_capacity_mwh_h2 * start_fullness_pct / 100.0
        soc_mwh = start_soc_mwh
        fullness_pct = [start_fullness_pct]
        for i in range(1, n):
            soc_mwh += (
                electrolyzer_draw[i - 1] * eta_charge
                - h2_turbine_output[i - 1] / eta_discharge
            )
            fullness_pct.append((soc_mwh / reservoir_capacity_mwh_h2) * 100.0)
        fullness_source = "reconstructed_from_power_flows"

    max_y = max(max(wind_mw), demand_mw, max(electrolyzer_draw), max(h2_turbine_output)) * 1.08
    min_y = 0.0

    fullness_min = min(fullness_pct)
    fullness_max = max(fullness_pct)
    y2_min = min(0.0, fullness_min)
    y2_max = max(100.0, fullness_max)
    if y2_max == y2_min:
        y2_max = y2_min + 1.0

    # Chart dimensions
    width, height = 1700, 860
    ml, mr, mt, mb = 90, 90, 80, 95
    cw = width - ml - mr
    ch = height - mt - mb

    def x_of(i):
        if n <= 1:
            return ml
        return ml + (i / (n - 1)) * cw

    def y_of(v):
        return mt + (1 - (v - min_y) / (max_y - min_y)) * ch

    def y2_of(v):
        return mt + (1 - (v - y2_min) / (y2_max - y2_min)) * ch

    x_vals = [x_of(i) for i in range(n)]

    wind_points = make_polyline(x_vals, [y_of(v) for v in wind_mw])
    demand_points = make_polyline(x_vals, [y_of(v) for v in demand_series])
    electro_points = make_polyline(x_vals, [y_of(v) for v in electrolyzer_draw])
    h2_points = make_polyline(x_vals, [y_of(v) for v in h2_turbine_output])
    fullness_points = make_polyline(x_vals, [y2_of(v) for v in fullness_pct])

    month_ticks = []
    for i, t in enumerate(times):
        if t.day == 1 and t.hour == 0:
            month_ticks.append((i, t.strftime("%b")))

    y_ticks = []
    tick_step = 2000
    v = 0
    while v <= max_y:
        y_ticks.append(v)
        v += tick_step

    y2_ticks = []
    y2_step = 20
    start_tick = int(y2_min // y2_step) * y2_step
    end_tick = int((y2_max + y2_step - 1) // y2_step) * y2_step
    t = start_tick
    while t <= end_tick:
        y2_ticks.append(t)
        t += y2_step

    lines = []
    add = lines.append
    add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">')
    add('<rect width="100%" height="100%" fill="white"/>')
    add("<style>")
    add(".axis{stroke:#1f1f1f;stroke-width:1.5}")
    add(".grid{stroke:#e2e2e2;stroke-width:1}")
    add(".title{font:22px sans-serif;font-weight:600;fill:#101010}")
    add(".label{font:14px sans-serif;fill:#222}")
    add(".small{font:12px sans-serif;fill:#444}")
    add("</style>")

    add('<text class="title" x="90" y="40">Wind, Flat AI Load, Electrolyzer Draw, and H2 Turbine Output (Hourly)</text>')

    # Horizontal grid and y-axis labels
    for yt in y_ticks:
        y = y_of(yt)
        add(f'<line class="grid" x1="{ml}" y1="{y:.2f}" x2="{ml+cw}" y2="{y:.2f}"/>')
        add(f'<text class="small" x="{ml-10}" y="{y+4:.2f}" text-anchor="end">{int(yt)}</text>')

    # Right axis labels for cavern fullness
    for yt in y2_ticks:
        y = y2_of(yt)
        add(f'<text class="small" x="{ml+cw+10}" y="{y+4:.2f}" text-anchor="start">{yt:.0f}</text>')

    # Vertical month lines and labels
    for i, label in month_ticks:
        x = x_of(i)
        add(f'<line class="grid" x1="{x:.2f}" y1="{mt}" x2="{x:.2f}" y2="{mt+ch}"/>')
        add(f'<text class="small" x="{x:.2f}" y="{mt+ch+26}" text-anchor="middle">{label}</text>')

    # Axes
    add(f'<line class="axis" x1="{ml}" y1="{mt+ch}" x2="{ml+cw}" y2="{mt+ch}"/>')
    add(f'<line class="axis" x1="{ml}" y1="{mt}" x2="{ml}" y2="{mt+ch}"/>')
    add(f'<line class="axis" x1="{ml+cw}" y1="{mt}" x2="{ml+cw}" y2="{mt+ch}"/>')

    # Series
    add(f'<polyline fill="none" stroke="#2c6db2" stroke-width="1.1" points="{wind_points}"/>')
    add(f'<polyline fill="none" stroke="#c83349" stroke-width="1.6" stroke-dasharray="8,6" points="{demand_points}"/>')
    add(f'<polyline fill="none" stroke="#2b9b57" stroke-width="1.0" points="{electro_points}"/>')
    add(f'<polyline fill="none" stroke="#d48806" stroke-width="1.0" points="{h2_points}"/>')
    add(f'<polyline fill="none" stroke="#ff0000" stroke-width="1.3" points="{fullness_points}"/>')

    # Axis labels
    add(f'<text class="label" x="{ml+cw/2:.2f}" y="{height-28}" text-anchor="middle">Time</text>')
    add(f'<text class="label" x="28" y="{mt+ch/2:.2f}" transform="rotate(-90 28 {mt+ch/2:.2f})" text-anchor="middle">Power (MW)</text>')
    add(
        f'<text class="label" x="{width-20}" y="{mt+ch/2:.2f}" '
        f'transform="rotate(90 {width-20} {mt+ch/2:.2f})" text-anchor="middle">Salt cavern fullness (%)</text>'
    )

    # Legend
    lx, ly, lw, lh = width - 610, 28, 570, 130
    add(f'<rect x="{lx}" y="{ly}" width="{lw}" height="{lh}" fill="white" stroke="#cfcfcf"/>')
    add(f'<line x1="{lx+14}" y1="{ly+22}" x2="{lx+54}" y2="{ly+22}" stroke="#2c6db2" stroke-width="2"/>')
    add(f'<text class="small" x="{lx+62}" y="{ly+26}">Wind farm production (MW)</text>')
    add(f'<line x1="{lx+14}" y1="{ly+44}" x2="{lx+54}" y2="{ly+44}" stroke="#c83349" stroke-width="2" stroke-dasharray="8,6"/>')
    add(f'<text class="small" x="{lx+62}" y="{ly+48}">Flat AI demand ({demand_mw:.0f} MW)</text>')
    add(f'<line x1="{lx+14}" y1="{ly+66}" x2="{lx+54}" y2="{ly+66}" stroke="#2b9b57" stroke-width="2"/>')
    add(f'<text class="small" x="{lx+62}" y="{ly+70}">Electrolyzer power draw (surplus only)</text>')
    add(f'<line x1="{lx+14}" y1="{ly+88}" x2="{lx+54}" y2="{ly+88}" stroke="#d48806" stroke-width="2"/>')
    add(f'<text class="small" x="{lx+62}" y="{ly+92}">H2 turbine output (deficit only)</text>')
    add(f'<line x1="{lx+14}" y1="{ly+110}" x2="{lx+54}" y2="{ly+110}" stroke="#ff0000" stroke-width="2"/>')
    add(
        f'<text class="small" x="{lx+62}" y="{ly+114}">Salt cavern fullness (%) '
        f'(start={start_fullness_pct:.0f}%)</text>'
    )

    add("</svg>")

    out_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"chart_file={out_path}")
    print(f"hours={n}")
    print(f"demand_mw={demand_mw:.3f}")
    print(f"max_wind_mw={max(wind_mw):.3f}")
    print(f"max_electrolyzer_draw_mw={max(electrolyzer_draw):.3f}")
    print(f"max_h2_turbine_output_mw={max(h2_turbine_output):.3f}")
    print(f"eta_charge={eta_charge:.4f}")
    print(f"eta_discharge={eta_discharge:.4f}")
    print(f"reservoir_capacity_mwh_h2={reservoir_capacity_mwh_h2:.3f}")
    print(f"start_fullness_pct={start_fullness_pct:.3f}")
    print(f"min_fullness_pct={fullness_min:.3f}")
    print(f"max_fullness_pct={fullness_max:.3f}")
    print(f"fullness_source={fullness_source}")


if __name__ == "__main__":
    main()
