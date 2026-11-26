"""Render the README charts from the recorded compatibility and resource data."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from matplotlib.ticker import PercentFormatter


ROOT = Path(__file__).resolve().parents[1]
ENGINES = ("Chrome", "Moli", "Lightpanda", "Obscura", "Kitesurf")
THEMES = {
    "light": {
        "background": "#ffffff", "text": "#17181a", "muted": "#6c6e74", "grid": "#e7e6e4",
        "bars": dict(zip(ENGINES, ("#4285f4", "#229e58", "#096e88", "#202224", "#f56040"))),
    },
    "dark": {
        "background": "#0d1117", "text": "#f0f3f6", "muted": "#9ba4ae", "grid": "#262d36",
        "bars": dict(zip(ENGINES, ("#6ba3f8", "#2fab66", "#16809d", "#d7dadf", "#f56040"))),
    },
}


def render_overview(engines: list[dict], total: int, count_key: str, dataset: dict, colors: dict, output: Path) -> None:
    height = 1372 + (len(engines) - 4) * 244
    figure = plt.figure(figsize=(15, height / 200), dpi=200, facecolor=colors["background"])
    axis = figure.add_axes((0, 0, 1, 1), xlim=(0, 3000), ylim=(height, 0))
    axis.set_axis_off()
    axis.text(110, 68, f"{len(engines)} headless browsers on Lexbench-Headless-Browser",
              va="top", fontsize=30, weight="bold", color=colors["text"])
    population = "comparable tasks" if count_key == "comparable_passed" else "tasks"
    axis.text(110, 172, f"Browser automation compatibility · {total:,} {population}",
              va="top", fontsize=21, color=colors["muted"])
    axis.text(2890, 284, "TASK SUCCESS RATE", ha="right", fontsize=16, color=colors["muted"])

    for index, engine in enumerate(engines):
        name = engine["name"]
        center = 422 + index * 244
        rate = engine[count_key] / total
        axis.hlines(center - 122, 110, 2890, color=colors["grid"], linewidth=1.5)
        axis.text(110, center - 36, name, va="center", fontsize=30, weight="bold", color=colors["text"])
        version = engine["version"]
        if engine.get("layout"):
            version += f" · {engine['layout']}"
        axis.text(110, center + 46, version, va="center", fontsize=16, color=colors["muted"])
        if rate:
            axis.add_patch(FancyBboxPatch(
                (805, center - 42), 1610 * rate, 84, boxstyle="round,pad=0,rounding_size=12",
                linewidth=0, facecolor=colors["bars"][name],
            ))
        axis.text(2890, center, f"{rate:.1%}", ha="right", va="center",
                  fontsize=30, weight="bold", color=colors["text"])

    note = f"Local results require all {dataset['local_attempts_per_task']} attempts to pass"
    note += f" · Moli run {dataset['moli_refresh']['date']} · Other results from earlier runs"
    axis.text(110, height - 56, note, fontsize=14, color=colors["muted"])
    figure.savefig(output)
    plt.close(figure)


def render_efficiency_map(dataset: dict, colors: dict, output: Path) -> None:
    snapshot = dataset["october_snapshot"]
    resource = dataset["moli_resources"]
    moli = {**dataset["moli_refresh"], "peak_pss_mib": resource["metrics"]["pss_peak_mib"]["median"]}
    engines = [moli if engine["name"] == moli["name"] else engine for engine in snapshot["engines"]]
    figure = plt.figure(figsize=(15, 8.6), dpi=200, facecolor=colors["background"])
    axis = figure.add_axes((0.085, 0.16, 0.87, 0.665), facecolor=colors["background"])
    axis.set(xlim=(0, 780), ylim=(28, 108), xticks=range(0, 701, 100), yticks=(40, 60, 80, 100))
    axis.yaxis.set_major_formatter(PercentFormatter(100, decimals=0))
    axis.tick_params(length=0, pad=10, labelsize=10, colors=colors["muted"])
    axis.grid(color=colors["grid"], linewidth=1.2)
    axis.set_axisbelow(True)
    for spine in axis.spines.values():
        spine.set_visible(False)
    axis.set_xlabel("Median sampled peak PSS per attempt (MiB)",
                    fontsize=20, color=colors["muted"], labelpad=14)
    axis.set_ylabel("Task success rate", fontsize=20, color=colors["muted"], labelpad=12)
    offsets = {"Chrome": (-30, 42), "Moli": (34, -8), "Lightpanda": (-4, -48), "Obscura": (34, -8)}
    for engine in engines:
        name = engine["name"]
        current = engine is moli
        memory = engine["peak_pss_mib"]
        rate = 100 * engine["passed"] / dataset["tasks"]
        color = colors["bars"][name]
        axis.scatter(
            memory, rate, s=800, facecolor="none" if current else color,
            edgecolor=color if current else colors["background"], linewidth=2.5, zorder=3,
        )
        horizontal, vertical = offsets[name]
        label = f"{name} {engine['version']}" if current else name
        memory_label = f"{memory:.1f}" if current else f"{memory:.0f}"
        for content, shift, size, weight, text_color in (
            (label, 0, 24, "bold", colors["text"]),
            (f"{rate:.1f}% · {memory_label} MiB", -24, 18, "normal", colors["muted"]),
        ):
            axis.annotate(content, (memory, rate), xytext=(horizontal, vertical + shift),
                          textcoords="offset points", ha="right" if name == "Chrome" else "left",
                          fontsize=size, fontweight=weight, color=text_color)

    figure.text(0.038, 0.968, "Compatibility vs. memory on Lexbench-Headless-Browser",
                va="top", fontsize=29, weight="bold", color=colors["text"])
    subtitle = f"{dataset['tasks']:,}-task pass rate · {resource['tasks']:,}-task resource suite · Indicative samples"
    figure.text(0.038, 0.897, subtitle,
                va="top", fontsize=19, color=colors["muted"])
    figure.text(
        0.038, 0.038,
        f"Outlined marker uses {resource['passing_attempts']:,} Moli-only passes · "
        f"{snapshot['month']} points use {snapshot['shared_resource_attempts']:,} shared passes",
        fontsize=14, color=colors["muted"],
    )
    figure.savefig(output)
    plt.close(figure)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "docs/assets")
    args = parser.parse_args()
    data = json.loads((ROOT / "docs/evidence/readme-results.json").read_text(encoding="utf-8"))
    args.output_dir.mkdir(parents=True, exist_ok=True)
    moli = data["moli_refresh"]
    current = [moli if engine["name"] == moli["name"] else engine
               for engine in data["october_snapshot"]["engines"]]
    for theme, colors in THEMES.items():
        render_overview(current, data["tasks"], "passed", data, colors,
                        args.output_dir / f"four-engine-overview-{theme}.png")
        render_overview([*current, data["kitesurf"]], data["comparable_subset"]["tasks"],
                        "comparable_passed", data, colors,
                        args.output_dir / f"five-engine-caliber-b-{theme}.png")
        render_efficiency_map(data, colors, args.output_dir / f"efficiency-map-{theme}.png")


if __name__ == "__main__":
    main()
