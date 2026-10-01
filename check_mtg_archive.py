#!/usr/bin/env python3
"""
check_mtg_archive.py
---------------------
Script de verificação PONTUAL (não faz parte do fluxo normal da
plataforma) — confirma se o servidor LSA SAF ainda tem ficheiros
LSA-509 (MTG Fire Radiative Power Pixel) guardados para uma data/janela
específica, sem precisar de credenciais (a listagem do diretório é
pública; só o download do próprio ficheiro .csv.gz exige autenticação).

Escreve o resultado num JSON simples para ser lido via API do GitHub
(sem precisar de aceder aos logs da Action).
"""
import os
import re
import sys
import json
import argparse
import datetime as dt
from urllib.parse import urljoin

import requests

BASE_URL = "https://datalsasaf.lsasvcs.ipma.pt/PRODUCTS/MTG/MTFRPPixel/NATIVE/"
PATTERN = re.compile(r"LSA-509[\w\-]*ListProduct[\w\-]*_(\d{12})\.csv\.gz")


def list_dir(url):
    r = requests.get(url, timeout=60)
    return r.status_code, (r.text if r.status_code == 200 else "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="AAAA-MM-DD")
    ap.add_argument("--start-hour", type=int, default=0)
    ap.add_argument("--end-hour", type=int, default=24)
    ap.add_argument("--outdir", default=".")
    args = ap.parse_args()

    day = dt.datetime.strptime(args.date, "%Y-%m-%d")
    day_url = urljoin(BASE_URL, f"{day.year:04d}/{day.month:02d}/{day.day:02d}/")

    status, html = list_dir(day_url)
    result = {
        "checked_at": dt.datetime.utcnow().isoformat() + "Z",
        "date": args.date,
        "directory_url": day_url,
        "http_status": status,
        "directory_exists": status == 200,
        "files_in_window": [],
        "total_files_found_in_day": 0,
    }

    if status == 200:
        matches = sorted(set(PATTERN.finditer(html)), key=lambda m: m.group(1))
        all_ts = [m.group(1) for m in matches]
        result["total_files_found_in_day"] = len(all_ts)
        for m in matches:
            ts = m.group(1)  # AAAAMMDDHHMM
            hh = int(ts[8:10])
            if args.start_hour <= hh < args.end_hour:
                result["files_in_window"].append({
                    "filename": m.group(0),
                    "timestamp": ts,
                })

    os.makedirs(args.outdir, exist_ok=True)
    out_path = os.path.join(args.outdir, "mtg_archive_check.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if status == 200 else 1)


if __name__ == "__main__":
    main()
