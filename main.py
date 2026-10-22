# /// script
# requires-python = ">=3.10"
# dependencies = ["requests"]
# ///
"""
Recolhe os preços do mercado diário de eletricidade (OMIE, zonas PT e ES)
e guarda-os em data/omie_precos.csv.

Uso:
    uv run fetch_omie.py                       # continua a partir do último dia no CSV (ou 2023-01-01)
    uv run fetch_omie.py --start 2025-01-01    # backfill a partir de uma data
    uv run fetch_omie.py --start 2025-01-01 --end 2025-01-31

O OMIE publica o preço do dia D+1 por volta das 13h (hora de Madrid), por isso
por omissão o script tenta chegar até amanhã e ignora os dias ainda não publicados.

Colunas do CSV:
    date            dia de entrega (hora de Madrid, que é a hora do mercado)
    period          nº do período dentro do dia (1..24 ou 1..96; 23/25 ou 92/100 nos dias de mudança de hora)
    resolution_min  60 (horário) ou 15 (quarto-horário)
    start_utc       início do período em UTC (ISO 8601)
    start_lisbon    início do período em hora de Lisboa (ISO 8601)
    price_pt        preço marginal Portugal (EUR/MWh)
    price_es        preço marginal Espanha (EUR/MWh)
"""
from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

CSV_PATH = Path("data/omie_precos.csv")
FIELDS = ["date", "period", "resolution_min", "start_utc", "start_lisbon", "price_pt", "price_es"]
DEFAULT_START = date(2023, 1, 1)

MADRID = ZoneInfo("Europe/Madrid")
LISBON = ZoneInfo("Europe/Lisbon")

# Padrão de download usado pelo site do OMIE para o ficheiro "marginalpdbc"
# (preço marginal do mercado diário). Se mudar, só é preciso alterar aqui.
URL = "https://www.omie.es/en/file-download?parents%5B0%5D=marginalpdbc&filename=marginalpdbc_{ymd}.{ver}"
VERSIONS = ("1", "2")  # tenta .1 e depois .2

HEADERS = {"User-Agent": "Mozilla/5.0 (dataset-eletricidade; +https://github.com/bernardoAlmeida25)"}


def fetch_day(session: requests.Session, day: date) -> str | None:
    """Devolve o texto do ficheiro do dia, ou None se ainda não existir."""
    for ver in VERSIONS:
        url = URL.format(ymd=day.strftime("%Y%m%d"), ver=ver)
        for attempt in range(3):
            try:
                r = session.get(url, headers=HEADERS, timeout=30)
            except requests.RequestException as e:
                print(f"  {day} .{ver}: erro de rede ({e}), tentativa {attempt + 1}/3", file=sys.stderr)
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code == 200 and r.text.strip().upper().startswith("MARGINALPDBC"):
                return r.text
            break  # 404 / HTML de erro / ficheiro vazio -> tenta a versão seguinte
    return None


def resolution_for(n_periods: int) -> int:
    if n_periods in (23, 24, 25):
        return 60
    if n_periods in (92, 96, 100):
        return 15
    raise ValueError(f"Número de períodos inesperado: {n_periods}")


def parse(text: str, day: date) -> list[dict]:
    """Converte o conteúdo do ficheiro marginalpdbc em linhas de CSV."""
    raw = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.upper().startswith("MARGINALPDBC") or line.startswith("*"):
            continue
        parts = [p for p in line.split(";") if p != ""]
        if len(parts) < 6:
            continue
        y, m, d, period = (int(parts[i]) for i in range(4))
        if date(y, m, d) != day:
            continue
        # Ordem das colunas no ficheiro: Portugal primeiro, Espanha depois.
        # Validado com 2025-01-01 (boletim MIBEL: média PT 103,62 / ES 100,93 €/MWh).
        price_pt, price_es = (float(parts[4].replace(",", ".")), float(parts[5].replace(",", ".")))
        raw.append((period, price_pt, price_es))

    if not raw:
        return []

    raw.sort()
    res = resolution_for(len(raw))
    midnight_utc = datetime(day.year, day.month, day.day, tzinfo=MADRID).astimezone(timezone.utc)

    rows = []
    for period, price_pt, price_es in raw:
        start_utc = midnight_utc + timedelta(minutes=res * (period - 1))
        rows.append({
            "date": day.isoformat(),
            "period": period,
            "resolution_min": res,
            "start_utc": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "start_lisbon": start_utc.astimezone(LISBON).isoformat(),
            "price_pt": price_pt,
            "price_es": price_es,
        })
    return rows


def load_existing() -> dict[tuple[str, int], dict]:
    if not CSV_PATH.exists():
        return {}
    with CSV_PATH.open(newline="", encoding="utf-8") as f:
        return {(r["date"], int(r["period"])): r for r in csv.DictReader(f)}


def save(data: dict[tuple[str, int], dict]) -> None:
    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for key in sorted(data):
            w.writerow({k: data[key][k] for k in FIELDS})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=date.fromisoformat)
    ap.add_argument("--end", type=date.fromisoformat)
    ap.add_argument("--check", action="store_true",
                    help="Mostra a média diária PT/ES de cada dia descarregado (para validar colunas)")
    args = ap.parse_args()

    data = load_existing()

    if args.start:
        start = args.start
    elif data:
        start = date.fromisoformat(max(k[0] for k in data)) + timedelta(days=1)
    else:
        start = DEFAULT_START
    end = args.end or (datetime.now(MADRID).date() + timedelta(days=1))

    print(f"A recolher {start} -> {end}")
    session = requests.Session()
    new_days = 0
    missing = []

    day = start
    while day <= end:
        text = fetch_day(session, day)
        if text is None:
            missing.append(day)
        else:
            rows = parse(text, day)
            if not rows:
                missing.append(day)
            else:
                for r in rows:
                    data[(r["date"], int(r["period"]))] = r
                new_days += 1
                if args.check:
                    avg_pt = sum(float(r["price_pt"]) for r in rows) / len(rows)
                    avg_es = sum(float(r["price_es"]) for r in rows) / len(rows)
                    print(f"  {day}: {len(rows)} períodos | média PT {avg_pt:.2f} | média ES {avg_es:.2f}")
        day += timedelta(days=1)
        time.sleep(0.3)  # simpatia com o servidor

    save(data)
    print(f"{new_days} dia(s) gravados em {CSV_PATH} ({len(data)} linhas no total)")
    if missing:
        print(f"{len(missing)} dia(s) sem ficheiro: {missing[0]} ... {missing[-1]}" if len(missing) > 3
              else f"Dias sem ficheiro: {', '.join(map(str, missing))}")
    # Falha só se pedimos dias antigos e nenhum chegou (provável URL/formato errado)
    return 1 if new_days == 0 and start < datetime.now(MADRID).date() - timedelta(days=2) else 0


if __name__ == "__main__":
    sys.exit(main())