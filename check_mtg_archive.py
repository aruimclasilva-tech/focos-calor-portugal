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
    try:
        r = requests.get(url, timeout=60)
    except requests.RequestException as e:
        # Falha de rede (não um 4xx/5xx) — devolve como "não encontrado"
        # em vez de deixar a exceção propagar e abortar o script a meio
        # de um percurso com muitos pedidos (ver discover_earliest).
        return 0, f"(pedido falhou: {e})"
    return r.status_code, (r.text if r.status_code == 200 else "")


# Entradas de diretório num Apache/nginx "autoindex" — nomes de
# pasta terminados em "/", em href="...". Serve tanto para os níveis
# AAAA/ e MM/ como DD/ (a estrutura é sempre BASE_URL/AAAA/MM/DD/).
DIR_ENTRY_PATTERN = re.compile(r'href="(\d{2,4})/"')


def list_subdirs(url):
    status, html = list_dir(url)
    if status != 200:
        return status, [], html[:0]
    names = sorted(set(DIR_ENTRY_PATTERN.findall(html)))
    # Se não apanhou nada, pode ser que o formato da listagem não seja o
    # esperado — devolve uma amostra do HTML para diagnóstico em vez de só
    # "sem entradas".
    sample = html[:1500] if not names else ""
    return status, names, sample


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

    # Tudo dentro de um try — para uma falha a meio do percurso (muitos
    # pedidos encadeados: anos -> meses -> dias) deixar pelo menos os
    # passos já percorridos no resultado, em vez de perder tudo.
    try:
        status, years, sample = list_subdirs(BASE_URL)
        result["steps"].append({"url": BASE_URL, "http_status": status, "entries": years, "html_sample": sample})
        if status != 200 or not years:
            result["earliest_date"] = None
            result["error"] = "não consegui listar o diretório base (ou está vazio) — ver html_sample no passo acima"
            return result

        # Tenta cada ano a partir do mais antigo — se um ano não tiver
        # meses (pasta vazia/placeholder), avança para o seguinte.
        for year in years:
            year_url = urljoin(BASE_URL, f"{year}/")
            status, months, sample = list_subdirs(year_url)
            result["steps"].append({"url": year_url, "http_status": status, "entries": months, "html_sample": sample})
            if status != 200 or not months:
                continue

            for month in months:
                month_url = urljoin(year_url, f"{month}/")
                status, days, sample = list_subdirs(month_url)
                result["steps"].append({"url": month_url, "http_status": status, "entries": days, "html_sample": sample})
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
                # este mês não tinha dias com ficheiros — tenta o mês seguinte
            # este ano não tinha meses com dias com ficheiros — tenta o ano seguinte

        result["earliest_date"] = None
        result["error"] = "percorri todos os anos/meses/dias listados e nenhum tinha ficheiros LSA-509"
        return result
    except Exception as e:
        result["earliest_date"] = None
        result["error"] = f"exceção não tratada a meio do percurso: {type(e).__name__}: {e}"
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
        try:
            result = discover_earliest(args.outdir)
        except Exception as e:
            # Nunca deixar uma exceção a meio do percurso (muitos pedidos
            # encadeados) abortar sem escrever nada — fica pelo menos o
            # erro e os passos já percorridos até à falha.
            result = {
                "checked_at": dt.datetime.utcnow().isoformat() + "Z",
                "mode": "discover_earliest",
                "base_url": BASE_URL,
                "earliest_date": None,
                "error": f"exceção não tratada: {type(e).__name__}: {e}",
            }
        os.makedirs(args.outdir, exist_ok=True)
        out_path = os.path.join(args.outdir, "mtg_archive_check.json")
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        # Sai sempre com sucesso — o resultado (incluindo "error", se a
        # procura não encontrou nada) é o que interessa consultar depois;
        # sair com erro aqui só impedia o passo seguinte de o publicar.
        sys.exit(0)

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
