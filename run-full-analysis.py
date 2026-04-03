#!/usr/bin/env python3
"""
run-full-analysis.py updated — Calls Full_Analysis v2 with CLI args (no regex patching).

Usage:
  python run-full-analysis.py --lat 43.08 --lng -6.25 --buffer 5000 --name Somiedo
  python run-full-analysis.py --lat 39.44 --lng -2.46 --buffer 3000 --name LaGranja --profile CER --tier tier2
  python run-full-analysis.py --lat 39.44 --lng -2.46 --buffer 3000 --name LaGranja --json

Outputs JSON with: LECI, PatchIntegrity, Connectivity, HQ, TreeCanopy, FireRisk, DroughtRisk,
                    StructureScore (habitat-adaptive), PNOA Tier 2 (if --tier tier2)
"""
import sys
import os
import json
import argparse
import subprocess
import csv
import tempfile

FULL_ANALYSIS_DIR = os.environ.get(
    "FULL_ANALYSIS_DIR",
    "/Users/blanca/Documents/wti-repos/Full_Analysis"
)
FULL_ANALYSIS_SCRIPT = os.path.join(FULL_ANALYSIS_DIR, "full_analysis.py")


def run_full_analysis(lat, lng, buffer_m, name, output_dir,
                      profile="auto", tier="tier1"):
    """Run Full_Analysis v2 via CLI args (no patching)."""
    work_dir = output_dir or tempfile.mkdtemp(prefix=f"ws_{name}_")
    os.makedirs(work_dir, exist_ok=True)

    json_mode = '--json' in sys.argv
    log = lambda msg: print(msg, file=sys.stderr) if json_mode else print(msg)

    log(f"\n  Running Full_Analysis v2 for {name}")
    log(f"  Location: ({lat}, {lng}), buffer={buffer_m}m")
    log(f"  Profile: {profile}, Tier: {tier}")
    log(f"  Output: {work_dir}")

    # Call Full_Analysis with proper CLI args (v2 supports argparse)
    cmd = [
        sys.executable, FULL_ANALYSIS_SCRIPT,
        "--lat", str(lat),
        "--lon", str(lng),
        "--buffer", str(buffer_m),
        "--profile", profile,
        "--tier", tier,
        "--output", work_dir,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=2400,  # 40 min (tier2 with PNOA needs more time)
        )
        if result.returncode != 0:
            err = result.stderr[-500:] if result.stderr else "unknown"
            log(f"  ⚠ Full_Analysis failed (exit {result.returncode}):\n{err}")
            return {"error": err}
        if result.stdout:
            for line in result.stdout.strip().split('\n')[-20:]:
                log(f"  {line}")
    except subprocess.TimeoutExpired:
        log("  ⚠ Full_Analysis timed out (40 min)")
        return {"error": "timeout"}
    except Exception as e:
        log(f"  ⚠ Full_Analysis error: {e}")
        return {"error": str(e)}

    # Read results from CSVs
    results = {}

    csv_files = {
        "leci": ("LECI_Metrics.csv", {
            "score": "LECI_structure_norm",
            "density": "density_m_per_ha",
            "vegetated_pct": "vegetated_pct",
        }),
        "structure_score": ("Final_Structure_Score.csv", {
            "composite": ["Structure_Score", "Composite"],
            "profile": "Profile",
            "leci_norm": "LECI_norm",
            "patch_norm": "PatchStructure_norm",
            "connectivity_norm": "DeltaIIC_norm",
            "hq_norm": "HQ_norm",
            "canopy_norm": "Canopy_norm",
            "fire_norm": "FireRisk_norm",
            "drought_norm": "Drought_norm",
            "pnoa_density": "PNOA_density_m_ha",
            "pnoa_tree_count": "PNOA_tree_count",
        }),
    }

    for key, (filename, fields) in csv_files.items():
        csv_path = os.path.join(work_dir, filename)
        if not os.path.exists(csv_path):
            # Try in LECI_RESULTS subfolder
            csv_path = os.path.join(work_dir, "LECI_RESULTS", filename)
        if os.path.exists(csv_path):
            with open(csv_path) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    entry = {}
                    for out_key, csv_key in fields.items():
                        if isinstance(csv_key, list):
                            for k in csv_key:
                                if k in row and row[k]:
                                    entry[out_key] = _safe_float(row[k])
                                    break
                        elif csv_key in row:
                            entry[out_key] = _safe_float(row[csv_key])
                    results[key] = entry
                    break

    # Tree Canopy
    for subdir in [work_dir, os.path.join(FULL_ANALYSIS_DIR, "Tree_canopy_results")]:
        canopy_csv = os.path.join(subdir, "TreeCanopy_Metrics.csv")
        if os.path.exists(canopy_csv):
            with open(canopy_csv) as f:
                for row in csv.DictReader(f):
                    results["tree_canopy"] = {
                        "canopy_pct": _safe_float(row.get("canopy_percentage", 0)),
                        "canopy_area_ha": _safe_float(row.get("canopy_area_ha", 0)),
                    }
                    break
            break

    # Fire & Drought
    for subdir in [work_dir, os.path.join(FULL_ANALYSIS_DIR, "Ecological_risk_results")]:
        for name_csv, result_key in [("FireRisk_Metrics.csv", "fire_risk"),
                                      ("DroughtRisk_Metrics.csv", "drought_risk")]:
            fpath = os.path.join(subdir, name_csv)
            if fpath and os.path.exists(fpath):
                with open(fpath) as f:
                    for row in csv.DictReader(f):
                        results[result_key] = {k: _safe_float(v) for k, v in row.items() if v}
                        break

    # PNOA Vegetation (Tier 2)
    pnoa_csv = os.path.join(work_dir, "PNOA_Vegetation_Metrics.csv")
    if os.path.exists(pnoa_csv):
        with open(pnoa_csv) as f:
            for row in csv.DictReader(f):
                results["pnoa"] = {k: _safe_float(v) for k, v in row.items() if v}
                break

    results["output_dir"] = work_dir
    results["profile"] = profile
    results["tier"] = tier
    return results


def _safe_float(v):
    """Convert to float, return original string if not numeric."""
    try:
        return float(v)
    except (ValueError, TypeError):
        return v


def main():
    parser = argparse.ArgumentParser(
        description="Run Full_Analysis v2 (habitat-adaptive) for WildCity/WS-BI"
    )
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lng", type=float, required=True)
    parser.add_argument("--buffer", type=int, default=5000)
    parser.add_argument("--name", type=str, default="territory")
    parser.add_argument("--profile", default="auto",
                        choices=["auto", "CER", "OLV", "DEH", "BOS", "RIB", "MAT", "URB"])
    parser.add_argument("--tier", default="tier1", choices=["tier1", "tier2"])
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    results = run_full_analysis(
        args.lat, args.lng, args.buffer, args.name,
        args.output_dir, args.profile, args.tier
    )

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"  Full_Analysis v2 — {args.name} ({args.profile})")
        print(f"{'='*60}")
        for key, val in results.items():
            if key == "output_dir":
                continue
            if isinstance(val, dict):
                print(f"\n  {key}:")
                for k, v in val.items():
                    print(f"    {k}: {v}")
            else:
                print(f"  {key}: {val}")
        print(f"\n  Output: {results.get('output_dir', 'N/A')}")


if __name__ == "__main__":
    main()
