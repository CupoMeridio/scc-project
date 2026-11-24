#!/usr/bin/env python3
"""Exploratory and data-quality analysis for one OhioT1DM patient.

Reads either the original GlucoseClassification.zip or an extracted directory.
ST(t), EA(t), HR(t) are excluded from the analysis feature set. Future glucose
and risk class are targets, never predictors.
"""
from __future__ import annotations

import argparse
import io
import re
import sys
import zipfile
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError as exc:
    raise SystemExit("matplotlib is required: pip install matplotlib") from exc

TARGET_GLUCOSE = "G(t+6)"
TARGET_CLASS = "RiskClass(t+6)"
DISCARDED = ["ST(t)", "EA(t)", "HR(t)"]
CLASS_EDGES = [0.0, 3.0, 3.9, 5.0, 7.8, 10.0, 13.9, np.inf]
CLASS_LABELS = list(range(1, 8))
CLASS_NAMES = [
    "Very low [0, 3)", "Low [3, 3.9)", "Low-to-target [3.9, 5)",
    "Target [5, 7.8)", "Above-target [7.8, 10)",
    "High [10, 13.9)", "Very high [13.9, inf)",
]


def read_csv_from_source(source: Path, patient: str, split: str) -> pd.DataFrame:
    filename = f"{patient}-ws-{split}(t+30).csv"
    if source.is_file() and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            matches = [n for n in archive.namelist() if n.endswith(f"/{patient}/{filename}")]
            if not matches:
                raise FileNotFoundError(f"{filename} for patient {patient} not found in {source}")
            with archive.open(matches[0]) as member:
                return pd.read_csv(member)
    if source.is_dir():
        candidates = [source / "GlucoseClassification" / patient / filename,
                      source / patient / filename]
        for candidate in candidates:
            if candidate.is_file():
                return pd.read_csv(candidate)
        raise FileNotFoundError(f"Could not find {filename}; checked: " + ", ".join(map(str, candidates)))
    raise FileNotFoundError(f"Data source must be an existing ZIP or extracted directory: {source}")


def split_patient_args(source: Path, requested: str) -> list[str]:
    if requested.lower() != "auto":
        return [requested]
    if source.is_file() and zipfile.is_zipfile(source):
        with zipfile.ZipFile(source) as archive:
            patients = sorted({m.group(1) for n in archive.namelist()
                               if (m := re.search(r"/(\d+)/\d+-ws-training\(t\+30\)\.csv$", n))})
    elif source.is_dir():
        base = source / "GlucoseClassification"
        if not base.is_dir():
            base = source
        patients = sorted(p.name for p in base.iterdir() if p.is_dir() and p.name.isdigit())
    else:
        patients = []
    if not patients:
        raise ValueError("Could not discover patient folders; provide --patient explicitly")
    return patients


def feature_columns(df: pd.DataFrame) -> list[str]:
    excluded = set(DISCARDED + [TARGET_GLUCOSE, TARGET_CLASS])
    return [c for c in df.columns if c not in excluded]


def expected_class(glucose: pd.Series) -> pd.Series:
    # Intervals are [lower, upper); values outside [0, infinity) remain missing.
    return pd.cut(glucose, bins=CLASS_EDGES, labels=CLASS_LABELS,
                  right=False, include_lowest=True).astype("float")


