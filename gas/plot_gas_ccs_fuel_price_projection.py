#!/usr/bin/env python3
"""
Plot low/medium/high fuel-price projections for gas + CCS lifecycle costs.
"""

import argparse
import csv
import json
from pathlib import Path

import gas_ccs_cost_projection as gcc


def scenario_order(scenarios):
    preferred = ["low", "base", "high"]
    ordered = [s for s in preferred if s in scenarios]
    ordered.extend(sorted(s for s in scenarios if s not in preferred))
    return ordered


def scenario_label(name):
    labels = {
        "low": "Low fuel price",
        "base": "Medium fuel price",
        "high": "High fuel price",
    }
    return labels.get(name, name)


def scenario_color(name):
    colors = {
        "low": "#2E8B57",
        "base": "#1F77B4",
        "high": "#D62728",
    }
    return colors.get(name, None)


def write_projection_csv(path: Path, rows):
    if not rows:
        with path.open("w", newline="") as f:
            f.write("")
        return

    fieldnames = list(rows[0].keys())
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def load_wind_h2_cumulative_series(summary_path: Path, years):
    with summary_path.open() as f:
        payload = json.load(f)

    best = payload.get("best_design")
    if not isinstance(best, dict):
        raise ValueError("Wind+H2 summary does not contain best_design.")

    capex_total = float(best["capex_total"])
    annual_opex_total = float(best["annual_opex_total"])

    series = [((capex_total + annual_opex_total * y) / 1_000_000_000.0) for y in years]
    return {
        "capex_total_gbp": capex_total,
        "annual_opex_total_gbp": annual_opex_total,
        "series_billion": series,
    }


