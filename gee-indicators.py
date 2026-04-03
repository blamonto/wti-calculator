#!/usr/bin/env python3
"""
GEE Indicators — Compute ALL satellite-based WTI indicators in a single GEE session.

Input: GeoJSON polygon via stdin or --geojson-file
Output: JSON with all indicators

Usage:
  python get-municipality-geometry.py "Somiedo" | python gee-indicators.py
  python gee-indicators.py --geojson-file somiedo.geojson

Indicators computed:
  - EII (Ecosystem Integrity Index) — Leutner et al. 2024
  - MODIS Burned Area — Giglio et al. 2018
  - VIIRS Nighttime Lights — Elvidge et al. 2017
  - WDPA Protected Areas — UNEP-WCMC 2024
  - Shannon Diversity — ESA WorldCover 2021
  - Canopy Height — ETH Lang et al. 2023
  - NDVI — Sentinel-2 SR Harmonized
  - Land Cover fractions — ESA WorldCover 2021
  - Carbon Stock — IPCC Tier 1 + ETH + NDVI
  - Patch Integrity — WorldCover + ETH Canopy
"""
import sys
import json
import math
import argparse
import ee

GEE_PROJECT = 'gee-wildsquare'

def init_gee():
    ee.Initialize(project=GEE_PROJECT)

def geojson_to_ee(geom_dict):
    """Convert GeoJSON geometry dict to ee.Geometry."""
    gtype = geom_dict['type']
    coords = geom_dict['coordinates']
    if gtype == 'Polygon':
        return ee.Geometry.Polygon(coords)
    elif gtype == 'MultiPolygon':
        return ee.Geometry.MultiPolygon(coords)
    else:
        raise ValueError(f"Unsupported geometry type: {gtype}")

# ─── EII ──────────────────────────────────────────────────────────────────
def compute_eii(geom):
    """EII via Leutner et al. (2024) eii package."""
    try:
        from eii.client import get_stats
        stats = get_stats(geom, stats=['mean', 'min', 'max'], include_components=True)
        values = stats.get('values', {})
        eii_vals = values.get('eii', {})
        func = values.get('functional_integrity', {})
        struct = values.get('structural_integrity', {})
        comp = values.get('compositional_integrity', {})
        to100 = lambda v: round(float(v) * 100, 1) if v is not None else None
        mean = to100(eii_vals.get('mean'))
        quality = 'Muy alta' if mean and mean >= 75 else 'Alta' if mean and mean >= 55 else 'Moderada' if mean and mean >= 35 else 'Baja' if mean and mean >= 15 else 'Muy baja' if mean else 'Sin datos'
        return {
            'mean': mean, 'min': to100(eii_vals.get('min')), 'max': to100(eii_vals.get('max')),
            'quality': quality,
            'functional': to100(func.get('mean')),
            'structural': to100(struct.get('mean')),
            'compositional': to100(comp.get('mean')),
        }
    except Exception as e:
        print(f"  EII service error: {e}. Computing local proxy...", file=sys.stderr)
        # FALLBACK: local EII proxy from WorldCover + NDVI
        # Not equivalent to Leutner et al. but avoids total failure
        try:
            wc = ee.ImageCollection('ESA/WorldCover/v200').first().select('Map')
            natural = wc.eq(10).Or(wc.eq(20)).Or(wc.eq(30)).Or(wc.eq(80)).Or(wc.eq(90))
            natural_pct = natural.reduceRegion(
                reducer=ee.Reducer.mean(), geometry=geom, scale=100, maxPixels=1e8
            ).get('Map')
            nat_val = ee.Number(natural_pct).multiply(100).getInfo() or 0

            s2 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
                .filterBounds(geom).filterDate('2024-04-01', '2024-09-30') \
                .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20))
            ndvi = s2.map(lambda img: img.normalizedDifference(['B8', 'B4'])).median()
            ndvi_val = ndvi.reduceRegion(
                reducer=ee.Reducer.mean(), geometry=geom, scale=100, maxPixels=1e8
            ).getInfo().get('B8', 0) or 0
            ndvi_score = min(100, ndvi_val * 100 / 0.8) if ndvi_val else 0

            # Proxy EII = 60% natural cover + 40% NDVI vigor
            proxy = round(nat_val * 0.6 + ndvi_score * 0.4, 1)
            return {
                'mean': proxy, 'quality': 'Proxy (EII service unavailable)',
                'functional': round(ndvi_score, 1),
                'structural': round(nat_val, 1),
                'compositional': None,
                'note': f'EII proxy: 60% natural cover ({nat_val:.0f}) + 40% NDVI ({ndvi_score:.0f}). '
                        f'Original error: {str(e)[:100]}'
            }
        except Exception as e2:
            print(f"  EII proxy also failed: {e2}", file=sys.stderr)
            return {'mean': None, 'error': str(e), 'proxy_error': str(e2)}