def write_analysis(df: pd.DataFrame, patient: str, split: str, outdir: Path) -> None:
    outdir.mkdir(parents=True, exist_ok=True)
    stem = f"patient_{patient}_{split}"
    features = feature_columns(df)
    missing_cols = [c for c in [*features, *DISCARDED, TARGET_GLUCOSE, TARGET_CLASS] if c in df.columns]

    numeric = df.select_dtypes(include=[np.number])
    summary = numeric.describe(percentiles=[.01, .05, .25, .5, .75, .95, .99]).T
    summary.to_csv(outdir / f"{stem}_numeric_summary.csv")

    missing = pd.DataFrame({
        "missing_count": df[missing_cols].isna().sum(),
        "missing_percent": df[missing_cols].isna().mean().mul(100),
        "non_finite_count": [int((~np.isfinite(pd.to_numeric(df[c], errors="coerce"))).sum())
                             for c in missing_cols],
    })
    missing.to_csv(outdir / f"{stem}_missingness.csv")

    class_counts = df[TARGET_CLASS].value_counts(dropna=False).sort_index() if TARGET_CLASS in df else pd.Series(dtype=int)
    class_report = pd.DataFrame({"count": class_counts, "fraction": class_counts / max(len(df), 1)})
    class_report.to_csv(outdir / f"{stem}_class_distribution.csv")

    duplicate_rows = int(df.duplicated().sum())
    report_lines = [f"Patient: {patient}", f"Split: {split}", f"Rows: {len(df)}", f"Columns: {len(df.columns)}",
                    f"Duplicate rows: {duplicate_rows}", f"Feature columns used: {len(features)}",
                    "Excluded signals: " + ", ".join(c for c in DISCARDED if c in df.columns),
                    "Target columns excluded from features: " + ", ".join(c for c in [TARGET_GLUCOSE, TARGET_CLASS] if c in df.columns)]
    if TARGET_GLUCOSE in df and TARGET_CLASS in df:
        mapped = expected_class(pd.to_numeric(df[TARGET_GLUCOSE], errors="coerce"))
        actual = pd.to_numeric(df[TARGET_CLASS], errors="coerce")
        invalid_glucose = int(mapped.isna().sum())
        mismatch = int((mapped.notna() & actual.notna() & mapped.ne(actual)).sum())
        report_lines += [f"Glucose values outside defined class intervals or missing: {invalid_glucose}",
                         f"RiskClass vs glucose interval mismatches: {mismatch}"]
    for col in ["G(t)", TARGET_GLUCOSE]:
        if col in df:
            vals = pd.to_numeric(df[col], errors="coerce")
            report_lines.append(f"{col} range: {vals.min():.6g} to {vals.max():.6g}")
    (outdir / f"{stem}_data_quality.txt").write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    # Class distribution, including empty canonical classes.
    if TARGET_CLASS in df:
        counts = pd.to_numeric(df[TARGET_CLASS], errors="coerce").value_counts().reindex(CLASS_LABELS, fill_value=0)
        fig, ax = plt.subplots(figsize=(9, 5))
        ax.bar(CLASS_LABELS, counts.values, color="#4472C4")
        ax.set(xticks=CLASS_LABELS, xlabel="Risk class", ylabel="Rows", title=f"Patient {patient} {split}: target class distribution")
        ax.grid(axis="y", alpha=.25)
        fig.tight_layout(); fig.savefig(outdir / f"{stem}_class_distribution.png", dpi=160); plt.close(fig)

    # Ordered window-index plots; source CSV has no timestamp column.
    if TARGET_GLUCOSE in df:
        glucose = pd.to_numeric(df[TARGET_GLUCOSE], errors="coerce")
        current = pd.to_numeric(df["G(t)"], errors="coerce") if "G(t)" in df else None
        fig, ax = plt.subplots(figsize=(12, 5))
        if current is not None:
            ax.plot(current.to_numpy(), label="G(t), current", linewidth=.8, alpha=.75)
        ax.plot(glucose.to_numpy(), label="G(t+6), future target", linewidth=.9)
        for edge in CLASS_EDGES[1:-1]: ax.axhline(edge, color="gray", linestyle=":", linewidth=.8)
        ax.set(xlabel="Ordered window row (no timestamp supplied)", ylabel="Glucose (archive plotting code indicates mmol/L)", title=f"Patient {patient} {split}: glucose sequence")
        ax.legend(); ax.grid(alpha=.2); fig.tight_layout()
        fig.savefig(outdir / f"{stem}_glucose_sequence.png", dpi=160); plt.close(fig)

        fig, ax = plt.subplots(figsize=(8, 5))
        ax.hist(glucose.dropna(), bins=40, color="#70AD47", edgecolor="white")
        ax.set(xlabel="G(t+6)", ylabel="Rows", title=f"Patient {patient} {split}: future glucose distribution")
        ax.grid(axis="y", alpha=.2); fig.tight_layout()
        fig.savefig(outdir / f"{stem}_future_glucose_histogram.png", dpi=160); plt.close(fig)

    # Feature correlations explicitly omit discarded signals and target leakage.
    if features:
        corr = df[features].select_dtypes(include=[np.number]).corr()
        corr.to_csv(outdir / f"{stem}_feature_correlations.csv")
        if not corr.empty:
            fig, ax = plt.subplots(figsize=(max(9, len(corr.columns) * .34), max(7, len(corr.columns) * .30)))
            image = ax.imshow(corr.to_numpy(), vmin=-1, vmax=1, cmap="coolwarm", aspect="auto")
            ax.set_xticks(range(len(corr.columns)), corr.columns, rotation=90, fontsize=6)
            ax.set_yticks(range(len(corr.index)), corr.index, fontsize=6)
            ax.set_title(f"Patient {patient} {split}: analysis-feature correlations")
            fig.colorbar(image, ax=ax, shrink=.7, label="Pearson correlation")
            fig.tight_layout(); fig.savefig(outdir / f"{stem}_feature_correlations.png", dpi=160); plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True, help="GlucoseClassification.zip or extracted archive directory")
    parser.add_argument("--patient", default="591", help="Patient ID, or 'auto' to analyze all discovered patients (default: 591)")
    parser.add_argument("--split", choices=["training", "testing", "both"], default="both")
    parser.add_argument("--output", type=Path, default=Path("ohio_analysis"), help="Output directory")
    args = parser.parse_args()
    if not args.data.exists():
        parser.error(f"--data does not exist: {args.data}")
    splits = ["training", "testing"] if args.split == "both" else [args.split]
    try:
        for patient in split_patient_args(args.data, args.patient):
            for split in splits:
                df = read_csv_from_source(args.data, patient, split)
                required = {TARGET_GLUCOSE, TARGET_CLASS}
                if not required.issubset(df.columns):
                    raise ValueError(f"{patient} {split}: missing required target column(s): {sorted(required - set(df.columns))}")
                outdir = args.output / f"patient_{patient}"
                write_analysis(df, patient, split, outdir)
                print(f"Analyzed patient {patient} {split}: {len(df):,} rows, {len(df.columns)} columns -> {outdir}")
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
