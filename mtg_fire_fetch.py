#!/usr/bin/env python3
"""
mtg_fire_fetch.py
------------------
Descarrega o produto MTG Fire Radiative Power Pixel (LSA-509, EUMETSAT/LSA SAF),
filtra deteções dentro de Portugal Continental, e escreve um JSON simples que a
página focos_calor_portugal.html consegue ler diretamente.

Cadência real da fonte: ficheiros novos a cada 10 minutos, disponíveis com um
atraso tipico de ~15-25 minutos após o timestamp nominal.

REQUISITOS
----------
1. Conta gratuita LSA SAF: https://lsa-saf.eumetsat.int  (Register)
   -> as mesmas credenciais servem para autenticar no servidor de dados.
2. pip install requests

CONFIGURAÇÃO
------------
Definir as credenciais como variáveis de ambiente (recomendado, evita
guardar a password no ficheiro):

    export LSASAF_USER="o_teu_utilizador"
    export LSASAF_PASS="a_tua_password"

ou editar diretamente as constantes USERNAME / PASSWORD abaixo.

UTILIZAÇÃO
----------
Uma execução única (descarrega o último ficheiro disponível):
    python3 mtg_fire_fetch.py

Modo contínuo, alinhado ao ciclo de 10 min da fonte (recomendado, corre em
1º plano ou como serviço/tarefa agendada):
    python3 mtg_fire_fetch.py --watch

O JSON de saída (por omissão ./portugal_fires_mtg.json) deve ficar na mesma
pasta que o focos_calor_portugal.html, servidos por um pequeno servidor local
(necessário porque abrir o HTML por duplo-clique bloqueia o fetch() a
ficheiros locais):

    python3 -m http.server 8000
    # depois abrir http://localhost:8000/focos_calor_portugal.html
"""

import os
import sys
import gzip
import csv
import io
import json
import time
import argparse
import datetime as dt
from urllib.parse import urljoin

import requests

# --------------------------------------------------------------------------
# CONFIGURAÇÃO
# --------------------------------------------------------------------------

USERNAME = os.environ.get("LSASAF_USER", "")
PASSWORD = os.environ.get("LSASAF_PASS", "")

BASE_URL = "https://datalsasaf.lsasvcs.ipma.pt/PRODUCTS/MTG/MTFRPPixel/NATIVE/"

# Bounding box de Portugal Continental: (lat_min, lat_max, lon_min, lon_max)
BBOX = (36.85, 42.2, -9.65, -6.05)

OUTPUT_PATH = "portugal_fires_mtg.json"
POLL_INTERVAL_SEC = 600          # 10 minutos, igual à cadência da fonte
LATENCY_BUFFER_SEC = 5 * 60      # margem extra antes de tentar o próximo ciclo
REQUEST_TIMEOUT = 60

# Nomes de coluna possíveis no CSV (o produto pode variar a nomenclatura
# exata; o script tenta encontrar por substring, case-insensitive).
COLUMN_HINTS = {
    "lat": ["lat"],
    "lon": ["lon"],
    "frp": ["frp"],
    "confidence": ["conf"],
    "time": ["time", "date", "obs"],
}


def log(msg):
    print(f"[{dt.datetime.utcnow().strftime('%H:%M:%S')} UTC] {msg}", flush=True)


# --------------------------------------------------------------------------
# DESCOBERTA DO FICHEIRO MAIS RECENTE (listagem de diretório é pública)
# --------------------------------------------------------------------------