# ─── MODIS Burned Area ────────────────────────────────────────────────────
def compute_burned_area(geom):
    """% of municipality burned 2001-2024 using MODIS MCD64A1 (Giglio et al. 2018)."""
    burned_col = ee.ImageCollection('MODIS/061/MCD64A1') \
        .filterBounds(geom) \
        .filterDate('2001-01-01', '2024-12-31') \
        .select('BurnDate')

    # Any pixel that burned at least once
    ever_burned = burned_col.map(lambda img: img.gt(0)).reduce(ee.Reducer.max()).rename('burned')
    burned_area = ever_burned.multiply(ee.Image.pixelArea()).reduceRegion(
        reducer=ee.Reducer.sum(), geometry=geom, scale=500, maxPixels=1e9
    )
    total_area = ee.Image.pixelArea().reduceRegion(
        reducer=ee.Reducer.sum(), geometry=geom, scale=500, maxPixels=1e9
    )
    ba = burned_area.get('burned')
    ta = total_area.get('area')
    pct = ee.Number(ba).divide(ee.Number(ta)).multiply(100).getInfo()
    pct = round(pct, 1) if pct else 0
    score = max(0, round(100 - 2 * pct, 1))
    return {'burned_pct': pct, 'score': score, 'period': '2001-2024', 'source': 'MODIS MCD64A1'}

# ─── VIIRS Nighttime Lights ───────────────────────────────────────────────
def compute_viirs(geom):
    """Mean nighttime radiance using VIIRS DNB Annual V2.2 (Elvidge et al. 2017)."""
    viirs = ee.ImageCollection('NOAA/VIIRS/DNB/ANNUAL_V22') \
        .filterBounds(geom) \
        .filterDate('2022-01-01', '2024-12-31') \
        .select('average')
    mean_rad = viirs.mean().reduceRegion(
        reducer=ee.Reducer.mean(), geometry=geom, scale=500, maxPixels=1e9
    )
    rad = mean_rad.get('average')
    rad_val = ee.Number(rad).getInfo()
    rad_val = round(rad_val, 3) if rad_val else 0
    # Score: 0 nW = 100, ≥10 nW = 0
    score = max(0, round(100 - (rad_val / 10) * 100, 1))
    return {'radiance_nw': rad_val, 'score': score, 'period': '2022-2024', 'source': 'VIIRS DNB V2.2'}

# ─── WDPA Protected Areas ────────────────────────────────────────────────
def compute_protected_areas(geom):
    """% of municipality under protection using WDPA (UNEP-WCMC 2024)."""
    wdpa = ee.FeatureCollection('WCMC/WDPA/current/polygons').filterBounds(geom)
    # Calculate overlap using union of protected area geometries
    n_areas = wdpa.size().getInfo()
    if n_areas > 0:
        protected_union = wdpa.union(1).geometry()
        intersection = protected_union.intersection(geom, 1)
        protected_area_m2 = intersection.area(1).getInfo()
        total_area_m2 = geom.area(1).getInfo()
        pct = round((protected_area_m2 / total_area_m2) * 100, 1) if total_area_m2 else 0
    else:
        pct = 0
    # Get area names
    names = wdpa.aggregate_array('NAME').distinct().getInfo() or []
    desigs = wdpa.aggregate_array('DESIG_ENG').distinct().getInfo() or []
    return {
        'pct': min(100, pct), 'score': min(100, pct),
        'areas': [{'name': n, 'designation': d} for n, d in zip(names[:10], desigs[:10])],
        'source': 'WDPA (UNEP-WCMC 2024)'
    }