def draw_svg_projection(
    output_file: Path,
    years,
    scenario_series_billion,
    scenario_names,
    scenario_prices,
    wind_h2_series_billion=None,
    wind_h2_label=None,
):
    legend_title = "Fuel price scenarios"
    legend_texts = [
        f"{scenario_label(name)} ({float(scenario_prices[name]):.2f} GBP/MWh_th)"
        for name in scenario_names
    ]
    if wind_h2_series_billion is not None:
        legend_title = "Scenarios"
        legend_texts.append(wind_h2_label or "Wind + H2")
    max_legend_chars = max([len(legend_title)] + [len(t) for t in legend_texts])
    # Approximate text width for Arial 14-15px to keep legend box from clipping.
    legend_box_width = max(260, int(max_legend_chars * 7.4) + 64)

    height = 760
    margin_left = 95
    margin_right = 25 + legend_box_width + 20
    margin_top = 80
    margin_bottom = 80
    plot_w = 885
    plot_h = height - margin_top - margin_bottom
    width = margin_left + plot_w + margin_right

    x_min = min(years)
    x_max = max(years)
    y_min = 0.0
    y_max = max(max(vals) for vals in scenario_series_billion.values())
    if wind_h2_series_billion is not None:
        y_max = max(y_max, max(wind_h2_series_billion))
    if y_max <= y_min:
        y_max = y_min + 1.0

    def x_px(x):
        if x_max == x_min:
            return margin_left + plot_w / 2
        return margin_left + ((x - x_min) / (x_max - x_min)) * plot_w

    def y_px(y):
        return margin_top + (1.0 - (y - y_min) / (y_max - y_min)) * plot_h

    y_grid_n = 6
    y_grid_vals = [y_min + i * (y_max - y_min) / y_grid_n for i in range(y_grid_n + 1)]

    x_tick_step = 5 if (x_max - x_min) > 10 else 1
    x_ticks = list(range(int(x_min), int(x_max) + 1, x_tick_step))
    if x_ticks[-1] != x_max:
        x_ticks.append(x_max)
    x_ticks = sorted(set(x_ticks))

    lines = []
    lines.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="Gas CCS fuel price projection">'
    )
    lines.append('<rect x="0" y="0" width="100%" height="100%" fill="white"/>')
    lines.append(
        f'<text x="{width / 2:.2f}" y="36" text-anchor="middle" font-family="Arial" font-size="26" '
        'font-weight="700">Gas + CCS 25-Year Cost Projection by Fuel Price Scenario</text>'
    )

    # Grid and Y-axis labels.
    for yv in y_grid_vals:
        yp = y_px(yv)
        lines.append(
            f'<line x1="{margin_left}" y1="{yp:.2f}" x2="{margin_left + plot_w}" y2="{yp:.2f}" '
            'stroke="#d9d9d9" stroke-width="1"/>'
        )
        lines.append(
            f'<text x="{margin_left - 12}" y="{yp + 5:.2f}" text-anchor="end" '
            f'font-family="Arial" font-size="13" fill="#333">{yv:.1f}</text>'
        )

    # Axes.
    lines.append(
        f'<line x1="{margin_left}" y1="{margin_top + plot_h}" x2="{margin_left + plot_w}" '
        f'y2="{margin_top + plot_h}" stroke="#333" stroke-width="2"/>'
    )
    lines.append(
        f'<line x1="{margin_left}" y1="{margin_top}" x2="{margin_left}" '
        f'y2="{margin_top + plot_h}" stroke="#333" stroke-width="2"/>'
    )

    # X-axis labels.
    for xv in x_ticks:
        xp = x_px(xv)
        lines.append(
            f'<line x1="{xp:.2f}" y1="{margin_top + plot_h}" x2="{xp:.2f}" '
            f'y2="{margin_top + plot_h + 6}" stroke="#333" stroke-width="1"/>'
        )
        lines.append(
            f'<text x="{xp:.2f}" y="{margin_top + plot_h + 24}" text-anchor="middle" '
            f'font-family="Arial" font-size="13" fill="#333">{int(xv)}</text>'
        )

    lines.append(
        f'<text x="{margin_left + plot_w / 2:.2f}" y="{height - 18}" text-anchor="middle" '
        'font-family="Arial" font-size="16" fill="#222">Project year</text>'
    )
    lines.append(
        f'<text x="28" y="{margin_top + plot_h / 2:.2f}" transform="rotate(-90, 28, {margin_top + plot_h / 2:.2f})" '
        'text-anchor="middle" font-family="Arial" font-size="16" fill="#222">'
        "Cumulative total expenditure (GBP billion)"
        "</text>"
    )

    # Series lines.
    for name in scenario_names:
        color = scenario_color(name) or "#555555"
        points = []
        for x_val, y_val in zip(years, scenario_series_billion[name]):
            points.append(f"{x_px(x_val):.2f},{y_px(y_val):.2f}")
        points_str = " ".join(points)
        lines.append(
            f'<polyline fill="none" stroke="{color}" stroke-width="3" points="{points_str}"/>'
        )

    if wind_h2_series_billion is not None:
        points = []
        for x_val, y_val in zip(years, wind_h2_series_billion):
            points.append(f"{x_px(x_val):.2f},{y_px(y_val):.2f}")
        points_str = " ".join(points)
        lines.append(
            '<polyline fill="none" stroke="#111111" stroke-width="3" '
            'stroke-dasharray="10 6" '
            f'points="{points_str}"/>'
        )

    # Legend.
    legend_x = margin_left + plot_w + 25
    legend_y = margin_top + 30
    legend_rows = len(scenario_names) + (1 if wind_h2_series_billion is not None else 0)
    lines.append(
        f'<rect x="{legend_x - 14}" y="{legend_y - 30}" width="{legend_box_width}" height="{34 * legend_rows + 28}" '
        'fill="white" stroke="#c7c7c7" stroke-width="1"/>'
    )
    lines.append(
        f'<text x="{legend_x}" y="{legend_y - 8}" font-family="Arial" font-size="15" '
        f'font-weight="700" fill="#222">{legend_title}</text>'
    )
    for i, name in enumerate(scenario_names):
        y = legend_y + i * 34 + 14
        color = scenario_color(name) or "#555555"
        lines.append(
            f'<line x1="{legend_x}" y1="{y}" x2="{legend_x + 28}" y2="{y}" '
            f'stroke="{color}" stroke-width="4"/>'
        )
        lines.append(
            f'<text x="{legend_x + 36}" y="{y + 5}" font-family="Arial" font-size="14" fill="#333">'
            f"{legend_texts[i]}"
            "</text>"
        )

    if wind_h2_series_billion is not None:
        y = legend_y + len(scenario_names) * 34 + 14
        lines.append(
            f'<line x1="{legend_x}" y1="{y}" x2="{legend_x + 28}" y2="{y}" '
            'stroke="#111111" stroke-width="4" stroke-dasharray="10 6"/>'
        )
        lines.append(
            f'<text x="{legend_x + 36}" y="{y + 5}" font-family="Arial" font-size="14" fill="#333">'
            f"{wind_h2_label or 'Wind + H2'}"
            "</text>"
        )

    lines.append("</svg>")
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text("\n".join(lines))


