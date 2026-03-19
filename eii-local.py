#!/usr/bin/env python3
"""
EII Local Calculator — Ecosystem Integrity Index via Google Earth Engine
Uses local GEE credentials (gee-wildsquare project).

Usage:
  python scripts/eii-local.py 43.08 -6.25
  python scripts/eii-local.py 43.08 -6.25 5000   # buffer in meters
"""
import sys
import json
import ee

def compute_eii(lat, lng, buffer_m=3000):
    ee.Initialize(project='gee-wildsquare')
    from eii.client import get_stats

    geometry = ee.Geometry.Point([lng, lat]).buffer(buffer_m)
    stats = get_stats(geometry, stats=['mean', 'min', 'max'], include_components=True)

    values = stats.get('values', {})
    eii_vals = values.get('eii', {})
    functional = values.get('functional_integrity', {})
    structural = values.get('structural_integrity', {})
    compositional = values.get('compositional_integrity', {})

    def to100(v):
        return round(float(v) * 100, 1) if v is not None else None

    eii_mean = to100(eii_vals.get('mean'))
    if eii_mean is None: quality = 'Sin datos'
    elif eii_mean >= 75: quality = 'Muy alta'
    elif eii_mean >= 55: quality = 'Alta'
    elif eii_mean >= 35: quality = 'Moderada'
    elif eii_mean >= 15: quality = 'Baja'
    else: quality = 'Muy baja'

    return {
        'eii_mean': eii_mean,
        'eii_min': to100(eii_vals.get('min')),
        'eii_max': to100(eii_vals.get('max')),
        'quality_label': quality,
        'functional_mean': to100(functional.get('mean')),
        'structural_mean': to100(structural.get('mean')),
        'compositional_mean': to100(compositional.get('mean')),
    }

if __name__ == '__main__':
    if len(sys.argv) < 3:
        print('Usage: python eii-local.py <lat> <lng> [buffer_m]')
        sys.exit(1)
    lat = float(sys.argv[1])
    lng = float(sys.argv[2])
    buf = int(sys.argv[3]) if len(sys.argv) > 3 else 3000
    result = compute_eii(lat, lng, buf)
    print(json.dumps(result))