# ─── ESA WorldCover ───────────────────────────────────────────────────────
def compute_landcover(geom):
    """Land cover fractions + Shannon H' from ESA WorldCover 2021 (Zanaga et al. 2022)."""
    wc = ee.ImageCollection('ESA/WorldCover/v200').first()
    # Band is 'Map' in v200
    wc = wc.select(['Map'])
    # Count pixels per class
    classes = [10, 20, 30, 40, 50, 60, 70, 80, 90, 95, 100]
    class_names = {10:'Forest', 20:'Shrubland', 30:'Grassland', 40:'Cropland', 50:'Builtup',
                   60:'Bare', 70:'Snow', 80:'Water', 90:'Wetland', 95:'Mangrove', 100:'Moss'}
    total_pixels = wc.gte(0).reduceRegion(
        reducer=ee.Reducer.count(), geometry=geom, scale=10, maxPixels=1e9
    ).get('Map')
    total = ee.Number(total_pixels).getInfo()
    if not total or total == 0:
        return {'error': 'No WorldCover data'}

    fractions = {}
    for cls in classes:
        count = wc.eq(cls).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=geom, scale=10, maxPixels=1e9
        ).get('Map')
        c = ee.Number(count).getInfo() or 0
        fractions[class_names[cls]] = round((c / total) * 100, 2)

    # Shannon H'
    h_prime = 0
    for cls_name, pct in fractions.items():
        if pct > 0:
            p = pct / 100
            h_prime -= p * math.log(p)
    n_present = sum(1 for p in fractions.values() if p > 0)
    h_max = math.log(n_present) if n_present > 1 else 1
    shannon_norm = round((h_prime / h_max) * 100, 1) if h_max > 0 else 0

    forest_pct = fractions.get('Forest', 0)
    natural_pct = forest_pct + fractions.get('Shrubland', 0) + fractions.get('Grassland', 0) + fractions.get('Wetland', 0) + fractions.get('Moss', 0)
    builtup_pct = fractions.get('Builtup', 0)

    return {
        'fractions': fractions,
        'forest_pct': round(forest_pct, 1),
        'natural_pct': round(natural_pct, 1),
        'builtup_pct': round(builtup_pct, 1),
        'shannon_h': round(h_prime, 3),
        'shannon_normalized': shannon_norm,
        'n_classes_present': n_present,
        'source': 'ESA WorldCover v200 (2021)'
    }

# ─── ETH Canopy Height ───────────────────────────────────────────────────
def compute_canopy(geom):
    """Mean canopy height from ETH Global Canopy Height 2020 (Lang et al. 2023)."""
    canopy = ee.Image('users/nlang/ETH_GlobalCanopyHeight_2020_10m_v1')
    stats = canopy.reduceRegion(
        reducer=ee.Reducer.mean().combine(ee.Reducer.percentile([90]), sharedInputs=True),
        geometry=geom, scale=10, maxPixels=1e9
    )
    mean_h = ee.Number(stats.get('b1_mean')).getInfo()
    p90_h = ee.Number(stats.get('b1_p90')).getInfo()
    # % mature forest (canopy > 15m)
    mature = canopy.gt(15).reduceRegion(
        reducer=ee.Reducer.mean(), geometry=geom, scale=10, maxPixels=1e9
    )
    mature_pct = ee.Number(mature.get('b1')).multiply(100).getInfo()
    return {
        'mean_height_m': round(mean_h, 1) if mean_h else 0,
        'p90_height_m': round(p90_h, 1) if p90_h else 0,
        'mature_forest_pct': round(mature_pct, 1) if mature_pct else 0,
        'source': 'ETH Global Canopy Height 2020 (Lang et al. 2023)'
    }

# ─── Sentinel-2 NDVI ─────────────────────────────────────────────────────
def compute_ndvi(geom):
    """Mean NDVI from Sentinel-2 SR (2024 growing season)."""
    s2 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
        .filterBounds(geom) \
        .filterDate('2024-04-01', '2024-09-30') \
        .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20))

    def add_ndvi(img):
        scl = img.select('SCL')
        mask = scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10))
        ndvi = img.updateMask(mask).normalizedDifference(['B8', 'B4']).rename('NDVI')
        return ndvi

    ndvi = s2.map(add_ndvi).mean()
    stats = ndvi.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=geom, scale=10, maxPixels=1e9
    )
    mean_ndvi = ee.Number(stats.get('NDVI')).getInfo()
    return {'mean': round(mean_ndvi, 4) if mean_ndvi else 0, 'period': '2024 Apr-Sep', 'source': 'Sentinel-2 L2A'}

