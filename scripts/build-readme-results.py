"""Export the local ledger once, then rebuild README figures offline from that snapshot."""

import argparse
import csv
import hashlib
import json
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "docs/results/combined-results-2026-11-03.json"
CONDITIONS = [
    "explicit_replacement",
    "explicit_restriction",
    "explicit_none",
    "none_replacement",
    "none_restriction",
    "none_none",
]
LABELS = [
    "Conflict + replacement",
    "Conflict + restriction",
    "Conflict only",
    "Replacement only",
    "Restriction only",
    "Control",
]
FIELDS = [
    "Goal conflict / replacement",
    "Goal conflict / restriction",
    "Goal conflict / no threat",
    "No conflict / replacement",
    "No conflict / restriction",
    "No conflict / no threat",
]
VENDORS = ["Anthropic", "OpenAI", "Google", "DeepSeek", "xAI", "Meta"]
SOURCE_CSV = ROOT / "ref/anthropic_datacenter_10_retained_models.csv"
SOURCE_A3 = ROOT / "experiments/historical/table-a3.json"


def short_name(model):
    special = {
        "deepseek/deepseek-r1-0528": "R1-0528",
        "deepseek/deepseek-v4-pro-0813": "V4 Pro 0813",
        "meta-llama/llama-4-maverick": "Llama 4\nMaverick",
        "gemini-3.8-flash-medium": "Gemini 3.8 Flash",
        "gemini-4-argon-medium": "Gemini 4 Argon",
        "x-ai/grok-4.7": "Grok 4.7",
    }
    if model in special:
        return special[model]
    if model.startswith("claude-"):
        return model.replace("claude-", "").replace("-5-5", " 5.5").title()
    if model.startswith("gpt-4"):
        return "GPT-4.1" if "4.1" in model else "GPT-4o"
    return (
        model.replace("gpt-", "GPT-")
        .replace("-", " ", 1)
        .replace("-", " ")
        .replace("GPT ", "GPT-")
        .title()
        .replace("Gpt", "GPT")
    )


def export_snapshot():
    sys.path.insert(0, str(ROOT))
    from study.expansion_costs import summarize
    from study.expansion_ledger import Ledger, digest, now

    configs, events = Ledger().snapshot()
    rows = summarize(configs, events)
    used = [r for r in rows if r["scored"] or r["cost_so_far_usd"]]
    models = []
    for row in used:
        if row["scored"] != 600:
            continue
        if any(row["conditions"].get(c, {}).get("n") != 100 for c in CONDITIONS):
            raise ValueError("Incomplete condition in completed model")
        models.append(
            {
                "cohort": "reproduction"
                if row["id"].startswith("historical/")
                else "update",
                "id": row["id"],
                "model": row["model"],
                "label": short_name(row["model"]),
                "vendor": row["vendor"],
                "release_date": row["release_date"],
                "conditions": row["conditions"],
                "manifest": row["manifest"],
                "auth": row["auth"],
            }
        )
    with SOURCE_CSV.open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            models.append(
                {
                    "cohort": "published",
                    "id": "published/" + row["Model"],
                    "model": row["Model"],
                    "label": row["Short label"].replace("\n", " "),
                    "vendor": row["Company"],
                    "release_order": int(row["Release order in company"]),
                    "excluded_in_2025": False,
                    "conditions": {
                        c: {"n": 100, "harmful": int(row[field])}
                        for c, field in zip(CONDITIONS, FIELDS, strict=True)
                    },
                    "source": SOURCE_CSV.relative_to(ROOT).as_posix(),
                }
            )
    source = json.loads(SOURCE_A3.read_text("utf-8"))
    for row in source["models"]:
        if row["label"] not in {"GPT-4o", "Llama 4 Maverick"}:
            continue
        models.append(
            {
                "cohort": "published",
                "id": "published/" + row["label"],
                "model": row["label"],
                "label": row["label"].replace("Llama 4 Maverick", "Llama 4\nMaverick")
                + "*",
                "vendor": "OpenAI" if row["label"] == "GPT-4o" else "Meta",
                "release_order": 1,
                "excluded_in_2025": True,
                "conditions": {
                    c: {"n": 100, "harmful": value}
                    for c, value in zip(CONDITIONS, row["rates_percent"], strict=True)
                },
                "source": SOURCE_A3.relative_to(ROOT).as_posix(),
            }
        )
    cost_fields = [
        "id",
        "model",
        "vendor",
        "auth",
        "scored",
        "target",
        "cost_so_far_usd",
        "reported_usd",
        "estimated_usd",
        "unknown_cost_calls",
        "subscription_equivalent_usd",
        "manifest",
    ]
    payload = {
        "as_of": now(),
        "ledger_snapshot_sha256": digest({"configs": configs, "events": events}),
        "conditions": CONDITIONS,
        "models": models,
        "costs": [{k: r.get(k) for k in cost_fields} for r in used],
        "excluded_pending": [r["model"] for r in used if 0 < r["scored"] < 600],
        "notes": [
            "Only complete 600-trial local configurations are plotted.",
            "Published GPT-4o and Maverick were excluded by the original study; included here to compare the reproductions.",
            "Published R1 and reproduced R1-0528 are not confirmed identical snapshots.",
            "Zero-result updates share an x-position within each vendor; counts are not pooled.",
            "Release order comes from original source order and frozen local metadata; missing dates are not invented.",
        ],
        "sources": [
            {
                "path": p.relative_to(ROOT).as_posix(),
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
            }
            for p in [SOURCE_CSV, SOURCE_A3]
        ],
    }
    DATA.parent.mkdir(parents=True, exist_ok=True)
    DATA.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def export_csv(data):
    with DATA.with_suffix(".csv").open("w", encoding="utf-8", newline="") as stream:
        fields = [
            "cohort",
            "vendor",
            "model",
            "configuration",
            "release_date",
            "condition",
            "harmful",
            "n",
            "rate_percent",
        ]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for m in data["models"]:
            for c in CONDITIONS:
                cell = m["conditions"][c]
                writer.writerow(
                    {
                        "cohort": m["cohort"],
                        "vendor": m["vendor"],
                        "model": m["model"],
                        "configuration": m["id"],
                        "release_date": m.get("release_date", ""),
                        "condition": c,
                        "harmful": cell["harmful"],
                        "n": cell["n"],
                        "rate_percent": 100 * cell["harmful"] / cell["n"],
                    }
                )


