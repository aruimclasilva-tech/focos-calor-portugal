#!/usr/bin/env python3
"""
aircraft_fetch.py
------------------
Descarrega posições ADS-B ao vivo sobre Portugal Continental (adsb.fi, com
fallback para airplanes.live) e escreve um JSON simples que a página
focos_calor_portugal.html consegue ler diretamente (docs/portugal_aircraft.json).

PORQUÊ ISTO EXISTE EM VEZ DE A PÁGINA IR DIRETAMENTE BUSCAR OS DADOS
---------------------------------------------------------------------
Tentámos primeiro fazer o browser do utilizador chamar a API do adsb.fi (e a
do airplanes.live) diretamente. Não funciona: nenhuma das duas APIs envia os
cabeçalhos CORS necessários para um browser aceitar a resposta — o pedido
funciona perfeitamente a partir de um servidor (como este script), mas é
sempre bloqueado quando feito a partir de JavaScript no browser (ver
https://github.com/adsbfi/opendata/issues/6). A solução, tal como já
fazemos para os dados EUMETSAT MTG, é buscar aqui (servidor) e publicar um
JSON estático que o browser depois só precisa de ler (isso já não tem
problema de CORS, porque fica no mesmo site).

Cadência real: corre no mesmo workflow do GitHub Actions que já busca os
dados MTG (a cada ~10-15 min). Não é rastreio "segundo a segundo" — é uma
posição aproximada, renovada nesse intervalo.

UTILIZAÇÃO
----------
    python3 aircraft_fetch.py --outdir docs
"""

import os
import sys
import json
import argparse
import datetime as dt

import requests

# Centro aproximado de Portugal Continental e raio (NM) que cobre todo o
# território a partir daí, com margem — dentro do limite de 250 NM destas APIs.
CENTER_LAT = 39.6
CENTER_LON = -8.0
RADIUS_NM = 200

# Campos que a página realmente usa — mantemos o ficheiro pequeno.
# "dbFlags" é um bitmask da base de dados da fonte (convenção readsb/tar1090,
# usada por adsb.fi e airplanes.live): bit 1 = aeronave registada como
# militar. Serve para o filtro "Militares" no painel.
KEEP_FIELDS = [
    "hex", "flight", "r", "t", "desc", "category",
    "lat", "lon", "alt_baro", "gs", "track", "true_heading", "dbFlags",
]

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


def trim(ac):
    return {k: ac.get(k) for k in KEEP_FIELDS if ac.get(k) is not None}


def fetch_adsbfi():
    url = f"https://opendata.adsb.fi/api/v3/lat/{CENTER_LAT}/lon/{CENTER_LON}/dist/{RADIUS_NM}"
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data.get("ac", [])


def fetch_airplaneslive():
    url = f"https://api.airplanes.live/v2/point/{CENTER_LAT}/{CENTER_LON}/{RADIUS_NM}"
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data.get("ac", [])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--outdir", default=".", help="Pasta onde escrever portugal_aircraft.json")
    args = parser.parse_args()

    source = None
    aircraft = []
    error = None

    for name, fn in (("adsb.fi", fetch_adsbfi), ("airplanes.live", fetch_airplaneslive)):
        try:
            aircraft = fn()
            source = name
            break
        except Exception as exc:
            error = f"{name}: {exc}"
            continue

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": source,
        "error": None if source else error,
        "aircraft": [trim(ac) for ac in aircraft if ac.get("lat") is not None and ac.get("lon") is not None],
    }

    os.makedirs(args.outdir, exist_ok=True)
    out_path = os.path.join(args.outdir, "portugal_aircraft.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    if source:
        print(f"OK — {len(out['aircraft'])} aeronaves via {source} -> {out_path}")
    else:
        print(f"FALHOU (adsb.fi e airplanes.live) — {error}", file=sys.stderr)
        # Não é fatal para o workflow: escreve o ficheiro na mesma (lista
        # vazia + erro registado) para a página poder mostrar isso.


if __name__ == "__main__":
    main()