# ─── Water Quality (Turbidity + Chlorophyll-a) ────────────────────────────
# Based on WILD-SQUARE/sentinel-evalscripts
def compute_water_quality(geom):
    """
    Water turbidity (NDTI) and Chlorophyll-a from Sentinel-2.
    Formulas from sentinel-evalscripts repo:
      - Turbidity (NTU): 8.93 × (B03/B01) - 6.39  (Se2WaQ model)
      - Chlorophyll-a (mg/m³): 4.26 × (B03/B01)^3.94
      - NDCI: (B05 - B04) / (B05 + B04)
    Only computed over water pixels (NDWI > 0).
    Source: Dogliotti et al. (2015), Mishra & Mishra (2012).
    """
    s2 = ee.ImageCollection('COPERNICUS/S2_SR_HARMONIZED') \
        .filterBounds(geom) \
        .filterDate('2024-01-01', '2024-12-31') \
        .filter(ee.Filter.lt('CLOUDY_PIXEL_PERCENTAGE', 20))

    def compute_indices(img):
        scl = img.select('SCL')
        mask = scl.neq(3).And(scl.neq(8)).And(scl.neq(9)).And(scl.neq(10))
        img = img.updateMask(mask)
        b01 = img.select('B1').divide(10000)
        b03 = img.select('B3').divide(10000)
        b04 = img.select('B4').divide(10000)
        b05 = img.select('B5').divide(10000)
        b08 = img.select('B8').divide(10000)
        b11 = img.select('B11').divide(10000)
        # Water mask: NDWI > 0
        ndwi = b03.subtract(b08).divide(b03.add(b08)).rename('NDWI')
        water_mask = ndwi.gt(0)
        # Turbidity (NTU) — Se2WaQ model
        ratio = b03.divide(b01.max(0.001))
        turbidity = ratio.multiply(8.93).subtract(6.39).rename('turbidity')
        # Chlorophyll-a (mg/m³)
        chla = ratio.pow(3.94).multiply(4.26).rename('chla')
        # NDCI (Normalized Difference Chlorophyll Index)
        ndci = b05.subtract(b04).divide(b05.add(b04).max(0.001)).rename('NDCI')
        return turbidity.addBands(chla).addBands(ndci).addBands(ndwi).updateMask(water_mask)

    composite = s2.map(compute_indices).median()
    stats = composite.reduceRegion(
        reducer=ee.Reducer.mean(), geometry=geom, scale=20, maxPixels=1e9
    )
    turb = stats.get('turbidity')
    chla = stats.get('chla')
    ndci = stats.get('NDCI')

    turb_val = ee.Number(turb).getInfo() if turb else None
    chla_val = ee.Number(chla).getInfo() if chla else None
    ndci_val = ee.Number(ndci).getInfo() if ndci else None

    # Score turbidity: 0 NTU = 100, >20 NTU = 0
    turb_score = max(0, round(100 - (turb_val / 20 * 100), 1)) if turb_val and turb_val > 0 else 50
    # Score chla: <6 mg/m³ (oligotrophic) = 100, >30 = 0
    chla_score = max(0, round(100 - (chla_val / 30 * 100), 1)) if chla_val and chla_val > 0 else 50

    return {
        'turbidity_ntu': round(turb_val, 2) if turb_val else None,
        'turbidity_score': turb_score,
        'chla_mg_m3': round(chla_val, 2) if chla_val else None,
        'chla_score': chla_score,
        'ndci': round(ndci_val, 4) if ndci_val else None,
        'source': 'Sentinel-2 L2A (Se2WaQ: Dogliotti 2015, Mishra & Mishra 2012)',
        'script_ref': 'WILD-SQUARE/sentinel-evalscripts'
    }

