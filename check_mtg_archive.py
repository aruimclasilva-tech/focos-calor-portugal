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


# Entradas de diretório num Apache/nginx "autoindex" — nomes de
# pasta terminados em "/", em href="...". Serve tanto para os níveis
# AAAA/ e MM/ como DD/ (a estrutura é sempre BASE_URL/AAAA/MM/DD/).
DIR_ENTRY_PATTERN = re.compile(r'href="(\d{2,4})/"')


def list_subdirs(url):
    status, html = list_dir(url)
    if status != 200:
        return status, []
    names = sorted(set(DIR_ENTRY_PATTERN.findall(html)))
    return status, names


def discover_earliest(outdir):
    """Percorre BASE_URL/AAAA/ -> MM/ -> DD/ pelo valor mais pequeno em
    cada nível até encontrar o primeiro dia com pelo menos um ficheiro
    LSA-509 — essa é a data mais antiga disponível no arquivo."""
    result = {
        "checked_at": dt.datetime.utcnow().isoformat() + "Z",
        "mode": "discover_earliest",
        "base_url": BASE_URL,
        "steps": [],
    }

    status, years = list_subdirs(BASE_URL)
    result["steps"].append({"url": BASE_URL, "http_status": status, "entries": years})
    if status != 200 or not years:
        result["earliest_date"] = None
        result["error"] = "não consegui listar o diretório base (ou está vazio)"
        return result

    # Tenta cada ano a partir do mais antigo — se um ano não tiver meses
    # (pasta vazia/placeholder), avança para o seguinte.
    for year in years:
        year_url = urljoin(BASE_URL, f"{year}/")
        status, months = list_subdirs(year_url)
        result["steps"].append({"url": year_url, "http_status": status, "entries": months})
        if status != 200 or not months:
            continue

        for month in months:
            month_url = urljoin(year_url, f"{month}/")
            status, days = list_subdirs(month_url)
            result["steps"].append({"url": month_url, "http_status": status, "entries": days})
            if status != 200 or not days:
                continue

            for day in days:
                day_url = urljoin(month_url, f"{day}/")
                status, html = list_dir(day_url)
                if status != 200:
                    continue
                matches = sorted(set(PATTERN.finditer(html)), key=lambda m: m.group(1))
                if matches:
                    result["earliest_date"] = f"{year}-{month}-{day}"
                    result["earliest_directory_url"] = day_url
                    result["earliest_day_total_files"] = len(matches)
                    result["earliest_file"] = matches[0].group(0)
                    result["earliest_timestamp"] = matches[0].group(1)
                    return result
            # este mês não tinha nenhum dia com ficheiros — tenta o mês seguinte
        # este ano não tinha nenhum mês com dias com ficheiros — tenta o ano seguinte

    result["earliest_date"] = None
    result["error"] = "percorri todos os anos/meses/dias listados e nenhum tinha ficheiros LSA-509"
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="AAAA-MM-DD")
    ap.add_argument("--start-hour", type=int, default=0)
    ap.add_argument("--end-hour", type=int, default=24)
    ap.add_argument("--outdir", default=".")
    ap.add_argument("--discover-earliest", action="store_true",
                     help="Em vez de verificar --date, descobre a data mais antiga disponível no arquivo.")
    args = ap.parse_args()

    if args.discover_earliest:
        result = discover_earliest(args.outdir)
        os.makedirs(args.outdir, exist_ok=True)
        out_path = os.path.join(args.outdir, "mtg_archive_check.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        sys.exit(0 if result.get("earliest_date") else 1)

    if not args.date:
        print("ERRO: --date é obrigatório (ou usa --discover-earliest).", file=sys.stderr)
        sys.exit(2)

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