def main():
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Plot gas+CCS cumulative cost projection for low/base/high fuel prices."
    )
    parser.add_argument(
        "--config",
        default=str(script_dir / "gas_ccs_config_midrange.json"),
        help="Path to gas+CCS JSON config file.",
    )
    parser.add_argument(
        "--output-file",
        help="Path for plot output file (default: <output_prefix>_fuel_price_projection.svg).",
    )
    parser.add_argument(
        "--output-csv",
        help="Path for scenario projection table CSV (default: <output_prefix>_fuel_price_projection.csv).",
    )
    parser.add_argument(
        "--wind-summary",
        default=str(script_dir.parent / "wind" / "h2_total_expenditure_opt_summary.json"),
        help=(
            "Path to wind+H2 total expenditure summary JSON. "
            "Used to add a single cumulative CAPEX+OPEX line."
        ),
    )
    parser.add_argument(
        "--wind-line-label",
        default="Wind + H2 (CAPEX + annual OPEX)",
        help="Legend label for the wind+H2 line.",
    )
    parser.add_argument(
        "--no-wind-line",
        action="store_true",
        help="Disable plotting the wind+H2 reference line.",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    cfg = gcc.load_config(config_path)
    gcc.validate_config(cfg)

    cfg.update(gcc.resolve_capacity_and_generation(cfg))
    cfg["plant_count_for_maintenance"] = gcc.get_plant_count_for_maintenance(cfg)

    scenarios = cfg["fuel_price_scenarios_gbp_per_mwh_th"]
    order = scenario_order(scenarios)
    projections = {
        name: gcc.compute_yearly_projection(cfg, float(scenarios[name])) for name in order
    }

    if args.output_file is not None:
        output_file = Path(args.output_file)
    else:
        output_file = config_path.parent / f"{cfg['output_prefix']}_fuel_price_projection.svg"

    if args.output_csv is not None:
        output_csv = Path(args.output_csv)
    else:
        output_csv = config_path.parent / f"{cfg['output_prefix']}_fuel_price_projection.csv"

    years = [r["year"] for r in projections[order[0]]["rows"]]
    wind_h2 = None
    wind_summary_path = Path(args.wind_summary)
    if not args.no_wind_line and wind_summary_path.exists():
        try:
            wind_h2 = load_wind_h2_cumulative_series(wind_summary_path, years)
        except Exception as exc:
            print(f"warning: could not load wind+H2 summary: {exc}")
            wind_h2 = None
    elif not args.no_wind_line:
        print(f"warning: wind+H2 summary not found at {wind_summary_path}")

    table_rows = []
    for i, year in enumerate(years):
        row = {"year": year}
        for name in order:
            r = projections[name]["rows"][i]
            row[f"{name}_fuel_price_gbp_per_mwh_th"] = float(scenarios[name])
            row[f"{name}_annual_opex_gbp"] = r["total_opex_gbp"]
            row[f"{name}_cumulative_opex_gbp"] = r["cumulative_opex_gbp"]
            row[f"{name}_cumulative_total_expenditure_gbp"] = r[
                "cumulative_total_expenditure_gbp"
            ]
        if wind_h2 is not None:
            row["wind_h2_cumulative_total_expenditure_gbp"] = (
                wind_h2["series_billion"][i] * 1_000_000_000.0
            )
            row["wind_h2_capex_total_gbp"] = wind_h2["capex_total_gbp"]
            row["wind_h2_annual_opex_total_gbp"] = wind_h2["annual_opex_total_gbp"]
        table_rows.append(row)

    write_projection_csv(output_csv, table_rows)

    scenario_series_billion = {}
    for name in order:
        scenario_series_billion[name] = [
            r["cumulative_total_expenditure_gbp"] / 1_000_000_000.0
            for r in projections[name]["rows"]
        ]
    draw_svg_projection(
        output_file=output_file,
        years=years,
        scenario_series_billion=scenario_series_billion,
        scenario_names=order,
        scenario_prices=scenarios,
        wind_h2_series_billion=(wind_h2["series_billion"] if wind_h2 is not None else None),
        wind_h2_label=args.wind_line_label,
    )

    print(f"plot_file={output_file}")
    print(f"table_file={output_csv}")
    print(f"scenarios={','.join(order)}")
    print(f"lifecycle_years={int(cfg['project_lifecycle_years'])}")
    for name in order:
        final_total = projections[name]["rows"][-1]["cumulative_total_expenditure_gbp"]
        print(f"{name}_final_total_gbp={final_total:.2f}")
    if wind_h2 is not None:
        print(f"wind_h2_summary={wind_summary_path}")
        print(
            f"wind_h2_final_total_gbp={wind_h2['series_billion'][-1] * 1_000_000_000.0:.2f}"
        )


if __name__ == "__main__":
    main()