# ─── Carbon Stock ─────────────────────────────────────────────────────────
def compute_carbon(geom, canopy_data, landcover_data, ndvi_data, area_km2):
    """
    Estimate carbon stock using IPCC Tier 1 + ETH Canopy + NDVI.
    Based on IPCC (2006) Guidelines for National GHG Inventories.
    """
    mean_height = canopy_data.get('mean_height_m', 0)
    forest_pct = landcover_data.get('forest_pct', 0) if not landcover_data.get('error') else 50
    ndvi_mean = ndvi_data.get('mean', 0)

    # IPCC (2006, 2019 Refinement) Tier 2 — Above-Ground Biomass by forest type
    # Table 4.7 (Vol 4, Ch 4): Default AGB densities for European forests
    # Units: tonnes dry matter / hectare
    # Source: IPCC (2019) Refinement to 2006 Guidelines, Vol 4, Table 4.7
    IPCC_AGB_BY_TYPE = {
        # Mediterranean forests (Spain, Portugal, S.France, Italy, Greece)
        'mediterranean_broadleaf': 90,    # Quercus ilex, Q. suber, Olea
        'mediterranean_conifer':   80,    # Pinus halepensis, P. pinaster
        'temperate_broadleaf':    120,    # Fagus, Quercus robur, Castanea
        'temperate_conifer':      130,    # Pinus sylvestris, P. nigra
        'boreal_conifer':         50,     # High altitude Pinus, Abies
        'shrubland':              25,     # Matorral, maquis (Table 4.7 "shrubs")
        'agroforestry':           40,     # Dehesa (sparse tree cover 20-50%)
    }
    # Select AGB based on forest % and NDVI (proxy for Mediterranean vs temperate)
    if ndvi_mean > 0.5 and forest_pct > 40:
        agb_default = IPCC_AGB_BY_TYPE['temperate_broadleaf']
    elif forest_pct > 20:
        agb_default = IPCC_AGB_BY_TYPE['mediterranean_broadleaf']
    elif forest_pct > 5:
        agb_default = IPCC_AGB_BY_TYPE['agroforestry']
    else:
        agb_default = IPCC_AGB_BY_TYPE['shrubland']

    # Adjust with canopy height (taller = more biomass than default)
    # Height scaling: AGB ∝ h^2.0 (Chave et al. 2014, Eq. 7, simplified)
    if mean_height > 0:
        height_ref = 12.0  # reference height for default AGB values
        height_factor = min((mean_height / height_ref) ** 2.0, 3.0)
        agb_per_ha = agb_default * height_factor
    else:
        agb_per_ha = agb_default * 0.5  # no canopy data → conservative

    # Carbon fraction = 0.47 (IPCC default, Table 4.3)
    carbon_per_ha = agb_per_ha * 0.47  # tC/ha
    # CO2 equivalent = C × 3.667
    tco2e_per_ha = carbon_per_ha * 3.667  # tCO₂e/ha

    # Apply to forested area only
    area_ha = area_km2 * 100
    ha_forest = area_ha * (forest_pct / 100)
    total_tco2e = tco2e_per_ha * ha_forest

    # MITECO eligible hectares (forested land >0.5 ha continuous)
    ha_elegibles = ha_forest * 0.85  # ~85% of forest is eligible (exclude fragmented patches)
    creditos_potenciales = round(ha_elegibles * tco2e_per_ha * 0.01)  # conservative: 1% annual increment

    # Score: normalize to 0-100 (benchmark: 400 tCO₂e/ha = perfect)
    score = min(100, round((tco2e_per_ha / 400) * 100, 1))

    return {
        'tco2e_per_ha': round(tco2e_per_ha, 1),
        'agb_t_per_ha': round(agb_per_ha, 1),
        'total_tco2e': round(total_tco2e),
        'ha_elegibles_miteco': round(ha_elegibles, 1),
        'creditos_potenciales': creditos_potenciales,
        'score': score,
        'methodology': 'IPCC Tier 1 + ETH Canopy Height + Sentinel-2 NDVI',
        'source': 'IPCC (2006), Lang et al. (2023)'
    }

