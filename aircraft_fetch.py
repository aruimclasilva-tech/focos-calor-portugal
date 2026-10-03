#!/usr/bin/env python3
"""
aircraft_fetch.py
------------------
Descarrega posições ADS-B ao vivo sobre Portugal Continental (adsb.fi, com
fallback para airplanes.live) e escreve um JSON simples que a página
focos_calor_portugal.html consegue ler diretamente (docs/portugal_aircraft.json).

Uma terceira fonte, adsb.lol, só é usada à parte — não faz parte deste
fallback principal — para uma verificação específica do grupo Heli INEM:
se uma das matrículas rastreadas (TRACKED_REGISTRATIONS) não aparecer na
fonte principal, vai-se às outras duas fontes confirmar se continua a
reportar, antes de a dar como tendo deixado de voar (ver
recheck_inem_on_other_sources).

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
# território a partir daí, com margem — dentro do limite de 250 NM destas
# APIs. O raio por si só não exclui Espanha com precisão (Portugal é
# estreito mas alongado — um círculo grande o suficiente para cobrir
# norte-sul acaba sempre por incluir uma faixa de Espanha a leste); por
# isso, a seguir ao pedido, filtra-se o resultado pelo retângulo
# PT_BBOX (o mesmo usado para os focos de calor MTG), que é que
# garante excluir Madrid etc. com precisão. O raio só precisa de ser
# grande o suficiente para não cortar nenhum canto do retângulo.
CENTER_LAT = 39.6
CENTER_LON = -8.0
RADIUS_NM = 195

# Retângulo de Portugal Continental (lat_min, lat_max, lon_min, lon_max) —
# igual ao BBOX de mtg_fire_fetch.py. Aplicado depois do pedido por raio,
# para recortar com precisão a parte do círculo que cai em Espanha.
PT_BBOX = (36.85, 42.2, -9.65, -6.05)


def within_pt_bbox(ac):
    lat, lon = ac.get("lat"), ac.get("lon")
    if lat is None or lon is None:
        return False
    lat_min, lat_max, lon_min, lon_max = PT_BBOX
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max

# Campos que a página realmente usa — mantemos o ficheiro pequeno.
# "dbFlags" é um bitmask da base de dados da fonte (convenção readsb/tar1090,
# usada por adsb.fi e airplanes.live): bit 1 = aeronave registada como
# militar. Serve para o filtro "Militares" no painel.
# "src" não vem da API — é marcado por este script (ver main() e
# recheck_inem_on_other_sources) com a fonte REAL de cada aeronave, já
# que uma Heli INEM recuperada pela verificação cruzada pode vir de uma
# fonte diferente da principal ("source" global). A página usa isto para
# o link "abrir tracker" e o campo "Fonte" do balão apontarem sempre
# para o sítio certo, aeronave a aeronave.
KEEP_FIELDS = [
    "hex", "flight", "r", "t", "desc", "category",
    "lat", "lon", "alt_baro", "gs", "track", "true_heading", "dbFlags", "src",
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


# ---------------------------------------------------------------------------
# Trajeto persistido (6h) das aeronaves do grupo Heli INEM
# ---------------------------------------------------------------------------
# Mantido em sincronia manualmente com INEM_HELI_GROUP em
# focos_calor_portugal.html — ao confirmar uma nova matrícula, adicionar
# aqui também. Ao contrário do rasto desenhado em runtime no browser (que
# só dura enquanto a página estiver aberta), este histórico é gravado pelo
# workflow a cada ciclo e publicado num ficheiro à parte, para ficar
# disponível na plataforma para qualquer pessoa que abra a página,
# independentemente do dispositivo/browser.
TRACKED_REGISTRATIONS = ["9H-GMA", "9H-MIA", "9H-GMF", "9H-GME"]

TRAIL_RETENTION_HOURS = 6
# Salvaguarda contra um ficheiro a crescer sem limite caso a cadência real
# venha a ser mais curta do que se espera (ex. 1/min durante 6h = 360).
TRAIL_MAX_POINTS_PER_AIRCRAFT = 500


def _norm_reg(r):
    return (r or "").upper().replace(" ", "").replace("-", "")


def _match_tracked(ac):
    """Devolve a matrícula "canónica" (de TRACKED_REGISTRATIONS, com
    hífen) se esta aeronave corresponder a uma base Heli INEM — confirma
    pela matrícula ADS-B ("r") e, se essa não bater certo, também pelo
    indicativo/callsign ("flight"). Cobre um caso já visto na prática: a
    base de dados de matrículas/tipos da fonte ADS-B pode estar errada
    para um hex em concreto (devolve outra matrícula/tipo qualquer), mas o
    indicativo que a própria aeronave transmite — convenção comum: a
    matrícula sem o hífen — continua correto e identifica-a na mesma."""
    reg_norm = _norm_reg(ac.get("r"))
    flight_norm = _norm_reg(ac.get("flight"))
    for tracked in TRACKED_REGISTRATIONS:
        tn = _norm_reg(tracked)
        if tn == reg_norm or (flight_norm and tn == flight_norm):
            return tracked
    return None


def update_trails(outdir, aircraft, generated_at):
    """Acrescenta a posição atual de cada aeronave rastreada ao histórico
    persistido, e remove o que já tem mais de TRAIL_RETENTION_HOURS. Não é
    fatal para o fetch principal se isto falhar (ver chamada em main())."""
    path = os.path.join(outdir, "aircraft_trails.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            trails = json.load(f)
        if not isinstance(trails, dict):
            trails = {}
    except Exception:
        trails = {}

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=TRAIL_RETENTION_HOURS)

    for ac in aircraft:
        canonical = _match_tracked(ac)
        if not canonical:
            continue
        lat, lon = ac.get("lat"), ac.get("lon")
        if lat is None or lon is None:
            continue
        key = canonical.upper()
        trails.setdefault(key, []).append({"t": generated_at, "lat": lat, "lon": lon})

    for key in list(trails.keys()):
        pruned = []
        for p in trails[key]:
            try:
                t = dt.datetime.fromisoformat(p["t"])
            except Exception:
                continue
            if t >= cutoff:
                pruned.append(p)
        pruned = pruned[-TRAIL_MAX_POINTS_PER_AIRCRAFT:]
        if pruned:
            trails[key] = pruned
        else:
            del trails[key]

    with open(path, "w", encoding="utf-8") as f:
        json.dump(trails, f, ensure_ascii=False)

    return path


# ---------------------------------------------------------------------------
# Última posição conhecida (nunca podada) de cada aeronave do grupo
# ---------------------------------------------------------------------------
# O trajeto acima guarda só as últimas TRAIL_RETENTION_HOURS (6h) — é o que
# desenha a linha do rasto recente. Mas quando uma aeronave para de
# reportar durante mais de 6h (pousada, transponder desligado, fora de
# alcance), esse corte apagava também a ÚLTIMA posição conhecida, fazendo
# o ícone correspondente desaparecer do mapa por completo — o que não é o
# pretendido: o Heli deve continuar sempre visível no último sítio onde
# foi visto, com a indicação de quando ("Last seen"), por muito tempo que
# tenha passado. Por isso guarda-se à parte, num ficheiro que nunca é
# podado por idade — só é substituído quando há uma leitura mais recente.
def update_last_seen(outdir, aircraft, generated_at):
    path = os.path.join(outdir, "aircraft_last_seen.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            last_seen = json.load(f)
        if not isinstance(last_seen, dict):
            last_seen = {}
    except Exception:
        last_seen = {}

    for ac in aircraft:
        canonical = _match_tracked(ac)
        if not canonical:
            continue
        lat, lon = ac.get("lat"), ac.get("lon")
        if lat is None or lon is None:
            continue
        key = canonical.upper()
        last_seen[key] = {"t": generated_at, "lat": lat, "lon": lon}

    with open(path, "w", encoding="utf-8") as f:
        json.dump(last_seen, f, ensure_ascii=False)

    return path


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


def fetch_adsblol():
    url = f"https://api.adsb.lol/v2/lat/{CENTER_LAT}/lon/{CENTER_LON}/dist/{RADIUS_NM}"
    resp = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    return data.get("ac", [])


# Mesmo formato de resposta nas três ("ac"/"aircraft" + os mesmos campos
# por aeronave), por isso o resultado de qualquer uma pode ser tratado
# tal e qual pelo resto do script (trim/_match_tracked/etc.).
ALL_SOURCES = {
    "adsb.fi": fetch_adsbfi,
    "airplanes.live": fetch_airplaneslive,
    "adsb.lol": fetch_adsblol,
}


def find_missing_inem(aircraft):
    """Devolve as matrículas do grupo Heli INEM que NÃO apareceram na
    resposta da fonte principal — não é necessariamente "deixou de
    voar", pode só ser um buraco de cobertura dessa fonte em concreto."""
    found = {c for c in (_match_tracked(ac) for ac in aircraft) if c}
    return [r for r in TRACKED_REGISTRATIONS if r not in found]


