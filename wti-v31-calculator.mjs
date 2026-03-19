#!/usr/bin/env node
/**
 * Wild Territory Index (WTI) v3.1 — Calculadora Municipal
 *
 * Metodología científica de precisión con datos GEE reales.
 * Usa polígono municipal real (OSMnx) + GEE zonal stats.
 *
 * Uso:
 *   node scripts/wti-v31-calculator.mjs "Somiedo"
 *   node scripts/wti-v31-calculator.mjs "Somiedo" --full    # incluye Conectividad (Delta-IIC)
 *   node scripts/wti-v31-calculator.mjs "Somiedo" --investment 150000  # EUR invertidos en 5 años
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { execSync } from 'child_process';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const GBIF_BASE = 'https://api.gbif.org/v1';
const YEAR = new Date().getFullYear();
const GEE_ENV = 'source /Users/blanca/gee-env/bin/activate';

// ─── UTILS ────────────────────────────────────────────────────────────────
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
function log(s) { console.log(`\n${'═'.repeat(70)}\n  ${s}\n${'═'.repeat(70)}`); }
function item(l, v) { console.log(`  ${l.padEnd(45)} ${v}`); }
function normalize(val, min, max, invert = false) {
  const c = Math.max(min, Math.min(max, val));
  const n = ((c - min) / (max - min)) * 100;
  return invert ? 100 - n : n;
}

function wktFromBbox(bbox) {
  const { minLng, minLat, maxLng, maxLat } = bbox;
  return `POLYGON((${minLng} ${minLat}, ${maxLng} ${minLat}, ${maxLng} ${maxLat}, ${minLng} ${maxLat}, ${minLng} ${minLat}))`;
}

// ─── STEP 1: GET MUNICIPALITY GEOMETRY ────────────────────────────────────
function getMunicipalityGeometry(name) {
  const script = path.join(__dirname, 'get-municipality-geometry.py');
  const output = execSync(`${GEE_ENV} && python "${script}" "${name}"`, {
    timeout: 30000, shell: '/bin/zsh', encoding: 'utf-8'
  });
  return JSON.parse(output.trim());
}

// ─── STEP 2: GEE INDICATORS (all satellite data) ─────────────────────────
function getGEEIndicators(geojsonPath) {
  const script = path.join(__dirname, 'gee-indicators.py');
  const output = execSync(`${GEE_ENV} && python "${script}" --geojson-file "${geojsonPath}"`, {
    timeout: 600000, shell: '/bin/zsh', encoding: 'utf-8'
  });
  return JSON.parse(output.trim());
}

// ─── STEP 3: GBIF INDICATORS ─────────────────────────────────────────────

async function getGBIFData(bbox, areaKm2) {
  const geometry = wktFromBbox(bbox);

  // 3a. Species richness (all kingdoms)
  const classes = [
    { key: 212, name: 'Aves' }, { key: 359, name: 'Mamíferos' },
    { key: 358, name: 'Reptiles' }, { key: 131, name: 'Anfibios' },
    { key: 216, name: 'Insectos' }, { key: 6, name: 'Plantas' }, { key: 5, name: 'Hongos' },
  ];
  let totalSpecies = 0;
  const classResults = [];
  const allSpeciesKeys = new Set();

  for (const cls of classes) {
    try {
      const p = new URLSearchParams({ geometry, classKey: String(cls.key), country: 'ES', hasCoordinate: 'true', hasGeospatialIssue: 'false', limit: '300' });
      const r = await fetch(`${GBIF_BASE}/occurrence/search?${p}`, { signal: AbortSignal.timeout(15000) });
      if (!r.ok) continue;
      const d = await r.json();
      const sp = new Set();
      for (const o of (d.results || [])) { if (o.speciesKey) { sp.add(o.speciesKey); allSpeciesKeys.add(o.speciesKey); } }
      totalSpecies += sp.size;
      classResults.push({ class: cls.name, species: sp.size, occurrences: d.count || 0 });
      await sleep(200);
    } catch { classResults.push({ class: cls.name, species: 0 }); }
  }

  // Density with benchmark 10 spp/km² = 100 (Kier et al. 2005)
  const density = totalSpecies / areaKm2;
  const richnessScore = Math.min(100, (density / 10) * 100);

  // 3b. Observation quality (composite score per Chapman 2005)
  let qualityScore = 50;
  try {
    const totalP = new URLSearchParams({ geometry, country: 'ES', hasCoordinate: 'true', limit: '0' });
    const photoP = new URLSearchParams({ geometry, country: 'ES', hasCoordinate: 'true', mediaType: 'StillImage', limit: '0' });
    const geoP = new URLSearchParams({ geometry, country: 'ES', hasCoordinate: 'true', hasGeospatialIssue: 'false', limit: '0' });
    const [totalR, photoR, geoR] = await Promise.all([
      fetch(`${GBIF_BASE}/occurrence/search?${totalP}`, { signal: AbortSignal.timeout(15000) }),
      fetch(`${GBIF_BASE}/occurrence/search?${photoP}`, { signal: AbortSignal.timeout(15000) }),
      fetch(`${GBIF_BASE}/occurrence/search?${geoP}`, { signal: AbortSignal.timeout(15000) }),
    ]);
    const total = (await totalR.json()).count || 1;
    const withPhoto = (await photoR.json()).count || 0;
    const withGeo = (await geoR.json()).count || 0;
    const photoPct = (withPhoto / total) * 100;
    const geoPct = (withGeo / total) * 100;
    // Composite: 40% photo + 40% geo accuracy + 20% base (GBIF is peer-reviewed platform)
    qualityScore = Math.round(photoPct * 0.4 + geoPct * 0.4 + 20);
    var qualityDetail = { total, withPhoto, photoPct: Math.round(photoPct * 10) / 10, geoPct: Math.round(geoPct * 10) / 10, compositeScore: qualityScore };
  } catch { var qualityDetail = { error: 'GBIF quality fetch failed' }; }

  // 3c. Native species change (species-level, not records — Isaac et al. 2014)
  let changeRate = 0, changeDetail = {};
  try {
    const [oldR, newR] = await Promise.all([
      fetch(`${GBIF_BASE}/occurrence/search?${new URLSearchParams({ geometry, country: 'ES', hasCoordinate: 'true', year: '2014,2016', limit: '300' })}`, { signal: AbortSignal.timeout(15000) }),
      fetch(`${GBIF_BASE}/occurrence/search?${new URLSearchParams({ geometry, country: 'ES', hasCoordinate: 'true', year: '2022,2024', limit: '300' })}`, { signal: AbortSignal.timeout(15000) }),
    ]);
    const oldSp = new Set(), newSp = new Set();
    for (const o of ((await oldR.json()).results || [])) if (o.speciesKey) oldSp.add(o.speciesKey);
    for (const o of ((await newR.json()).results || [])) if (o.speciesKey) newSp.add(o.speciesKey);
    changeRate = oldSp.size > 0 ? ((newSp.size - oldSp.size) / oldSp.size) * 100 : 0;
    // Annualized over ~8 years
    const annualChange = changeRate / 8;
    // Score: normalize annual change -5% to +10% → 0-100
    const changeScore = normalize(annualChange, -5, 10);
    changeDetail = { oldSpecies: oldSp.size, newSpecies: newSp.size, totalChange: Math.round(changeRate * 10) / 10, annualChange: Math.round(annualChange * 100) / 100, score: Math.round(changeScore * 10) / 10 };
  } catch { changeDetail = { error: 'GBIF change fetch failed' }; }

  // 3d. Invasive species proportion
  let invasiveScore = 100, invasiveDetail = {};
  try {
    let invasiveCount = 0;
    const easinR = await fetch('https://easin.jrc.ec.europa.eu/apixg/catxg/incountries/ES/skip/0/take/100', { signal: AbortSignal.timeout(15000) });
    if (easinR.ok) {
      const easinData = await easinR.json();
      const names = easinData.filter(s => s.HasImpact || s.IsEUConcern).map(s => s.Name).slice(0, 30);
      for (const name of names) {
        try {
          const r = await fetch(`${GBIF_BASE}/occurrence/search?${new URLSearchParams({ scientificName: name, geometry, country: 'ES', hasCoordinate: 'true', limit: '1' })}`, { signal: AbortSignal.timeout(8000) });
          if (r.ok && (await r.json()).count > 0) invasiveCount++;
        } catch {}
        if (names.indexOf(name) % 10 === 9) await sleep(500);
      }
    }
    const pct = totalSpecies > 0 ? (invasiveCount / totalSpecies) * 100 : 0;
    // Score = 100 - (20 × pct_invasoras)
    invasiveScore = Math.max(0, Math.round(100 - 20 * pct));
    invasiveDetail = { invasiveCount, totalSpecies, pct: Math.round(pct * 100) / 100, score: invasiveScore };
  } catch { invasiveDetail = { error: 'Invasive check failed' }; }

  // 3e. Source breakdown (iNaturalist, eBird, etc.)
  let sourceBreakdown = {};
  try {
    // Check iNaturalist contribution
    const inatP = new URLSearchParams({ geometry, country: 'ES', hasCoordinate: 'true', datasetKey: '50c9509d-22c7-4a22-a47d-8c48425ef4a7', limit: '0' });
    const inatR = await fetch(`${GBIF_BASE}/occurrence/search?${inatP}`, { signal: AbortSignal.timeout(10000) });
    const inatCount = inatR.ok ? (await inatR.json()).count : 0;
    // eBird
    const ebirdP = new URLSearchParams({ geometry, country: 'ES', hasCoordinate: 'true', datasetKey: '4fa7b334-ce0d-4e88-aaae-2e0c138d049e', limit: '0' });
    const ebirdR = await fetch(`${GBIF_BASE}/occurrence/search?${ebirdP}`, { signal: AbortSignal.timeout(10000) });
    const ebirdCount = ebirdR.ok ? (await ebirdR.json()).count : 0;
    sourceBreakdown = { iNaturalist: inatCount, eBird: ebirdCount, other: Math.max(0, (qualityDetail.total || 0) - inatCount - ebirdCount) };
  } catch {}

  return {
    richness: { totalSpecies, density: Math.round(density * 100) / 100, score: Math.round(richnessScore * 10) / 10, classes: classResults, benchmark: '10 spp/km² = 100 (Kier et al. 2005)' },
    quality: { ...qualityDetail, source: 'Chapman (2005) composite: 40% photo + 40% geo + 20% base' },
    nativeChange: { ...changeDetail, note: 'Basado en especies únicas (speciesKey), no registros. Isaac et al. (2014)', source: 'GBIF Occurrence API' },
    invasives: { ...invasiveDetail, source: 'GBIF + EASIN (JRC) + Catálogo EEI (MITECO)' },
    sourceBreakdown,
    diaNote: 'Para inventarios más completos, consultar Declaraciones de Impacto Ambiental (DIA) del municipio en BOE/BOPA/MITECO.',
  };
}

// ─── TYPOLOGY ─────────────────────────────────────────────────────────────
function detectTypology(lat, lng, areaKm2, comunidad) {
  const isUrban = areaKm2 < 50;
  const isMountain = lat > 42 || ['Asturias', 'Cantabria', 'Navarra', 'Aragón'].some(c => comunidad?.includes(c));
  if (isUrban) return 'Urbano/Periurbano';
  if (isMountain) return 'Forestal/Montaña';
  return 'Agrícola';
}

const TYPOLOGY_CONFIG = {
  'Forestal/Montaña': {
    pilar1: ['eii', 'richness', 'quality', 'nativeChange', 'invasives', 'patchIntegrity', 'shannon'],
    pilar2: ['carbon', 'biodiversityCredits', 'connectivity'],
    pilar3: ['fireRisk', 'lightPollution', 'protectedAreas', 'investment'],
  },
  'Agrícola': {
    pilar1: ['eii', 'richness', 'quality', 'nativeChange', 'invasives', 'leci', 'shannon'],
    pilar2: ['carbon', 'biodiversityCredits', 'connectivity'],
    pilar3: ['droughtRisk', 'lightPollution', 'protectedAreas', 'investment'],
  },
  'Humedales/Fluvial': {
    pilar1: ['eii', 'richness', 'quality', 'nativeChange', 'invasives', 'leci', 'ndti', 'chlorophyll', 'shannon'],
    pilar2: ['biodiversityCredits', 'connectivity'],
    pilar3: ['droughtRisk', 'lightPollution', 'protectedAreas', 'investment'],
  },
  'Urbano/Periurbano': {
    pilar1: ['eii', 'richness', 'quality', 'nativeChange', 'invasives', 'shannon'],
    pilar2: ['biodiversityCredits', 'connectivity', 'evu', 'cau'],
    pilar3: ['lightPollution', 'lst', 'protectedAreas', 'investment'],
  },
};

// ─── SCORING v3.1 ─────────────────────────────────────────────────────────
function scorePillar1(gee, gbif, typology, fullAnalysis) {
  const applicable = TYPOLOGY_CONFIG[typology]?.pilar1 || [];
  const c = [];

  if (applicable.includes('eii') && gee.eii?.mean != null)
    c.push({ n: 'EII (Leutner 2024)', s: gee.eii.mean, w: 0.25 });
  if (applicable.includes('richness') && gbif.richness?.score != null)
    c.push({ n: 'Riqueza Especies (GBIF)', s: gbif.richness.score, w: 0.20 });
  if (applicable.includes('quality') && gbif.quality?.compositeScore != null)
    c.push({ n: 'Calidad Observaciones', s: gbif.quality.compositeScore, w: 0.10 });
  if (applicable.includes('nativeChange') && gbif.nativeChange?.score != null)
    c.push({ n: 'Cambio Sp. Nativas', s: gbif.nativeChange.score, w: 0.20 });
  if (applicable.includes('invasives') && gbif.invasives?.score != null)
    c.push({ n: 'Prop. Invasoras', s: gbif.invasives.score, w: 0.10 });
  if (applicable.includes('patchIntegrity') && gee.patch_integrity?.score != null)
    c.push({ n: 'Integridad Parches', s: gee.patch_integrity.score, w: 0.10 });
  if (applicable.includes('shannon') && gee.landcover?.shannon_normalized != null)
    c.push({ n: 'Shannon (WorldCover)', s: gee.landcover.shannon_normalized, w: 0.05 });
  // Water quality indicators (Humedales only)
  if (applicable.includes('ndti') && gee.water_quality?.turbidity_score != null)
    c.push({ n: 'Turbidez Agua (NDTI)', s: gee.water_quality.turbidity_score, w: 0.10 });
  if (applicable.includes('chlorophyll') && gee.water_quality?.chla_score != null)
    c.push({ n: 'Clorofila-a (OC3)', s: gee.water_quality.chla_score, w: 0.08 });

  const tw = c.reduce((s, x) => s + x.w, 0);
  return { score: tw > 0 ? Math.round(c.reduce((s, x) => s + x.s * x.w / tw, 0) * 100) / 100 : 0, components: c };
}

function scorePillar2(gee, gbif, typology, fullAnalysis) {
  const applicable = TYPOLOGY_CONFIG[typology]?.pilar2 || [];
  const c = [];

  if (applicable.includes('carbon') && gee.carbon?.score != null)
    c.push({ n: 'Potencial Carbono (IPCC)', s: gee.carbon.score, w: 0.30 });
  if (applicable.includes('biodiversityCredits') && gee.eii?.mean != null)
    c.push({ n: 'Créditos Biodiversidad', s: gee.eii.mean * 0.8, w: 0.20 });
  if (applicable.includes('connectivity')) {
    const val = fullAnalysis?.connectivity?.delta_iic_norm;
    if (val != null) c.push({ n: 'Conectividad (Delta-IIC)', s: val * 100, w: 0.25 });
    else c.push({ n: 'Conectividad', s: null, w: 0.25, noData: true });
  }

  const tw = c.filter(x => !x.noData).reduce((s, x) => s + x.w, 0);
  return { score: tw > 0 ? Math.round(c.filter(x => !x.noData).reduce((s, x) => s + x.s * x.w / tw, 0) * 100) / 100 : 0, components: c };
}

function scorePillar3(gee, typology, investmentEUR, areaHa) {
  const applicable = TYPOLOGY_CONFIG[typology]?.pilar3 || [];
  const c = [];

  if (applicable.includes('fireRisk') && gee.fire?.score != null)
    c.push({ n: 'Riesgo Incendio (MODIS)', s: gee.fire.score, w: 0.25 });
  if (applicable.includes('lightPollution') && gee.light?.score != null)
    c.push({ n: 'Presión Antrópica (VIIRS)', s: gee.light.score, w: 0.20 });
  if (applicable.includes('protectedAreas') && gee.protected?.score != null)
    c.push({ n: 'Áreas Protegidas (WDPA)', s: gee.protected.score, w: 0.25 });
  if (applicable.includes('investment')) {
    if (investmentEUR != null && areaHa > 0) {
      const eurHaYear = investmentEUR / areaHa / 5;
      const investScore = Math.min(100, (eurHaYear / 50) * 100);
      c.push({ n: 'Inversión Conservación', s: Math.round(investScore), w: 0.10, detail: `${Math.round(eurHaYear * 10) / 10} EUR/ha/año` });
    } else {
      c.push({ n: 'Inversión Conservación', s: 0, w: 0.10, noData: true, detail: '[pendiente municipio]' });
    }
  }

  const tw = c.filter(x => !x.noData).reduce((s, x) => s + x.w, 0);
  return { score: tw > 0 ? Math.round(c.filter(x => !x.noData).reduce((s, x) => s + x.s * x.w / tw, 0) * 100) / 100 : 0, components: c };
}

// ─── MAIN ─────────────────────────────────────────────────────────────────
async function main() {
  const args = process.argv.slice(2);
  if (!args.length) {
    console.log('Uso: node wti-v31-calculator.mjs "Municipio" [--full] [--investment EUR]');
    process.exit(1);
  }

  const name = args[0];
  const runFull = args.includes('--full');
  const investIdx = args.indexOf('--investment');
  const investmentEUR = investIdx >= 0 ? parseFloat(args[investIdx + 1]) : null;
  const geojsonIdx = args.indexOf('--geojson');
  const geojsonFile = geojsonIdx >= 0 ? args[geojsonIdx + 1] : null;
  const typoIdx = args.indexOf('--typology');
  const manualTypology = typoIdx >= 0 ? args[typoIdx + 1] : null;

  // ═══ STEP 1: Territory geometry ═══
  let muniData;
  if (geojsonFile) {
    // Use pre-built GeoJSON file (for custom fincas)
    console.log(`\n  → Usando geometría de "${geojsonFile}"...`);
    muniData = JSON.parse(fs.readFileSync(geojsonFile, 'utf-8'));
  } else {
    console.log(`\n  → Obteniendo polígono de "${name}" (OSMnx)...`);
    try {
      muniData = getMunicipalityGeometry(name);
    } catch (e) {
      console.error(`  Error: No se encontró el municipio "${name}"`);
      process.exit(1);
    }
  }
  const { lat, lng, area_km2: areaKm2 } = muniData;
  const areaHa = areaKm2 * 100;
  const typology = manualTypology || detectTypology(lat, lng, areaKm2, '');

  // Save geometry for GEE
  const geomPath = path.join(__dirname, `.wti-temp-${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}.json`);
  fs.writeFileSync(geomPath, JSON.stringify(muniData));

  console.log(`
╔══════════════════════════════════════════════════════════════════════════╗
║  WILD TERRITORY INDEX (WTI) v3.1                                         ║
║  Municipio: ${name.padEnd(50)}         ║
║  Centro: ${lat}°N, ${lng}°W                                          ║
║  Área: ${areaKm2} km² (${Math.round(areaHa)} ha)                                   ║
║  Tipología: ${typology.padEnd(40)}               ║
║  Fecha: ${new Date().toISOString().slice(0, 10)}                                                   ║
╚══════════════════════════════════════════════════════════════════════════╝`);

  // ═══ STEP 2: GEE indicators ═══
  log('INDICADORES SATELITALES (Google Earth Engine)');
  console.log('  → Ejecutando gee-indicators.py con polígono real...');
  let gee;
  try {
    gee = getGEEIndicators(geomPath);
  } catch (e) {
    console.error(`  Error GEE: ${String(e).slice(0, 200)}`);
    gee = {};
  }

  // Display GEE results
  if (gee.eii?.mean != null) item('EII:', `${gee.eii.mean}/100 (${gee.eii.quality})`);
  if (gee.fire?.burned_pct != null) item('Fire (MODIS):', `${gee.fire.burned_pct}% quemado → score ${gee.fire.score}/100`);
  if (gee.light?.radiance_nw != null) item('VIIRS:', `${gee.light.radiance_nw} nW/cm²/sr → score ${gee.light.score}/100`);
  if (gee.protected?.pct != null) item('Protegido (WDPA):', `${gee.protected.pct}%`);
  if (gee.landcover && !gee.landcover.error) {
    item('Bosque:', `${gee.landcover.forest_pct}%`);
    item('Natural:', `${gee.landcover.natural_pct}%`);
    item('Shannon H\':', `${gee.landcover.shannon_h} (norm: ${gee.landcover.shannon_normalized}/100)`);
  }
  if (gee.canopy?.mean_height_m) item('Dosel medio:', `${gee.canopy.mean_height_m}m (maduro: ${gee.canopy.mature_forest_pct}%)`);
  if (gee.ndvi?.mean) item('NDVI:', `${gee.ndvi.mean}`);
  if (gee.carbon?.tco2e_per_ha) {
    item('Carbono:', `${gee.carbon.tco2e_per_ha} tCO₂e/ha`);
    item('Ha elegibles MITECO:', `${gee.carbon.ha_elegibles_miteco}`);
    item('Créditos potenciales:', `${gee.carbon.creditos_potenciales}`);
  }
  if (gee.patch_integrity?.score) item('Integridad Parches:', `${gee.patch_integrity.score}/100`);

  // Protected areas list
  if (gee.protected?.areas?.length) {
    for (const a of gee.protected.areas.slice(0, 5)) item(`  ${a.name}:`, a.designation);
  }

  // ═══ STEP 3: GBIF + EASIN indicators ═══
  log('INDICADORES BIODIVERSIDAD (GBIF + EASIN)');
  const bbox = {
    minLat: lat - Math.sqrt(areaKm2) / 111 / 2,
    maxLat: lat + Math.sqrt(areaKm2) / 111 / 2,
    minLng: lng - Math.sqrt(areaKm2) / (111 * Math.cos(lat * Math.PI / 180)) / 2,
    maxLng: lng + Math.sqrt(areaKm2) / (111 * Math.cos(lat * Math.PI / 180)) / 2,
  };
  const gbif = await getGBIFData(bbox, areaKm2);

  item('Especies:', `${gbif.richness.totalSpecies} (${gbif.richness.density}/km²) → score ${gbif.richness.score}/100`);
  for (const c of gbif.richness.classes) if (c.species > 0) item(`  ${c.class}:`, `${c.species} sp`);
  item('Calidad:', `${gbif.quality.compositeScore}/100 (foto: ${gbif.quality.photoPct}%, geo: ${gbif.quality.geoPct}%)`);
  item('Cambio nativas:', `${gbif.nativeChange.annualChange}%/año → score ${gbif.nativeChange.score}/100`);
  item('Invasoras:', `${gbif.invasives.pct}% → score ${gbif.invasives.score}/100`);
  if (gbif.sourceBreakdown) {
    item('Fuentes GBIF:', `iNaturalist: ${gbif.sourceBreakdown.iNaturalist}, eBird: ${gbif.sourceBreakdown.eBird}, otros: ${gbif.sourceBreakdown.other}`);
  }
  console.log(`  Nota: ${gbif.diaNote}`);

  // ═══ STEP 4: Full_Analysis (optional) ═══
  let fullAnalysis = null;
  if (runFull) {
    log('FULL_ANALYSIS (Conectividad Delta-IIC)');
    try {
      const faScript = path.join(__dirname, 'run-full-analysis.py');
      const faOut = path.join(__dirname, `wti-full-${name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`);
      const output = execSync(`${GEE_ENV} && python "${faScript}" --lat ${lat} --lng ${lng} --buffer 8000 --name "${name}" --output-dir "${faOut}" --json`, {
        timeout: 1800000, shell: '/bin/zsh', encoding: 'utf-8'
      });
      fullAnalysis = JSON.parse(output.trim());
      if (fullAnalysis?.connectivity) item('Delta-IIC:', `${(fullAnalysis.connectivity.delta_iic_norm * 100).toFixed(1)}/100`);
    } catch (e) {
      console.log(`  ⚠ Full_Analysis: ${String(e.message || e).slice(0, 100)}`);
    }
  }

  // ═══ STEP 5: SCORING ═══
  log(`WTI v3.1 — SCORING — ${typology}`);

  const p1 = scorePillar1(gee, gbif, typology, fullAnalysis);
  const p2 = scorePillar2(gee, gbif, typology, fullAnalysis);
  const p3 = scorePillar3(gee, typology, investmentEUR, areaHa);

  console.log('\n  PILAR 1 — Biodiversidad y Ecosistemas (40%):');
  item('    Score:', `${p1.score}/100`);
  for (const c of p1.components) item(`    ${c.n}:`, `${Math.round(c.s)}/100 (${(c.w * 100).toFixed(0)}%)`);

  console.log('\n  PILAR 2 — Servicios y Oportunidades (35%):');
  item('    Score:', `${p2.score}/100`);
  for (const c of p2.components) item(`    ${c.n}:`, c.noData ? '[sin datos — usa --full]' : `${Math.round(c.s)}/100 (${(c.w * 100).toFixed(0)}%)`);

  console.log('\n  PILAR 3 — Gobernanza y Riesgo (25%):');
  item('    Score:', `${p3.score}/100`);
  for (const c of p3.components) item(`    ${c.n}:`, c.noData ? `[${c.detail}]` : `${Math.round(c.s)}/100 (${(c.w * 100).toFixed(0)}%)${c.detail ? ' (' + c.detail + ')' : ''}`);

  const wti = Math.round(((p1.score * 0.40) + (p2.score * 0.35) + (p3.score * 0.25)) * 100) / 100;
  const label = wti >= 80 ? 'Excelente' : wti >= 65 ? 'Bueno' : wti >= 45 ? 'Moderado' : wti >= 25 ? 'Bajo' : 'Crítico';

  console.log(`
╔══════════════════════════════════════════════════════════════════════════╗
║  WTI v3.1 — ${name.padEnd(50)}           ║
║                                                                          ║
║  Pilar 1 (Biodiversidad  40%):  ${String(p1.score).padEnd(8)}/100                       ║
║  Pilar 2 (Servicios      35%):  ${String(p2.score).padEnd(8)}/100                       ║
║  Pilar 3 (Gobernanza     25%):  ${String(p3.score).padEnd(8)}/100                       ║
║                                                                          ║
║  ════════════════════════════════════════════                             ║
║  WTI SCORE:  ${String(wti).padEnd(8)}/100  (${label})                              ║
║  ════════════════════════════════════════════                             ║
╚══════════════════════════════════════════════════════════════════════════╝`);

  // Save result
  const result = {
    version: '3.1',
    municipality: name, typology, lat, lng, areaKm2, areaHa,
    date: new Date().toISOString(),
    wtiScore: wti, wtiLabel: label,
    pillars: { biodiversidad: p1, servicios: p2, gobernanza: p3 },
    gee, gbif,
    fullAnalysis: fullAnalysis || null,
    investmentEUR,
    methodology: {
      eii: 'Leutner et al. (2024) via GEE',
      fire: 'MODIS MCD64A1 (Giglio et al. 2018)',
      light: 'VIIRS DNB V2.2 (Elvidge et al. 2017)',
      protected: 'WDPA (UNEP-WCMC 2024)',
      landcover: 'ESA WorldCover v200 (Zanaga et al. 2022)',
      canopy: 'ETH Canopy Height (Lang et al. 2023)',
      ndvi: 'Sentinel-2 L2A SR Harmonized',
      carbon: 'IPCC Tier 1 (2006) + ETH + Sentinel-2',
      species: 'GBIF Occurrence API (incl. iNaturalist, eBird)',
      quality: 'Chapman (2005) composite index',
      change: 'Isaac et al. (2014) species-level',
      invasives: 'GBIF + EASIN (JRC) + MITECO Catálogo EEI',
      connectivity: 'Pascual-Hortal & Saura (2006) via Full_Analysis',
    },
  };

  const safeName = name.toLowerCase().replace(/[^a-z0-9]+/g, '-');
  const outPath = path.join(__dirname, `wti-v31-${safeName}.json`);
  fs.writeFileSync(outPath, JSON.stringify(result, null, 2));
  console.log(`\n  Resultado: ${outPath}`);

  // Cleanup temp file
  try { fs.unlinkSync(geomPath); } catch {}
}

main().catch(console.error);