# ─── Patch Integrity ──────────────────────────────────────────────────────
def compute_patch_integrity(eii_data, canopy_data, landcover_data):
    """
    Composite patch integrity score.
    40% mature forest + 30% total forest + 30% EII.
    """
    mature_pct = canopy_data.get('mature_forest_pct', 0)
    forest_pct = landcover_data.get('forest_pct', 0)
    eii = eii_data.get('mean', 50) or 50

    # Normalize each to 0-100
    mature_score = min(100, (mature_pct / 50) * 100)  # 50% mature = perfect
    forest_score = min(100, (forest_pct / 80) * 100)  # 80% forest = perfect
    eii_score = eii  # already 0-100

    composite = 0.4 * mature_score + 0.3 * forest_score + 0.3 * eii_score
    return {
        'score': round(composite, 1),
        'mature_forest_score': round(mature_score, 1),
        'total_forest_score': round(forest_score, 1),
        'eii_component': round(eii_score, 1),
        'source': 'WorldCover + ETH Canopy + EII'
    }

# ─── MAIN ─────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='Compute all GEE indicators for a municipality')
    parser.add_argument('--geojson-file', help='Path to GeoJSON file with municipality geometry')
    args = parser.parse_args()

    # Read geometry from stdin or file
    if args.geojson_file:
        with open(args.geojson_file) as f:
            data = json.load(f)
    else:
        data = json.load(sys.stdin)

    geom_dict = data.get('geometry', data)
    area_km2 = data.get('area_km2', 100)
    name = data.get('name', 'Unknown')

    print(f"  Computing GEE indicators for {name} ({area_km2} km²)...", file=sys.stderr)

    init_gee()
    geom = geojson_to_ee(geom_dict)

    results = {}

    # EII
    print("  → EII (Leutner et al. 2024)...", file=sys.stderr)
    results['eii'] = compute_eii(geom)

    # Burned Area
    print("  → Burned Area (MODIS MCD64A1)...", file=sys.stderr)
    try:
        results['fire'] = compute_burned_area(geom)
    except Exception as e:
        results['fire'] = {'error': str(e)}

    # VIIRS
    print("  → Nighttime Lights (VIIRS DNB)...", file=sys.stderr)
    try:
        results['light'] = compute_viirs(geom)
    except Exception as e:
        results['light'] = {'error': str(e)}

    # WDPA
    print("  → Protected Areas (WDPA)...", file=sys.stderr)
    try:
        results['protected'] = compute_protected_areas(geom)
    except Exception as e:
        results['protected'] = {'error': str(e)}

    # WorldCover + Shannon
    print("  → Land Cover + Shannon (WorldCover 2021)...", file=sys.stderr)
    try:
        results['landcover'] = compute_landcover(geom)
    except Exception as e:
        results['landcover'] = {'error': str(e)}

    # Canopy Height
    print("  → Canopy Height (ETH 2020)...", file=sys.stderr)
    try:
        results['canopy'] = compute_canopy(geom)
    except Exception as e:
        results['canopy'] = {'error': str(e)}

    # NDVI
    print("  → NDVI (Sentinel-2 2024)...", file=sys.stderr)
    try:
        results['ndvi'] = compute_ndvi(geom)
    except Exception as e:
        results['ndvi'] = {'error': str(e)}

    # Carbon (derived)
    print("  → Carbon Stock (IPCC Tier 1)...", file=sys.stderr)
    try:
        results['carbon'] = compute_carbon(geom, results.get('canopy', {}), results.get('landcover', {}), results.get('ndvi', {}), area_km2)
    except Exception as e:
        results['carbon'] = {'error': str(e)}

    # Water Quality (turbidity + chlorophyll — for wetlands)
    # Only compute if area has >5% water
    water_pct = results.get('landcover', {}).get('fractions', {}).get('Water', 0)
    wetland_pct = results.get('landcover', {}).get('fractions', {}).get('Wetland', 0)
    if water_pct + wetland_pct > 5:
        print("  → Water Quality: Turbidity + Chlorophyll (sentinel-evalscripts)...", file=sys.stderr)
        try:
            results['water_quality'] = compute_water_quality(geom)
        except Exception as e:
            results['water_quality'] = {'error': str(e)}

    # Patch Integrity (derived)
    print("  → Patch Integrity...", file=sys.stderr)
    try:
        results['patch_integrity'] = compute_patch_integrity(results.get('eii', {}), results.get('canopy', {}), results.get('landcover', {}))
    except Exception as e:
        results['patch_integrity'] = {'error': str(e)}

    print("  ✓ All GEE indicators computed.", file=sys.stderr)
    print(json.dumps(results))

if __name__ == '__main__':
    main()
