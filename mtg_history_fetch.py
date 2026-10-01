#!/usr/bin/env python3
"""
mtg_history_fetch.py
---------------------
Descarrega uma JANELA ESPECÍFICA e já passada do produto MTG Fire
Radiative Power Pixel (LSA-509, EUMETSAT/LSA SAF) e publica-a como um
"período de estudo", separado do histórico normal (que só guarda as
últimas 72h) — para rever na página um incêndio ou período concreto do
passado, carregado manualmente quando precisares, não automaticamente.

Reaproveita as funções de download/parsing já existentes em
mtg_fire_fetch.py (mesma fonte, mesmas credenciais).

Não corre em agendamento nenhum — dispara-se manualmente via
workflow_dispatch (ver .github/workflows/fetch-mtg-study.yml), indicando
a data e a janela de horas pretendida.

UTILIZAÇÃO
----------
    python3 mtg_history_fetch.py --date 2025-09-12 --start-hour 0 --end-hour 20 --outdir docs
"""

import os
import re
import sys
import json
import argparse
import datetime as dt
from urllib.parse import urljoin

import requests

import mtg_fire_fetch as base

STUDY_DIR_NAME = "mtg_study"
PATTERN = re.compile(r"LSA-509[\w\-]*ListProduct[\w\-]*_(\d{12})\.csv\.gz")


def _study_dir(outdir):
    d = os.path.join(outdir, STUDY_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def list_window_files(date_str, start_hour, end_hour):
    """Lista os ficheiros LSA-509 do dia pedido cujo instante (HH) cai
    dentro de [start_hour, end_hour). A listagem do diretório é pública
    (não precisa de credenciais) — só o download de cada ficheiro exige."""
    day = dt.datetime.strptime(date_str, "%Y-%m-%d")
    day_url = urljoin(base.BASE_URL, f"{day.year:04d}/{day.month:02d}/{day.day:02d}/")
    r = requests.get(day_url, timeout=base.REQUEST_TIMEOUT)
    r.raise_for_status()
    matches = {}
    for m in PATTERN.finditer(r.text):
        ts = m.group(1)
        hh = int(ts[8:10])
        if start_hour <= hh < end_hour:
            matches[ts] = day_url + m.group(0)  # dedup por timestamp
    return [matches[ts] for ts in sorted(matches.keys())]


def run(date_str, start_hour, end_hour, outdir):
    urls = list_window_files(date_str, start_hour, end_hour)
    if not urls:
        base.log(f"Nenhum ficheiro encontrado para {date_str} entre as {start_hour}h e as {end_hour}h.")
        return None

    base.log(f"{len(urls)} ficheiros encontrados — a descarregar…")
    snapshots = []
    total_detections = 0
    for i, url in enumerate(urls, 1):
        ts = base.timestamp_from_url(url)
        try:
            csv_text = base.download_csv(url)
        except requests.HTTPError as e:
            base.log(f"[{i}/{len(urls)}] falhou ({e}) — a saltar este instante.")
            continue
        detections = base.parse_rows(csv_text, ts)
        total_detections += len(detections)
        snapshots.append({"ts": ts, "detections": detections})
        base.log(f"[{i}/{len(urls)}] {ts} — {len(detections)} deteções")

    study_id = f"{date_str}_{start_hour:02d}-{end_hour:02d}"
    fname = f"{study_id}.json"
    sdir = _study_dir(outdir)
    generated_at = dt.datetime.utcnow().isoformat() + "Z"
    payload = {
        "id": study_id,
        "date": date_str,
        "start_hour": start_hour,
        "end_hour": end_hour,
        "generated_at": generated_at,
        "snapshots": snapshots,
    }
    with open(os.path.join(sdir, fname), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    # Atualiza o índice (pequeno — é o que a página descarrega para listar
    # os períodos disponíveis), substituindo uma entrada anterior com o
    # mesmo id se already existir (pedir a mesma janela outra vez atualiza-a).
    index_path = os.path.join(sdir, "index.json")
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            index = json.load(f)
        if not isinstance(index, list):
            index = []
    except (OSError, json.JSONDecodeError):
        index = []
    index = [e for e in index if e.get("id") != study_id]
    index.append({
        "id": study_id,
        "date": date_str,
        "start_hour": start_hour,
        "end_hour": end_hour,
        "file": fname,
        "generated_at": generated_at,
        "total_snapshots": len(snapshots),
        "total_detections": total_detections,
    })
    index.sort(key=lambda e: (e.get("date", ""), e.get("start_hour", 0)))
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)

    base.log(f"Período de estudo publicado: {fname} ({len(snapshots)} instantes, {total_detections} deteções)")
    return fname


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--date", required=True, help="AAAA-MM-DD")
    ap.add_argument("--start-hour", type=int, default=0)
    ap.add_argument("--end-hour", type=int, default=24)
    ap.add_argument("--outdir", default=".")
    args = ap.parse_args()

    ok = run(args.date, args.start_hour, args.end_hour, args.outdir)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
