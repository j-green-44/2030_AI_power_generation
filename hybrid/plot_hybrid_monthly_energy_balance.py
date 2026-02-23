#!/usr/bin/env python3
"""
Create a monthly hybrid energy bar chart with storage fullness on a right-axis line.

Bars (monthly totals, GWh):
- wind generation
- solar generation
- H2 turbine output
- electrolyzer electricity consumption

Line (monthly, %):
- H2 storage fullness (end-of-month or average-of-month)
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


MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def load_json_object(path: Path):
    with path.open() as f:
        payload = json.load(f)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object.")
    return payload


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


def parse_time(value):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unsupported timestamp format: {value}")


def load_and_align_profiles(wind_csv_path: Path, solar_csv_path: Path):
    wind_times_raw, wind_mw = hs.load_wind_series(wind_csv_path)
    solar_times_raw, solar_mw = hs.load_wind_series(solar_csv_path)

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

    if storage_capacity_mwh_h2 > 0:
        soc_floor_mwh = storage_capacity_mwh_h2 * soc_floor_pct / 100.0
        soc_ceiling_mwh = storage_capacity_mwh_h2 * soc_ceiling_pct / 100.0
        soc = storage_capacity_mwh_h2 * start_fullness_pct / 100.0
        soc = min(max(soc, soc_floor_mwh), soc_ceiling_mwh)
    else:
        soc_floor_mwh = 0.0
        soc_ceiling_mwh = 0.0
        soc = 0.0

    electrolyzer_draw_mw = []
    h2_turbine_output_mw = []
    fullness_pct = []

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

        electrolyzer_draw_mw.append(charge_input_mw)
        h2_turbine_output_mw.append(h2_output_mw)
        fullness_pct.append((soc / storage_capacity_mwh_h2) * 100.0 if storage_capacity_mwh_h2 > 0 else 0.0)

    return {
        "electrolyzer_draw_mw": electrolyzer_draw_mw,
        "h2_turbine_output_mw": h2_turbine_output_mw,
        "fullness_pct": fullness_pct,
    }


def choose_tick_step(max_value):
    for step in [100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000]:
        if max_value / step <= 9.0:
            return step
    return 100000


def aggregate_monthly(times, wind_mw, solar_mw, electrolyzer_mw, h2_turbine_mw, fullness_pct, fullness_mode):
    agg = {
        month: {
            "wind_mwh": 0.0,
            "solar_mwh": 0.0,
            "electrolyzer_mwh": 0.0,
            "h2_turbine_mwh": 0.0,
            "fullness_sum": 0.0,
            "fullness_count": 0,
            "fullness_end": None,
        }
        for month in range(1, 13)
    }

    for t, w, s, e, h2, f in zip(times, wind_mw, solar_mw, electrolyzer_mw, h2_turbine_mw, fullness_pct):
        m = t.month
        bucket = agg[m]
        bucket["wind_mwh"] += w
        bucket["solar_mwh"] += s
        bucket["electrolyzer_mwh"] += e
        bucket["h2_turbine_mwh"] += h2
        bucket["fullness_sum"] += f
        bucket["fullness_count"] += 1
        bucket["fullness_end"] = f

    wind_gwh = []
    solar_gwh = []
    h2_turbine_gwh = []
    electrolyzer_gwh = []
    fullness = []
    for m in range(1, 13):
        bucket = agg[m]
        wind_gwh.append(bucket["wind_mwh"] / 1000.0)
        solar_gwh.append(bucket["solar_mwh"] / 1000.0)
        h2_turbine_gwh.append(bucket["h2_turbine_mwh"] / 1000.0)
        electrolyzer_gwh.append(bucket["electrolyzer_mwh"] / 1000.0)
        if bucket["fullness_count"] == 0:
            fullness.append(0.0)
        elif fullness_mode == "avg":
            fullness.append(bucket["fullness_sum"] / bucket["fullness_count"])
        else:
            fullness.append(float(bucket["fullness_end"]))

    return wind_gwh, solar_gwh, h2_turbine_gwh, electrolyzer_gwh, fullness


def build_svg(
    out_path,
    months,
    wind_gwh,
    solar_gwh,
    h2_turbine_gwh,
    electrolyzer_gwh,
    fullness_pct,
    fullness_mode_label,
):
    width, height = 1760, 1060
    ml, mr, mt, mb = 95, 115, 220, 130
    cw = width - ml - mr
    ch = height - mt - mb

    bar_series = [wind_gwh, solar_gwh, h2_turbine_gwh, electrolyzer_gwh]
    max_bar = max(max(series) for series in bar_series)
    max_bar = max(max_bar * 1.12, 1.0)
    min_bar = 0.0

    fullness_min = min(fullness_pct)
    fullness_max = max(fullness_pct)
    y2_min = min(0.0, fullness_min)
    y2_max = max(100.0, fullness_max)
    if y2_max == y2_min:
        y2_max = y2_min + 1.0

    def y_of(v):
        return mt + (1.0 - (v - min_bar) / (max_bar - min_bar)) * ch

    def y2_of(v):
        return mt + (1.0 - (v - y2_min) / (y2_max - y2_min)) * ch

    n_groups = len(months)
    slot_w = cw / n_groups
    group_w = slot_w * 0.78
    bar_count = 4
    inner_gap = group_w * 0.04
    bar_w = (group_w - inner_gap * (bar_count - 1)) / bar_count

    group_centers = []
    group_starts = []
    for i in range(n_groups):
        x_slot = ml + i * slot_w
        g_start = x_slot + (slot_w - group_w) / 2.0
        group_starts.append(g_start)
        group_centers.append(g_start + group_w / 2.0)

    y_ticks = []
    step = choose_tick_step(max_bar)
    tick = 0.0
    while tick <= max_bar + 1e-9:
        y_ticks.append(tick)
        tick += step

    y2_ticks = []
    y2_step = 10
    t2 = int(y2_min // y2_step) * y2_step
    t2_end = int((y2_max + y2_step - 1) // y2_step) * y2_step
    while t2 <= t2_end:
        y2_ticks.append(t2)
        t2 += y2_step

    line_points = " ".join(
        f"{x:.2f},{y2_of(v):.2f}" for x, v in zip(group_centers, fullness_pct)
    )

    lines = []
    add = lines.append
    add(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
    )
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
        '<text class="title" x="95" y="42">'
        "Monthly Hybrid Energy Totals and H2 Storage Fullness"
        "</text>"
    )

    for v in y_ticks:
        y = y_of(v)
        add(f'<line class="grid" x1="{ml}" y1="{y:.2f}" x2="{ml+cw}" y2="{y:.2f}"/>')
        add(f'<text class="small" x="{ml-10}" y="{y+4:.2f}" text-anchor="end">{int(round(v))}</text>')

    for v in y2_ticks:
        y = y2_of(v)
        add(f'<text class="small" x="{ml+cw+10}" y="{y+4:.2f}" text-anchor="start">{v:.0f}</text>')

    for x, month in zip(group_centers, months):
        add(f'<text class="small" x="{x:.2f}" y="{mt+ch+26}" text-anchor="middle">{month}</text>')

    add(f'<line class="axis" x1="{ml}" y1="{mt+ch}" x2="{ml+cw}" y2="{mt+ch}"/>')
    add(f'<line class="axis" x1="{ml}" y1="{mt}" x2="{ml}" y2="{mt+ch}"/>')
    add(f'<line class="axis" x1="{ml+cw}" y1="{mt}" x2="{ml+cw}" y2="{mt+ch}"/>')

    colors = ["#2c6db2", "#f0a202", "#d48806", "#2b9b57"]
    series = [wind_gwh, solar_gwh, h2_turbine_gwh, electrolyzer_gwh]

    for i, g_start in enumerate(group_starts):
        for j, values in enumerate(series):
            x = g_start + j * (bar_w + inner_gap)
            y = y_of(values[i])
            h = (mt + ch) - y
            add(
                f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" height="{h:.2f}" '
                f'fill="{colors[j]}"/>'
            )

    add(f'<polyline fill="none" stroke="#c83349" stroke-width="2.2" points="{line_points}"/>')
    for x, v in zip(group_centers, fullness_pct):
        y = y2_of(v)
        add(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.8" fill="#c83349"/>')

    add(f'<text class="label" x="{ml+cw/2:.2f}" y="{height-34}" text-anchor="middle">Month</text>')
    add(
        f'<text class="label" x="28" y="{mt+ch/2:.2f}" transform="rotate(-90 28 {mt+ch/2:.2f})" '
        'text-anchor="middle">Monthly energy total (GWh)</text>'
    )
    add(
        f'<text class="label" x="{width-22}" y="{mt+ch/2:.2f}" '
        f'transform="rotate(90 {width-22} {mt+ch/2:.2f})" text-anchor="middle">'
        f'H2 storage fullness (%), {fullness_mode_label}</text>'
    )

    lx, ly, lw, lh = width - 590, 50, 550, 154
    add(
        f'<rect x="{lx}" y="{ly}" width="{lw}" height="{lh}" '
        'fill="white" fill-opacity="0.92" stroke="#cfcfcf"/>'
    )
    add(f'<rect x="{lx+14}" y="{ly+14}" width="28" height="12" fill="#2c6db2"/>')
    add(f'<text class="legend" x="{lx+52}" y="{ly+24}">Wind generation (GWh/month)</text>')
    add(f'<rect x="{lx+14}" y="{ly+36}" width="28" height="12" fill="#f0a202"/>')
    add(f'<text class="legend" x="{lx+52}" y="{ly+46}">Solar generation (GWh/month)</text>')
    add(f'<rect x="{lx+14}" y="{ly+58}" width="28" height="12" fill="#d48806"/>')
    add(f'<text class="legend" x="{lx+52}" y="{ly+68}">H2 turbine output (GWh/month)</text>')
    add(f'<rect x="{lx+14}" y="{ly+80}" width="28" height="12" fill="#2b9b57"/>')
    add(f'<text class="legend" x="{lx+52}" y="{ly+90}">Electrolyzer electricity use (GWh/month)</text>')
    add(f'<line x1="{lx+14}" y1="{ly+108}" x2="{lx+42}" y2="{ly+108}" stroke="#c83349" stroke-width="2.2"/>')
    add(f'<circle cx="{lx+28}" cy="{ly+108}" r="2.8" fill="#c83349"/>')
    add(f'<text class="legend" x="{lx+52}" y="{ly+112}">Storage fullness (%), {fullness_mode_label}</text>')

    add("</svg>")
    out_path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(
        description="Plot monthly hybrid energy bars with storage fullness line."
    )
    parser.add_argument(
        "--summary",
        default="hybrid/hybrid_dispatch_no_gas_25y_nondepleting_opt_summary.json",
        help="Path to hybrid summary JSON.",
    )
    parser.add_argument(
        "--output",
        default="hybrid/hybrid_monthly_energy_balance.svg",
        help="Output SVG file.",
    )
    parser.add_argument(
        "--fullness-mode",
        choices=["end", "avg"],
        default="end",
        help="Use end-of-month or average monthly storage fullness for the line.",
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

    wind_scale = (wind_mw_design / wind_profile_capacity_mw) * wind_stress if wind_profile_capacity_mw > 0 else 0.0
    solar_scale = (solar_mw_design / solar_profile_capacity_mw) * solar_stress if solar_profile_capacity_mw > 0 else 0.0
    wind_gen_mw = [v * wind_scale for v in wind_profile_raw_mw]
    solar_gen_mw = [v * solar_scale for v in solar_profile_raw_mw]

    sim = simulate_hourly_dispatch(
        wind_mw=wind_gen_mw,
        solar_mw=solar_gen_mw,
        demand_mw=float(inputs["demand_mw"]),
        eta_charge=float(inputs["eta_charge"]),
        eta_discharge=float(inputs["eta_discharge"]),
        electrolyzer_mw=electrolyzer_mw,
        h2_turbine_mw=h2_turbine_mw,
        storage_capacity_mwh_h2=storage_mwh_h2,
        start_fullness_pct=float(inputs["start_fullness_pct"]),
        soc_floor_pct=float(inputs["soc_floor_pct"]),
        soc_ceiling_pct=float(inputs["soc_ceiling_pct"]),
    )

    wind_gwh, solar_gwh, h2_turbine_gwh, electrolyzer_gwh, fullness = aggregate_monthly(
        times=times,
        wind_mw=wind_gen_mw,
        solar_mw=solar_gen_mw,
        electrolyzer_mw=sim["electrolyzer_draw_mw"],
        h2_turbine_mw=sim["h2_turbine_output_mw"],
        fullness_pct=sim["fullness_pct"],
        fullness_mode=args.fullness_mode,
    )

    fullness_mode_label = "end-of-month" if args.fullness_mode == "end" else "monthly average"
    out_path = Path(args.output)
    build_svg(
        out_path=out_path,
        months=MONTH_NAMES,
        wind_gwh=wind_gwh,
        solar_gwh=solar_gwh,
        h2_turbine_gwh=h2_turbine_gwh,
        electrolyzer_gwh=electrolyzer_gwh,
        fullness_pct=fullness,
        fullness_mode_label=fullness_mode_label,
    )

    print(f"chart_file={out_path}")
    print(f"summary_file={summary_path}")
    print(f"fullness_mode={args.fullness_mode}")
    print(f"annual_wind_gwh={sum(wind_gwh):.3f}")
    print(f"annual_solar_gwh={sum(solar_gwh):.3f}")
    print(f"annual_h2_turbine_gwh={sum(h2_turbine_gwh):.3f}")
    print(f"annual_electrolyzer_gwh={sum(electrolyzer_gwh):.3f}")
    print(f"min_monthly_fullness_pct={min(fullness):.3f}")
    print(f"max_monthly_fullness_pct={max(fullness):.3f}")


if __name__ == "__main__":
    main()
