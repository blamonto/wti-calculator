#!/usr/bin/env python3
"""
Search for Declaraciones de Impacto Ambiental (DIA) for a municipality.

Sources:
  1. BOE (Boletín Oficial del Estado) — API de datos abiertos
  2. MITECO SABIA portal (web scraping)

Usage:
  python search-dia.py "Somiedo"
  python search-dia.py "Somiedo" --province "Asturias"

Output: JSON with list of DIAs found, including:
  - Title, date, BOE reference
  - Species mentioned (if extractable)
  - Link to full document
"""
import sys
import json
import argparse
import re
import urllib.parse
import urllib.request

def search_boe(municipality, province=None):
    """
    Search BOE for Declaraciones de Impacto Ambiental mentioning the municipality.
    BOE open data: https://www.boe.es/datosabiertos/
    """
    results = []

    # BOE search API
    query = f"declaración impacto ambiental {municipality}"
    if province:
        query += f" {province}"

    encoded = urllib.parse.quote(query)
    url = f"https://www.boe.es/datosabiertos/buscar/json/coleccion/BOE/tipo_documento/Resolucion/?buscar={encoded}&p=1&ps=20"

    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'WildSquare-WTI/1.0', 'Accept': 'application/json'})
        with urllib.request.urlopen(req, timeout=15) as response:
            data = json.loads(response.read())
            items = data.get('data', {}).get('items', [])

            for item in items:
                title = item.get('titulo', '')
                # Filter for actual DIA/EIA documents
                if any(kw in title.lower() for kw in ['impacto ambiental', 'evaluación ambiental', 'declaración de impacto']):
                    results.append({
                        'title': title,
                        'date': item.get('fecha_publicacion', ''),
                        'boe_id': item.get('identificador', ''),
                        'url': f"https://www.boe.es/diario_boe/txt.php?id={item.get('identificador', '')}",
                        'section': item.get('seccion', ''),
                        'source': 'BOE',
                    })
    except Exception as e:
        print(f"  BOE search error: {e}", file=sys.stderr)

    # Also try searching for the municipality in environmental context
    try:
        query2 = f"ambiental {municipality}"
        encoded2 = urllib.parse.quote(query2)
        url2 = f"https://www.boe.es/datosabiertos/buscar/json/coleccion/BOE/?buscar={encoded2}&p=1&ps=10"
        req2 = urllib.request.Request(url2, headers={'User-Agent': 'WildSquare-WTI/1.0', 'Accept': 'application/json'})
        with urllib.request.urlopen(req2, timeout=15) as response2:
            data2 = json.loads(response2.read())
            for item in data2.get('data', {}).get('items', []):
                title = item.get('titulo', '')
                if any(kw in title.lower() for kw in ['impacto ambiental', 'evaluación ambiental', 'declaración', 'natura 2000', 'parque natural']):
                    boe_id = item.get('identificador', '')
                    if not any(r['boe_id'] == boe_id for r in results):
                        results.append({
                            'title': title,
                            'date': item.get('fecha_publicacion', ''),
                            'boe_id': boe_id,
                            'url': f"https://www.boe.es/diario_boe/txt.php?id={boe_id}",
                            'section': item.get('seccion', ''),
                            'source': 'BOE',
                        })
    except Exception as e:
        pass

    return results


def search_miteco_sabia(municipality):
    """
    Search MITECO SABIA portal for environmental assessments.
    Note: SABIA doesn't have a public API — this provides the search URL.
    """
    search_url = f"https://sede.miteco.gob.es/portal/site/seMITECO/navServicioContenido?idContenido=22536&vgnextoid=a3a09ec88c015710VgnVCM100000d84e900aRCRD"
    return {
        'note': f'Buscar manualmente en el portal SABIA de MITECO para "{municipality}"',
        'url': search_url,
        'instructions': [
            '1. Acceder al portal SABIA de MITECO',
            f'2. Buscar por municipio: "{municipality}"',
            '3. Filtrar por tipo: "Declaración de Impacto Ambiental"',
            '4. Descargar las DIAs relevantes',
            '5. Extraer inventarios de fauna/flora de los documentos',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description='Search for DIAs for a municipality')
    parser.add_argument('municipality', help='Municipality name')
    parser.add_argument('--province', help='Province name (optional)')
    args = parser.parse_args()

    print(f"  Buscando DIAs para {args.municipality}...", file=sys.stderr)

    boe_results = search_boe(args.municipality, args.province)
    miteco_info = search_miteco_sabia(args.municipality)

    result = {
        'municipality': args.municipality,
        'boe_dias': boe_results,
        'boe_count': len(boe_results),
        'miteco_sabia': miteco_info,
        'note': 'Las DIAs contienen inventarios detallados de fauna y flora realizados por consultoras ambientales. Son complementarias a los datos GBIF.',
        'usage': 'Añadir especies identificadas en las DIAs al inventario GBIF para un conteo más completo.',
    }

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