def recheck_inem_on_other_sources(missing_regs, primary_source_name):
    """Para cada matrícula Heli INEM que faltou na fonte principal, vai
    às OUTRAS DUAS fontes (de adsb.fi/airplanes.live/adsb.lol) ver se
    continua a reportar noutro lado, antes de se assumir que deixou
    mesmo de reportar. Só dispara quando há mesmo uma matrícula em
    falta — não faz pedidos extra às outras fontes no caso normal (tudo
    presente na fonte principal).

    Devolve (encontradas, origem) — "encontradas" é a lista dos
    registos ADS-B brutos para fundir na resposta final (mesmo formato
    das outras aeronaves), "origem" é {matrícula: nome_da_fonte}."""
    if not missing_regs:
        return [], {}

    other_sources = {name: fn for name, fn in ALL_SOURCES.items() if name != primary_source_name}
    still_missing = set(missing_regs)
    recovered = []
    recovered_from = {}

    for name, fn in other_sources.items():
        if not still_missing:
            break
        try:
            ac_list = fn()
        except Exception as exc:
            print(f"  (verificação INEM: {name} indisponível — {exc})", file=sys.stderr)
            continue
        for ac in ac_list:
            canonical = _match_tracked(ac)
            if canonical and canonical in still_missing and ac.get("lat") is not None and ac.get("lon") is not None:
                ac["src"] = name
                recovered.append(ac)
                recovered_from[canonical] = name
                still_missing.discard(canonical)

    return recovered, recovered_from


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

    if source and aircraft:
        for ac in aircraft:
            ac["src"] = source

    # Verificação específica do grupo Heli INEM: se alguma das matrículas
    # rastreadas não apareceu na fonte principal, vai às outras duas fontes
    # confirmar se continua a reportar noutro lado antes de se assumir que
    # deixou mesmo de voar (evita falsos "deixou de reportar" por um
    # buraco de cobertura de uma só fonte, em vez da aeronave em si).
    if source and aircraft:
        missing_inem = find_missing_inem(aircraft)
        if missing_inem:
            print(f"Aviso: {len(missing_inem)} aeronave(s) Heli INEM não apareceram em {source} ({missing_inem}) — a verificar nas outras fontes…")
            recovered, recovered_from = recheck_inem_on_other_sources(missing_inem, source)
            if recovered:
                aircraft = aircraft + recovered
                for reg, src in recovered_from.items():
                    print(f"  {reg} continua a reportar via {src} (não apareceu em {source})")
            still_missing = [r for r in missing_inem if r not in recovered_from]
            if still_missing:
                print(f"  {still_missing} não encontrada(s) em nenhuma fonte — deixou(aram) mesmo de reportar.")

    out = {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": source,
        "error": None if source else error,
        # Recorte a Portugal Continental (ver nota em PT_BBOX) — o raio por
        # si só inclui sempre uma faixa de Espanha a leste (ex. Madrid),
        # dada a forma alongada de Portugal.
        "aircraft": [
            trim(ac) for ac in aircraft
            if ac.get("lat") is not None and ac.get("lon") is not None and within_pt_bbox(ac)
        ],
    }

    os.makedirs(args.outdir, exist_ok=True)
    out_path = os.path.join(args.outdir, "portugal_aircraft.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    if source:
        print(f"OK — {len(out['aircraft'])} aeronaves via {source} -> {out_path}")
        try:
            trails_path = update_trails(args.outdir, out["aircraft"], out["generated_at"])
            print(f"Trajetos Heli INEM atualizados -> {trails_path}")
        except Exception as exc:
            print(f"Aviso: falha ao atualizar trajetos Heli INEM: {exc}", file=sys.stderr)
        try:
            last_seen_path = update_last_seen(args.outdir, out["aircraft"], out["generated_at"])
            print(f"Última posição conhecida atualizada -> {last_seen_path}")
        except Exception as exc:
            print(f"Aviso: falha ao atualizar última posição conhecida: {exc}", file=sys.stderr)
    else:
        print(f"FALHOU (adsb.fi e airplanes.live) — {error}", file=sys.stderr)
        # Não é fatal para o workflow: escreve o ficheiro na mesma (lista
        # vazia + erro registado) para a página poder mostrar isso.


if __name__ == "__main__":
    main()
