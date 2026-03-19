#!/usr/bin/env python3
"""
Wrapper for WILD-SQUARE/Full_Analysis — runs all structure indicators for a municipality.

Usage:
  python scripts/run-full-analysis.py --lat 43.08 --lng -6.25 --buffer 5000 --name Somiedo
  python scripts/run-full-analysis.py --lat 43.31 --lng -5.07 --buffer 5000 --name "Cangas de Onis"

Outputs JSON with: LECI, PatchIntegrity, Connectivity (Delta-IIC), HabitatQuality,
                    TreeCanopy, FireRisk, DroughtRisk, StructureScore
"""
import sys
import os
import json
import argparse
import importlib.util
import shutil
import tempfile

FULL_ANALYSIS_DIR = "/Users/blanca/Documents/wti-repos/Full_Analysis"

def patch_and_run(lat, lng, buffer_m, name, output_dir):
    """
    Patches full_analysis.py config and runs it, returning results as dict.
    """
    # Read original script
    script_path = os.path.join(FULL_ANALYSIS_DIR, "full_analysis.py")
    with open(script_path, "r") as f:
        code = f.read()

    # Patch configuration variables
    import re
    code = re.sub(r'LONGITUDE\s*=\s*[\-\d.]+', f'LONGITUDE = {lng}', code)
    code = re.sub(r'LATITUDE\s*=\s*[\-\d.]+', f'LATITUDE = {lat}', code)
    code = re.sub(r'BUFFER_RADIUS_M\s*=\s*\d+', f'BUFFER_RADIUS_M = {buffer_m}', code)

    # Auto-detect UTM zone for Spain/Portugal
    utm_zone = int((lng + 180) / 6) + 1
    hemisphere = 'N' if lat >= 0 else 'S'
    epsg = 32600 + utm_zone if hemisphere == 'N' else 32700 + utm_zone
    code = re.sub(r'TARGET_CRS\s*=\s*"EPSG:\d+"', f'TARGET_CRS = "EPSG:{epsg}"', code)

    # Write patched script to temp location
    work_dir = output_dir or tempfile.mkdtemp(prefix=f"wti_{name}_")
    os.makedirs(work_dir, exist_ok=True)
    patched_path = os.path.join(work_dir, "full_analysis_patched.py")
    with open(patched_path, "w") as f:
        f.write(code)

    # Set output dirs
    os.makedirs(os.path.join(work_dir, "LECI_RESULTS"), exist_ok=True)
    os.makedirs(os.path.join(work_dir, "Tree_canopy_results"), exist_ok=True)
    os.makedirs(os.path.join(work_dir, "Ecological_risk_results"), exist_ok=True)

    # Run the patched script
    json_mode = '--json' in sys.argv
    log = lambda msg: print(msg, file=sys.stderr) if json_mode else print(msg)
    log(f"\n  Running Full_Analysis for {name} ({lat}, {lng}) buffer={buffer_m}m...")
    log(f"  Output dir: {work_dir}")
    log(f"  UTM zone: EPSG:{epsg}")
    log(f"  This may take 10-20 minutes...\n")

    import subprocess
    try:
        result = subprocess.run(
            ["python", patched_path],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        if result.returncode != 0:
            # Print stderr for debugging
            err = result.stderr[-500:] if result.stderr else "unknown"
            print(f"  ⚠ Full_Analysis failed (exit {result.returncode}):\n{err}", file=sys.stderr)
            return {"error": err}
        if result.stdout:
            lines = result.stdout.strip().split('\n')
            for line in lines[-20:]:
                log(f"  {line}")
    except subprocess.TimeoutExpired:
        print("  ⚠ Full_Analysis timed out (30 min)", file=sys.stderr)
        return {"error": "timeout"}
    except Exception as e:
        print(f"  ⚠ Full_Analysis error: {e}", file=sys.stderr)
        return {"error": str(e)}

    # Read results from CSVs
    results = {}

    # LECI
    leci_csv = os.path.join(work_dir, "LECI_RESULTS", "LECI_Metrics.csv")
    if os.path.exists(leci_csv):
        import csv
        with open(leci_csv) as f:
            reader = csv.DictReader(f)
            for row in reader:
                results["leci"] = {
                    "score": float(row.get("LECI_structure_norm", 0)),
                    "density": float(row.get("density_m_per_ha", 0)),
                    "vegetated_pct": float(row.get("vegetated_pct", 0)),
                }
                break

    # Patch Integrity
    patch_csv = os.path.join(work_dir, "LECI_RESULTS", "PatchMetrics_v1.csv")
    if os.path.exists(patch_csv):
        import csv
        with open(patch_csv) as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            if rows:
                # Aggregate
                areas = [float(r.get("area_ha", 0)) for r in rows]
                results["patch_integrity"] = {
                    "num_patches": len(rows),
                    "total_area_ha": sum(areas),
                    "mean_area_ha": sum(areas) / len(areas) if areas else 0,
                }

    # Connectivity
    conn_csv = os.path.join(work_dir, "LECI_RESULTS", "Connectivity_Metrics.csv")
    if os.path.exists(conn_csv):
        import csv
        with open(conn_csv) as f:
            reader = csv.DictReader(f)
            for row in reader:
                results["connectivity"] = {
                    "delta_iic_norm": float(row.get("DeltaIIC_norm", 0)),
                    "iic_current": float(row.get("IIC_current", 0)),
                }
                break

    # Habitat Quality
    hq_csv = os.path.join(work_dir, "LECI_RESULTS", "HabitatQuality_Metrics.csv")
    if os.path.exists(hq_csv):
        import csv
        with open(hq_csv) as f:
            reader = csv.DictReader(f)
            for row in reader:
                results["habitat_quality"] = {
                    "hq_norm": float(row.get("HQ_norm", 0)),
                    "hq_mean": float(row.get("HQ_mean", 0)),
                }
                break

    # Structure Score
    struct_csv = os.path.join(work_dir, "LECI_RESULTS", "Final_Structure_Score.csv")
    if os.path.exists(struct_csv):
        import csv
        with open(struct_csv) as f:
            reader = csv.DictReader(f)
            for row in reader:
                results["structure_score"] = {
                    "composite": float(row.get("Structure_Score", row.get("Composite", 0))),
                    "leci_norm": float(row.get("LECI_norm", 0)),
                    "patch_norm": float(row.get("PatchStructure_norm", 0)),
                    "connectivity_norm": float(row.get("DeltaIIC_norm", 0)),
                    "hq_norm": float(row.get("HQ_norm", 0)),
                }
                break

    # Tree Canopy
    canopy_csv = os.path.join(work_dir, "Tree_canopy_results", "TreeCanopy_Metrics.csv")
    if os.path.exists(canopy_csv):
        import csv
        with open(canopy_csv) as f:
            reader = csv.DictReader(f)
            for row in reader:
                results["tree_canopy"] = {
                    "canopy_pct": float(row.get("canopy_percentage", 0)),
                    "canopy_area_ha": float(row.get("canopy_area_ha", row.get("area_ha", 0))),
                }
                break

    # Fire Risk
    fire_csv = os.path.join(work_dir, "Ecological_risk_results", "FireRisk_Metrics.csv")
    if os.path.exists(fire_csv):
        import csv
        with open(fire_csv) as f:
            reader = csv.DictReader(f)
            for row in reader:
                results["fire_risk"] = {k: float(v) for k, v in row.items() if v}
                break

    # Drought Risk
    drought_csv = os.path.join(work_dir, "Ecological_risk_results", "DroughtRisk_Metrics.csv")
    if os.path.exists(drought_csv):
        import csv
        with open(drought_csv) as f:
            reader = csv.DictReader(f)
            for row in reader:
                results["drought_risk"] = {k: float(v) for k, v in row.items() if v}
                break

    results["output_dir"] = work_dir
    return results


def main():
    parser = argparse.ArgumentParser(description="Run Full_Analysis for WTI indicators")
    parser.add_argument("--lat", type=float, required=True)
    parser.add_argument("--lng", type=float, required=True)
    parser.add_argument("--buffer", type=int, default=5000, help="Buffer radius in meters")
    parser.add_argument("--name", type=str, default="territory")
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument("--json", action="store_true", help="Output only JSON (for piping)")
    args = parser.parse_args()

    results = patch_and_run(args.lat, args.lng, args.buffer, args.name, args.output_dir)

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        print(f"\n{'='*60}")
        print(f"  Full_Analysis Results — {args.name}")
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
