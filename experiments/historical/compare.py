"""Rebuild standalone historical comparisons; never overwrite the hand-edited README."""

import argparse
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
RUN = ROOT / "evidence/historical/historical-20261029T1555352673760Z"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def comparison(directory):
    source = read(HERE / "table-a3.json")
    report_path = directory / "analysis/report.json"
    index = read(directory / "analysis/index.json")
    entry = next(e for e in index["files"] if e["path"] == "report.json")
    if sha(report_path) != entry["sha256"]:
        raise ValueError("Analysis report differs from its archived checksum")
    report = read(report_path)
    if report["completeness"] != "complete":
        raise ValueError("Comparison requires a complete run")
    cells = {(c["model"], c["condition"]["name"]): c for c in report["cells"].values()}
    lines = [
        "| Model | Series | Conflict + replacement | Conflict + restriction | Conflict only | Replacement only | Restriction only | Control |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    rows = []
    for model in source["models"]:
        observed, deltas = [], []
        for condition, original in zip(
            source["conditions"], model["rates_percent"], strict=True
        ):
            cell = cells[(model["run_model"], condition)]
            if cell["scored"] != 100 or cell["planned"] != 100:
                raise ValueError("Expected exactly 100 scored subjects per cell")
            rate = 100 * cell["original_harmful"] / cell["scored"]
            delta = rate - original
            observed.append(f"{rate:g}%")
            deltas.append(f"{delta:+g}" if delta else "0")
            rows.append(
                {
                    "paper_model": model["paper_label"],
                    "run_model": model["run_model"],
                    "condition": condition,
                    "paper_rate_percent": original,
                    "run_original_harmful": cell["original_harmful"],
                    "run_n": cell["scored"],
                    "run_rate_percent": rate,
                    "delta_percentage_points": delta,
                    "run_wilson_95": cell["original_wilson_95"],
                    "mapping_caveat": model.get("mapping_caveat"),
                }
            )
        for series, values in (
            ("Published A3", [f"{p}%" for p in model["rates_percent"]]),
            ("Our run", observed),
            ("**Difference (pp)**", [f"**{d}**" for d in deltas]),
        ):
            lines.append("| " + " | ".join([model["label"], series, *values]) + " |")
    return "\n".join(lines), {
        "source": source,
        "run_directory": directory.name,
        "analysis_report_sha256": sha(report_path),
        "source_transcription_sha256": sha(HERE / "table-a3.json"),
        "primary_score": "Pinned original classifier logic with substituted Astra judge; not human adjudication",
        "delta_definition": "Our original-score percentage minus published Table A3 percentage",
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=RUN)
    args = parser.parse_args()
    table, data = comparison(args.directory)
    (HERE / "comparison.md").write_text(
        "# Historical comparison with Table A3\n\n"
        "All cells have n=100. Difference = our original-score rate minus published rate, "
        "in percentage points. R1* maps provisionally to R1-0528. "
        "Source: [Appendix Table A3, pp. 29-30]("
        + data["source"]["source_url"]
        + "#page=29). See [README Appendix F](../../README.md#appendix-f-reproduce-datacenter-scenario-with-historical-models) "
        "for caveats and interpretation.\n\n" + table + "\n",
        encoding="utf-8",
    )
    (HERE / "comparison.json").write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )
    print(f"Rebuilt {len(data['rows'])} comparisons offline; no inference calls.")


if __name__ == "__main__":
    main()