def list_dir(url):
    r = requests.get(url, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.text


def find_latest_csv_gz(now_utc=None):
    """Percorre a listagem do dia (e do dia anterior perto da meia-noite)
    e devolve o URL completo do LSA-509 ...-ListProduct...csv.gz mais recente."""
    now_utc = now_utc or dt.datetime.utcnow()
    candidates = []

    for day_offset in (0, -1):  # hoje e ontem, para cobrir a virada do dia
        day = now_utc + dt.timedelta(days=day_offset)
        day_url = urljoin(BASE_URL, f"{day.year:04d}/{day.month:02d}/{day.day:02d}/")
        try:
            html = list_dir(day_url)
        except requests.HTTPError:
            continue
        for line in html.splitlines():
            if "ListProduct" in line and ".csv.gz" in line:
                # extrai o nome do ficheiro do href
                start = line.find("LSA-509")
                if start == -1:
                    continue
                end = line.find(".csv.gz", start) + len(".csv.gz")
                fname = line[start:end]
                candidates.append(day_url + fname)

    if not candidates:
        return None
    # os nomes contêm AAAAMMDDHHMM, ordenação de strings já dá ordem cronológica
    candidates.sort()
    return candidates[-1]


# --------------------------------------------------------------------------
# DOWNLOAD + PARSING
# --------------------------------------------------------------------------

def download_csv(url):
    if not USERNAME or not PASSWORD:
        raise RuntimeError(
            "Faltam credenciais LSA SAF. Define LSASAF_USER e LSASAF_PASS "
            "(variáveis de ambiente) com a conta gratuita registada em "
            "https://lsa-saf.eumetsat.int"
        )
    r = requests.get(url, auth=(USERNAME, PASSWORD), timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    raw = gzip.decompress(r.content)
    return raw.decode("utf-8", errors="replace")


def guess_columns(fieldnames):
    mapping = {}
    lower = {f: f.lower() for f in fieldnames}
    for key, hints in COLUMN_HINTS.items():
        for f, fl in lower.items():
            if any(h in fl for h in hints):
                mapping[key] = f
                break
    return mapping


def parse_rows(csv_text, timestamp_from_filename):
    reader = csv.DictReader(io.StringIO(csv_text))
    if not reader.fieldnames:
        log("AVISO: CSV sem cabeçalho reconhecível.")
        return []

    mapping = guess_columns(reader.fieldnames)
    log(f"Colunas detetadas no CSV: {reader.fieldnames}")
    log(f"Mapeamento usado: {mapping}")

    missing = [k for k in ("lat", "lon", "frp") if k not in mapping]
    if missing:
        log(f"ERRO: não encontrei colunas para {missing}. "
            f"Edita COLUMN_HINTS no script com os nomes reais acima.")
        return []

    lat_min, lat_max, lon_min, lon_max = BBOX
    out = []
    for row in reader:
        try:
            lat = float(row[mapping["lat"]])
            lon = float(row[mapping["lon"]])
        except (ValueError, KeyError):
            continue
        if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
            continue
        try:
            frp = float(row.get(mapping.get("frp", ""), 0) or 0)
        except ValueError:
            frp = 0.0
        confidence = row.get(mapping.get("confidence", ""), "") if "confidence" in mapping else ""
        out.append({
            "lat": lat,
            "lon": lon,
            "frp": frp,
            "confidence": str(confidence),
            "satellite": "MTG-FCI",
            "acq_time_utc": timestamp_from_filename,
        })
    return out


def timestamp_from_url(url):
    # .../LSA-509_MTG_MTFRPPIXEL-ListProduct_MTG-FD_202607040100.csv.gz
    fname = url.rsplit("/", 1)[-1]
    digits = "".join(c for c in fname if c.isdigit())[-12:]
    if len(digits) != 12:
        return dt.datetime.utcnow().isoformat() + "Z"
    y, m, d, hh, mm = digits[0:4], digits[4:6], digits[6:8], digits[8:10], digits[10:12]
    return f"{y}-{m}-{d}T{hh}:{mm}:00Z"


# --------------------------------------------------------------------------
# CICLO PRINCIPAL
# --------------------------------------------------------------------------

def run_once(output_path):
    url = find_latest_csv_gz()
    if not url:
        log("Não encontrei nenhum ficheiro ListProduct recente na listagem.")
        return False

    log(f"Ficheiro mais recente: {url}")
    ts = timestamp_from_url(url)
    try:
        csv_text = download_csv(url)
    except requests.HTTPError as e:
        log(f"Falha no download (HTTP {e.response.status_code}). "
            f"Confirma as credenciais LSA SAF.")
        return False

    detections = parse_rows(csv_text, ts)
    log(f"{len(detections)} deteções dentro de Portugal Continental.")

    payload = {
        "generated_at": dt.datetime.utcnow().isoformat() + "Z",
        "source_file": url,
        "source_timestamp_utc": ts,
        "detections": detections,
    }
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    log(f"Escrito em {output_path}")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--outdir", default=".", help="Pasta onde escrever o JSON (por omissão, pasta atual)")
    parser.add_argument("--watch", action="store_true", help="Corre em ciclo contínuo, a cada 10 minutos")
    args = parser.parse_args()

    output_path = os.path.join(args.outdir, OUTPUT_PATH)

    if not args.watch:
        ok = run_once(output_path)
        sys.exit(0 if ok else 1)

    log("Modo contínuo iniciado (Ctrl+C para parar).")
    while True:
        try:
            run_once(output_path)
        except Exception as e:
            log(f"Erro inesperado: {e}")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main()
