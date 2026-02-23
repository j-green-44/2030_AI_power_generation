#!/usr/bin/env python3
"""
Plot hourly wind + solar generation, flat demand, and H2 reservoir fullness
for a hybrid optimized design summary.
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
WIND_DIR = ROOT_DIR / "wind"

if str(WIND_DIR) not in sys.path:
    sys.path.insert(0, str(WIND_DIR))

import hydrogen_storage_sizing as hs  # noqa: E402


def load_json_object(path: Path):
    with path.open() as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return obj


def resolve_path(path_str: str, base_dir: Path):
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


def load_and_align_profiles(wind_csv_path: Path, solar_csv_path: Path):
    wind_times_raw, wind_mw = hs.load_wind_series(wind_csv_path)
    solar_times_raw, solar_mw = hs.load_wind_series(solar_csv_path)

    def parse_time(value):
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        raise ValueError(f"Unsupported timestamp format: {value}")

    wind_times = [parse_time(t) for t in wind_times_raw]
    solar_times = [parse_time(t) for t in solar_times_raw]

    solar_lookup = {t: v for t, v in zip(solar_times, solar_mw)}
    aligned_times = []
    aligned_wind = []
    aligned_solar = []
    for t, w in zip(wind_times, wind_mw):
        s = solar_lookup.get(t)
        if s is None:
            continue
        aligned_times.append(t)
        aligned_wind.append(w)
        aligned_solar.append(s)

    if not aligned_times:
        raise ValueError("No overlapping timestamps between wind and solar profiles.")
    return aligned_times, aligned_wind, aligned_solar


def make_polyline(x_vals, y_vals):
    return " ".join(f"{x:.2f},{y:.2f}" for x, y in zip(x_vals, y_vals))


def choose_tick_step(max_value):
    for step in [200, 500, 1000, 2000, 5000, 10000, 20000]:
        if max_value / step <= 9.5:
            return step
    return 50000


def simulate_hourly_dispatch(
    wind_mw,
    solar_mw,
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
    if len(wind_mw) != len(solar_mw):
        raise ValueError("wind and solar series must have same length.")

    n = len(wind_mw)
    if storage_capacity_mwh_h2 > 0:
        soc_floor_mwh = storage_capacity_mwh_h2 * soc_floor_pct / 100.0
        soc_ceiling_mwh = storage_capacity_mwh_h2 * soc_ceiling_pct / 100.0
        soc = storage_capacity_mwh_h2 * start_fullness_pct / 100.0
        soc = min(max(soc, soc_floor_mwh), soc_ceiling_mwh)
    else:
        soc_floor_mwh = 0.0
        soc_ceiling_mwh = 0.0
        soc = 0.0

    start_soc_mwh = soc
    fullness_pct = []
    electrolyzer_draw_mw = []
    h2_turbine_output_mw = []
    gas_dispatch_mw = []

    for w, s in zip(wind_mw, solar_mw):
        renewable = w + s
        surplus = max(renewable - demand_mw, 0.0)
        deficit = max(demand_mw - renewable, 0.0)

        charge_input_mw = 0.0
        if storage_capacity_mwh_h2 > 0 and electrolyzer_mw > 0:
            charge_input_mw = min(surplus, electrolyzer_mw)
            charge_room_electric = max((soc_ceiling_mwh - soc) / eta_charge, 0.0)
            charge_input_mw = min(charge_input_mw, charge_room_electric)

        charge_h2_mwh = charge_input_mw * eta_charge
        soc += charge_h2_mwh

        h2_output_mw = 0.0
        if storage_capacity_mwh_h2 > 0 and h2_turbine_mw > 0:
            h2_output_mw = min(deficit, h2_turbine_mw)
            h2_output_by_soc = max(soc - soc_floor_mwh, 0.0) * eta_discharge
            h2_output_mw = min(h2_output_mw, h2_output_by_soc)

        discharge_h2_mwh = h2_output_mw / eta_discharge if h2_output_mw > 0 else 0.0
        soc -= discharge_h2_mwh

        fullness = 0.0
        if storage_capacity_mwh_h2 > 0:
            fullness = (soc / storage_capacity_mwh_h2) * 100.0
        fullness_pct.append(fullness)

        electrolyzer_draw_mw.append(charge_input_mw)
        h2_turbine_output_mw.append(h2_output_mw)
        gas_dispatch_mw.append(max(deficit - h2_output_mw, 0.0))

    return {
        "hours": n,
        "fullness_pct": fullness_pct,
        "start_soc_mwh": start_soc_mwh,
        "end_soc_mwh": soc,
        "electrolyzer_draw_mw": electrolyzer_draw_mw,
        "h2_turbine_output_mw": h2_turbine_output_mw,
        "gas_dispatch_mw": gas_dispatch_mw,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Plot hourly hybrid wind+solar+H2 dispatch from optimized summary."
    )
    parser.add_argument(
        "--summary",
        default="hybrid/hybrid_dispatch_no_gas_apples_opt_summary.json",
        help="Path to hybrid summary JSON.",
    )
    parser.add_argument(
        "--output",
        default="hybrid/hybrid_wind_solar_h2_dispatch_vs_flat_load.svg",
        help="Output SVG file.",
    )
    parser.add_argument(
        "--demand-mw",
        type=float,
        help="Optional demand override (default from summary inputs).",
    )
    args = parser.parse_args()

    summary_path = Path(args.summary)
    summary = load_json_object(summary_path)
    inputs = summary.get("inputs", {})
    best = summary.get("best_design", {})
    if not inputs or not best:
        raise ValueError("Summary must contain 'inputs' and 'best_design'.")

    wind_cfg_path = resolve_path(str(inputs["wind_config"]), summary_path.parent)
    solar_cfg_path = resolve_path(str(inputs["solar_config"]), summary_path.parent)
    wind_cfg = load_json_object(wind_cfg_path)
    solar_cfg = load_json_object(solar_cfg_path)

    wind_csv = resolve_path(str(wind_cfg["csv"]), wind_cfg_path.parent)
    solar_csv = resolve_path(str(solar_cfg["csv"]), solar_cfg_path.parent)
    times, wind_profile_raw_mw, solar_profile_raw_mw = load_and_align_profiles(wind_csv, solar_csv)

    wind_profile_capacity_mw = float(inputs.get("wind_profile_capacity_mw", 0.0))
    solar_profile_capacity_mw = float(inputs.get("solar_profile_capacity_mw", 0.0))
    if wind_profile_capacity_mw <= 0:
        wind_profile_capacity_mw = float(wind_cfg["current_installed_capacity_mw"])
    if solar_profile_capacity_mw <= 0:
        solar_profile_capacity_mw = float(solar_cfg["current_installed_capacity_mw"])

    wind_stress = float(inputs.get("wind_stress_factor", wind_cfg.get("wind_stress_factor", 1.0)))
    solar_stress = float(
        inputs.get(
            "solar_stress_factor",
            solar_cfg.get("solar_stress_factor", solar_cfg.get("wind_stress_factor", 1.0)),
        )
    )

    wind_mw_design = float(best.get("wind_mw", 0.0))
    solar_mw_design = float(best.get("solar_mw", 0.0))
    electrolyzer_mw = float(best.get("electrolyzer_mw", 0.0))
    h2_turbine_mw = float(best.get("h2_turbine_mw", 0.0))
    storage_mwh_h2 = float(best.get("storage_mwh_h2", 0.0))

    wind_scale = 0.0 if wind_profile_capacity_mw <= 0 else (wind_mw_design / wind_profile_capacity_mw) * wind_stress
    solar_scale = (
        0.0 if solar_profile_capacity_mw <= 0 else (solar_mw_design / solar_profile_capacity_mw) * solar_stress
    )
    wind_gen_mw = [v * wind_scale for v in wind_profile_raw_mw]
    solar_gen_mw = [v * solar_scale for v in solar_profile_raw_mw]

    demand_mw = float(args.demand_mw if args.demand_mw is not None else inputs["demand_mw"])
    eta_charge = float(inputs["eta_charge"])
    eta_discharge = float(inputs["eta_discharge"])
    start_fullness_pct = float(inputs["start_fullness_pct"])
    soc_floor_pct = float(inputs["soc_floor_pct"])
    soc_ceiling_pct = float(inputs["soc_ceiling_pct"])

    sim = simulate_hourly_dispatch(
        wind_mw=wind_gen_mw,
        solar_mw=solar_gen_mw,
        demand_mw=demand_mw,
        eta_charge=eta_charge,
        eta_discharge=eta_discharge,
        electrolyzer_mw=electrolyzer_mw,
        h2_turbine_mw=h2_turbine_mw,
        storage_capacity_mwh_h2=storage_mwh_h2,
        start_fullness_pct=start_fullness_pct,
        soc_floor_pct=soc_floor_pct,
        soc_ceiling_pct=soc_ceiling_pct,
    )

    n = sim["hours"]
    demand_series = [demand_mw] * n
    fullness_pct = sim["fullness_pct"]
    if storage_mwh_h2 > 0:
        fullness_min = min(fullness_pct)
        fullness_max = max(fullness_pct)
    else:
        fullness_min = 0.0
        fullness_max = 0.0

    max_y = max(max(wind_gen_mw), max(solar_gen_mw), demand_mw) * 1.08
    min_y = 0.0
    y2_min = min(0.0, fullness_min)
    y2_max = max(100.0, fullness_max)
    if y2_max == y2_min:
        y2_max = y2_min + 1.0

    width, height = 1700, 980
    ml, mr, mt, mb = 90, 110, 200, 95
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
    wind_points = make_polyline(x_vals, [y_of(v) for v in wind_gen_mw])
    solar_points = make_polyline(x_vals, [y_of(v) for v in solar_gen_mw])
    demand_points = make_polyline(x_vals, [y_of(v) for v in demand_series])
    fullness_points = make_polyline(x_vals, [y2_of(v) for v in fullness_pct])

    month_ticks = []
    for i, t in enumerate(times):
        if t.day == 1 and t.hour == 0:
            month_ticks.append((i, t.strftime("%b")))

    y_ticks = []
    tick_step = choose_tick_step(max_y)
    v = 0.0
    while v <= max_y + 1e-6:
        y_ticks.append(v)
        v += tick_step

    y2_ticks = []
    y2_step = 10
    y2_start = int(y2_min // y2_step) * y2_step
    y2_end = int((y2_max + y2_step - 1) // y2_step) * y2_step
    v2 = y2_start
    while v2 <= y2_end:
        y2_ticks.append(v2)
        v2 += y2_step

    lines = []
    add = lines.append
    add(f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">')
    add('<rect width="100%" height="100%" fill="white"/>')
    add("<style>")
    add(".axis{stroke:#1f1f1f;stroke-width:1.5}")
    add(".grid{stroke:#e2e2e2;stroke-width:1}")
    add(".title{font:22px sans-serif;font-weight:600;fill:#101010}")
    add(".label{font:14px sans-serif;font-weight:700;fill:#222}")
    add(".small{font:12px sans-serif;fill:#444}")
    add(".legend{font:14px sans-serif;font-weight:700;fill:#1f1f1f}")
    add("</style>")

    add(
        '<text class="title" x="90" y="40">'
        "Hybrid Dispatch: Wind + Solar Generation, Flat Demand, and H2 Reservoir Fullness"
        "</text>"
    )

    for yt in y_ticks:
        y = y_of(yt)
        add(f'<line class="grid" x1="{ml}" y1="{y:.2f}" x2="{ml+cw}" y2="{y:.2f}"/>')
        add(f'<text class="small" x="{ml-10}" y="{y+4:.2f}" text-anchor="end">{int(round(yt))}</text>')

    for yt in y2_ticks:
        y = y2_of(yt)
        add(f'<text class="small" x="{ml+cw+10}" y="{y+4:.2f}" text-anchor="start">{yt:.0f}</text>')

    for i, label in month_ticks:
        x = x_of(i)
        add(f'<line class="grid" x1="{x:.2f}" y1="{mt}" x2="{x:.2f}" y2="{mt+ch}"/>')
        add(f'<text class="small" x="{x:.2f}" y="{mt+ch+26}" text-anchor="middle">{label}</text>')

    add(f'<line class="axis" x1="{ml}" y1="{mt+ch}" x2="{ml+cw}" y2="{mt+ch}"/>')
    add(f'<line class="axis" x1="{ml}" y1="{mt}" x2="{ml}" y2="{mt+ch}"/>')
    add(f'<line class="axis" x1="{ml+cw}" y1="{mt}" x2="{ml+cw}" y2="{mt+ch}"/>')

    add(f'<polyline fill="none" stroke="#2c6db2" stroke-width="1.1" points="{wind_points}"/>')
    add(f'<polyline fill="none" stroke="#f0a202" stroke-width="1.1" points="{solar_points}"/>')
    add(
        f'<polyline fill="none" stroke="#c83349" stroke-width="1.6" stroke-dasharray="8,6" '
        f'points="{demand_points}"/>'
    )
    add(f'<polyline fill="none" stroke="#b30000" stroke-width="1.3" points="{fullness_points}"/>')

    add(f'<text class="label" x="{ml+cw/2:.2f}" y="{height-28}" text-anchor="middle">Time</text>')
    add(
        f'<text class="label" x="28" y="{mt+ch/2:.2f}" transform="rotate(-90 28 {mt+ch/2:.2f})" '
        "text-anchor=\"middle\">Power (MW)</text>"
    )
    add(
        f'<text class="label" x="{width-20}" y="{mt+ch/2:.2f}" '
        f'transform="rotate(90 {width-20} {mt+ch/2:.2f})" text-anchor="middle">'
        "H2 reservoir fullness (%)</text>"
    )

    lx, ly, lw, lh = width - 570, 56, 530, 108
    add(
        f'<rect x="{lx}" y="{ly}" width="{lw}" height="{lh}" '
        'fill="white" fill-opacity="0.92" stroke="#cfcfcf"/>'
    )
    add(f'<line x1="{lx+14}" y1="{ly+22}" x2="{lx+54}" y2="{ly+22}" stroke="#2c6db2" stroke-width="2"/>')
    add(f'<text class="legend" x="{lx+62}" y="{ly+26}">Wind generation (MW)</text>')
    add(f'<line x1="{lx+14}" y1="{ly+44}" x2="{lx+54}" y2="{ly+44}" stroke="#f0a202" stroke-width="2"/>')
    add(f'<text class="legend" x="{lx+62}" y="{ly+48}">Solar generation (MW)</text>')
    add(
        f'<line x1="{lx+14}" y1="{ly+66}" x2="{lx+54}" y2="{ly+66}" '
        f'stroke="#c83349" stroke-width="2" stroke-dasharray="8,6"/>'
    )
    add(f'<text class="legend" x="{lx+62}" y="{ly+70}">Flat demand ({demand_mw:.0f} MW)</text>')
    add(f'<line x1="{lx+14}" y1="{ly+88}" x2="{lx+54}" y2="{ly+88}" stroke="#b30000" stroke-width="2"/>')
    add(f'<text class="legend" x="{lx+62}" y="{ly+92}">H2 reservoir fullness (%)</text>')

    add("</svg>")

    out_path = Path(args.output)
    out_path.write_text("\n".join(lines), encoding="utf-8")

    print(f"chart_file={out_path}")
    print(f"summary_file={summary_path}")
    print(f"hours={n}")
    print(f"demand_mw={demand_mw:.3f}")
    print(f"wind_design_mw={wind_mw_design:.3f}")
    print(f"solar_design_mw={solar_mw_design:.3f}")
    print(f"electrolyzer_mw={electrolyzer_mw:.3f}")
    print(f"h2_turbine_mw={h2_turbine_mw:.3f}")
    print(f"storage_mwh_h2={storage_mwh_h2:.3f}")
    print(f"start_fullness_pct={start_fullness_pct:.3f}")
    print(f"end_fullness_pct={fullness_pct[-1]:.3f}")
    print(f"min_fullness_pct={fullness_min:.3f}")
    print(f"max_fullness_pct={fullness_max:.3f}")
    print(f"max_wind_generation_mw={max(wind_gen_mw):.3f}")
    print(f"max_solar_generation_mw={max(solar_gen_mw):.3f}")
    print(f"max_gas_dispatch_mw={max(sim['gas_dispatch_mw']):.3f}")


if __name__ == "__main__":
    main()
