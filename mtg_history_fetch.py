#!/usr/bin/env python3
"""
mtg_history_fetch.py
---------------------
Descarrega uma JANELA ESPECÍFICA e já passada do produto MTG Fire
Radiative Power Pixel (LSA-509, EUMETSAT/LSA SAF) e publica-a como um
"período de estudo", separado do histórico normal (que só guarda as
últimas 72h) — para rever na página um incêndio ou período concreto do
passado, carregado manualmente quando precisares, não automaticamente.

A janela é um intervalo [--start, --end) que pode atravessar a meia-noite
(e até vários dias) — percorre-se cada pasta de dia necessária e filtra-se
pelo instante exato de cada ficheiro, não só pela hora dentro de um único
dia.

Reaproveita as funções de download/parsing já existentes em
mtg_fire_fetch.py (mesma fonte, mesmas credenciais).

Não corre em agendamento nenhum — dispara-se manualmente via
workflow_dispatch (ver .github/workflows/fetch-mtg-study.yml).

UTILIZAÇÃO
----------
    python3 mtg_history_fetch.py --start 2026-07-02T03:00 --end 2026-07-03T05:00 --outdir docs

As horas são as mesmas que aparecem nos nomes dos ficheiros na fonte
(UTC) — sem conversão de fuso horário.
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


def parse_dt(s):
    """Aceita 'AAAA-MM-DDTHH:MM', 'AAAA-MM-DD HH:MM' ou 'AAAA-MM-DDTHH'."""
    s = s.strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H"):
        try:
            return dt.datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"Data/hora inválida: {s!r} (usa AAAA-MM-DDTHH:MM)")


def list_window_files(start_dt, end_dt):
    """Lista os ficheiros LSA-509 cujo instante cai dentro de
    [start_dt, end_dt), percorrendo cada pasta de dia necessária (a janela
    pode atravessar a meia-noite). A listagem do diretório é pública (não
    precisa de credenciais) — só o download de cada ficheiro exige."""
    matches = {}
    day = dt.datetime(start_dt.year, start_dt.month, start_dt.day)
    last_day = dt.datetime(end_dt.year, end_dt.month, end_dt.day)
    while day <= last_day:
        day_url = urljoin(base.BASE_URL, f"{day.year:04d}/{day.month:02d}/{day.day:02d}/")
        try:
            r = requests.get(day_url, timeout=base.REQUEST_TIMEOUT)
            r.raise_for_status()
        except requests.HTTPError:
            day += dt.timedelta(days=1)
            continue
        for m in PATTERN.finditer(r.text):
            ts = m.group(1)
            ts_dt = dt.datetime.strptime(ts, "%Y%m%d%H%M")
            if start_dt <= ts_dt < end_dt:
                matches[ts] = day_url + m.group(0)  # dedup por timestamp
        day += dt.timedelta(days=1)
    return [matches[ts] for ts in sorted(matches.keys())]


def run(start_dt, end_dt, outdir):
    urls = list_window_files(start_dt, end_dt)
    if not urls:
        base.log(f"Nenhum ficheiro encontrado entre {start_dt} e {end_dt}.")
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

    study_id = f"{start_dt:%Y%m%dT%H%M}-{end_dt:%Y%m%dT%H%M}"
    fname = f"{study_id}.json"
    sdir = _study_dir(outdir)
    generated_at = dt.datetime.utcnow().isoformat() + "Z"
    start_iso = start_dt.isoformat() + "Z"
    end_iso = end_dt.isoformat() + "Z"
    payload = {
        "id": study_id,
        "start": start_iso,
        "end": end_iso,
        "generated_at": generated_at,
        "snapshots": snapshots,
    }
    with open(os.path.join(sdir, fname), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    # Atualiza o índice (pequeno — é o que a página descarrega para listar
    # os períodos disponíveis), substituindo uma entrada anterior com o
    # mesmo id se já existir (pedir a mesma janela outra vez atualiza-a).
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
        "start": start_iso,
        "end": end_iso,
        "file": fname,
        "generated_at": generated_at,
        "total_snapshots": len(snapshots),
        "total_detections": total_detections,
    })
    index.sort(key=lambda e: e.get("start", ""))
    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False)

    base.log(f"Período de estudo publicado: {fname} ({len(snapshots)} instantes, {total_detections} deteções)")
    return fname


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", required=True, help="AAAA-MM-DDTHH:MM (início, inclusive)")
    ap.add_argument("--end", required=True, help="AAAA-MM-DDTHH:MM (fim, exclusivo)")
    ap.add_argument("--outdir", default=".")
    args = ap.parse_args()

    start_dt = parse_dt(args.start)
    end_dt = parse_dt(args.end)
    if end_dt <= start_dt:
        print("ERRO: --end tem de ser depois de --start.", file=sys.stderr)
        sys.exit(2)

    ok = run(start_dt, end_dt, args.outdir)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