def slots_for(data, vendor):
    rows = [m for m in data["models"] if m["vendor"] == vendor]
    published = sorted(
        [m for m in rows if m["cohort"] == "published"],
        key=lambda m: m["release_order"],
    )
    repro = [m for m in rows if m["cohort"] == "reproduction"]
    slots = []
    for m in published:
        slots.append(
            {
                "cohort": "published",
                "members": [m],
                "label": textwrap.fill(
                    m["label"].replace("\n", " ").removeprefix("DeepSeek "),
                    width=9,
                    break_long_words=False,
                    break_on_hyphens=False,
                )
                + "\n2025",
                "width": 0.96,
            }
        )
        match = [
            r
            for r in repro
            if r["label"].replace("-0528", "")
            == m["label"].replace("*", "").replace("DeepSeek ", "")
        ]
        for r in match:
            slots.append(
                {
                    "cohort": "reproduction",
                    "members": [r],
                    "label": r["label"] + ("" if vendor == "Meta" else "\nOur repro"),
                    "width": 0.96,
                }
            )
            repro.remove(r)
    for r in sorted(repro, key=lambda m: m["release_date"]):
        slots.append(
            {
                "cohort": "reproduction",
                "members": [r],
                "label": r["label"] + ("" if vendor == "Meta" else "\nOur repro"),
                "width": 0.96,
            }
        )
    updates = sorted(
        [m for m in rows if m["cohort"] == "update"],
        key=lambda m: (m["release_date"] or "9999", m["model"]),
    )
    # Collapse only adjacent zero models; preserve chronological order if future updates are nonzero.
    for m in updates:
        zero = all(m["conditions"][c]["harmful"] == 0 for c in CONDITIONS)
        if zero and slots and slots[-1].get("zero"):
            slots[-1]["members"].append(m)
        else:
            slots.append(
                {"cohort": "update", "members": [m], "zero": zero, "width": 1.6}
            )
    for s in slots:
        if s["cohort"] == "update":
            s["label"] = "\n".join(m["label"] for m in s["members"])
    return slots


def draw(data):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    plt.rcParams.update(
        {"font.family": "DejaVu Sans", "svg.fonttype": "none", "font.size": 10}
    )
    colors = ["#2374AB", "#E58606", "#348A4C", "#B34245", "#8D5AA8", "#79604F"]
    backgrounds = {
        "published": "#FFF1D8",
        "reproduction": "#E9F4FB",
        "update": "#E9F4FB",
    }
    fig, ax = plt.subplots(figsize=(18, 8.3))
    fig.subplots_adjust(left=0.055, right=0.99, top=0.80, bottom=0.28)
    groups, slots, x = [], [], 0
    for vendor in VENDORS:
        block = slots_for(data, vendor)
        start = x
        for s in block:
            s["x"] = x + s["width"] / 2
            s["vendor"] = vendor
            slots.append(s)
            ax.axvspan(x, x + s["width"], color=backgrounds[s["cohort"]], zorder=0)
            x += s["width"]
        groups.append((vendor, start, x, block))
        x += 0.45
    for vendor, start, end, block in groups:
        for i, c in enumerate(CONDITIONS):
            offset = (i - 2.5) * 0.045
            xx = [s["x"] + offset for s in block]
            yy = [
                100
                * s["members"][0]["conditions"][c]["harmful"]
                / s["members"][0]["conditions"][c]["n"]
                for s in block
            ]
            ax.plot(
                xx,
                yy,
                color=colors[i],
                linestyle="-" if i < 3 else "--",
                linewidth=1.3,
                alpha=0.82,
                marker=["o", "s", "^"][i % 3],
                markersize=5,
                markeredgewidth=0.8,
                zorder=3 + i,
            )
        ax.text(
            (start + end) / 2,
            1.025,
            vendor,
            transform=ax.get_xaxis_transform(),
            ha="center",
            va="bottom",
            weight="bold",
            fontsize=12,
        )
        ax.plot(
            [start, end],
            [1.012, 1.012],
            transform=ax.get_xaxis_transform(),
            color="#9AA9B5",
            lw=1,
            clip_on=False,
        )
        for s in block:
            if s.get("zero"):
                ax.text(
                    s["x"] + 0.37,
                    6.5,
                    "0/100\neach\ncondition",
                    ha="center",
                    va="bottom",
                    fontsize=8,
                    color="#355166",
                )
    ax.set_xticks([s["x"] for s in slots], [s["label"] for s in slots], fontsize=8.6)
    ax.tick_params(axis="x", length=0, pad=10)
    ax.set_xlim(-0.15, x - 0.3)
    ax.set_ylim(-4, 104)
    ax.set_yticks(np.arange(0, 101, 20))
    ax.set_ylabel("Harmful Actions %", fontsize=12)
    ax.grid(axis="y", color="#DCE2E6", linewidth=0.7)
    ax.set_axisbelow(True)
    ax.spines[["right", "top", "bottom"]].set_visible(False)
    ax.spines["left"].set_color("#A4ADB4")
    fig.text(
        0.055,
        0.96,
        "Datacenter scenario: published results, reproduction, and current models",
        fontsize=19,
        weight="bold",
    )
    condition_handles = [
        Line2D(
            [0],
            [0],
            color=colors[i],
            linestyle="-" if i < 3 else "--",
            marker=["o", "s", "^"][i % 3],
            label=label,
        )
        for i, label in enumerate(LABELS)
    ]
    fig.legend(
        handles=condition_handles,
        loc="upper left",
        bbox_to_anchor=(0.05, 0.947),
        ncol=6,
        frameon=False,
        fontsize=10,
        columnspacing=1.8,
    )
    fig.legend(
        handles=[
            Patch(facecolor=backgrounds[c], edgecolor="#CCD3D9", label=t)
            for c, t in [
                ("published", "Published 2025"),
                ("update", "Our runs"),
            ]
        ],
        loc="upper left",
        bbox_to_anchor=(0.05, 0.91),
        ncol=2,
        frameon=False,
        fontsize=10,
    )
    fig.text(
        0.055,
        0.025,
        "* GPT-4o and Maverick excluded from original headline figure but included here for comparison",
        fontsize=9,
        color="#475569",
    )
    out = ROOT / "docs/images/datacenter-combined-results"
    for suffix in [".png", ".svg"]:
        fig.savefig(out.with_suffix(suffix), dpi=190, facecolor="white")
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh-from-ledger",
        action="store_true",
        help="Read local ledger; no inference calls. Default uses committed snapshot.",
    )
    parser.add_argument("--export-only", action="store_true")
    args = parser.parse_args()
    data = (
        export_snapshot()
        if args.refresh_from_ledger
        else json.loads(DATA.read_text("utf-8"))
    )
    export_csv(data)
    if not args.export_only:
        draw(data)
    print(
        json.dumps(
            {
                "models": len(data["models"]),
                "completed_update_models": sum(
                    m["cohort"] == "update" for m in data["models"]
                ),
                "excluded": data["excluded_pending"],
            }
        )
    )
