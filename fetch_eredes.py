# /// script
# requires-python = ">=3.10"
# dependencies = ["requests"]
# ///
"""
Recolhe o consumo mensal de eletricidade faturado por distrito (E-REDES,
Portugal continental) e guarda-o em data/eredes_consumos.csv.

Uso:  uv run fetch_eredes.py

Fonte: Portal Open Data E-REDES (CC BY 4.0)
    https://e-redes.opendatasoft.com/explore/dataset/3-consumos-faturados-por-municipio-ultimos-10-anos/

O dataset original está desagregado por concelho, freguesia e nível de tensão
(milhares de linhas por mês). Para não andar a descarregar isso tudo, o script
agrega no próprio servidor (parâmetro group_by da API) e guarda apenas o total
de energia ativa faturada por distrito e mês — ficam sempre ~20 pedidos
pequenos em vez de um download gigante.

Os consumos são faturados, não medidos em tempo real: o mês em curso ainda não
aparece e meses passados podem ser revistos por acertos de faturação. Por isso
cada execução refaz o histórico completo e sobrescreve o CSV.

Colunas do CSV:
    month     ano-mês do período de consumo (YYYY-MM)
    distrito  distrito do continente (os registos "OUTROS *distrito*"
              exigidos pelo RGPD já ficam somados dentro do distrito certo,
              porque o agrupamento é ao nível do distrito)
    kwh       energia ativa faturada no mês (kWh)
"""
from __future__ import annotations

import csv
import re
import sys
import time
from pathlib import Path

import requests

BASE = "https://e-redes.opendatasoft.com"
DATASET = "3-consumos-faturados-por-municipio-ultimos-10-anos"
CSV_PATH = Path("data/eredes_consumos.csv")
FIELDS = ["month", "distrito", "kwh"]
PAGE = 100

HEADERS = {"User-Agent": "Mozilla/5.0 (dataset-eletricidade; +https://github.com/bernardoAlmeida25)"}

# Nomes possíveis dos campos no dataset; o nome verdadeiro é descoberto em
# runtime através do endpoint de metadados. Se a E-REDES renomear colunas,
# basta acrescentar candidatos aqui.
CANDIDATES = {
    "month": ["date", "ano_mes", "invoiced_year_month", "data", "mes", "month"],
    "distrito": ["distrito", "district"],
    "energia": ["energia_ativa", "energia", "energia_ativa_kwh", "consumo",
                "kwh", "energia_ativa_faturada", "energia_ativa_kwh_faturada"],
}


def get(url: str, params: dict | None = None) -> dict:
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=60)
            if r.status_code == 200:
                return r.json()
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            print(f"  erro de rede ({e}), tentativa {attempt + 1}/4", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Sem resposta de {url}")


def field_names() -> list[str]:
    """Descobre os nomes dos campos do dataset em runtime."""
    # 1) endpoint de metadados (a estrutura da resposta já variou entre
    #    versões do portal, por isso aceitamos várias formas)
    try:
        meta = get(f"{BASE}/api/explore/v2.1/catalog/datasets/{DATASET}/")
        for node in (meta.get("dataset"), (meta.get("results") or [None])[0], meta):
            if isinstance(node, dict) and node.get("fields"):
                return [f["name"] for f in node["fields"]]
        print(f"  metadados em formato inesperado: {str(meta)[:200]}", file=sys.stderr)
    except (RuntimeError, ValueError) as e:
        print(f"  metadados indisponíveis: {e}", file=sys.stderr)
    # 2) plano B: os nomes dos campos são as chaves dos próprios registos
    js = get(f"{BASE}/api/explore/v2.1/catalog/datasets/{DATASET}/records", {"limit": 20})
    keys: set = set()
    for r in js.get("results", []):
        keys.update(r.keys())
    if not keys:
        raise RuntimeError("Não consegui obter os nomes dos campos (nem metadados nem registos).")
    return sorted(keys)


def resolve_fields() -> dict[str, str]:
    """Mapeia {mês, distrito, energia} para os nomes reais dos campos."""
    fields = field_names()
    low = {f.lower(): f for f in fields}
    heur = {
        "month": lambda n: "mes" in n or "mês" in n or "month" in n or n in ("date", "data"),
        "distrito": lambda n: "distrito" in n,
        "energia": lambda n: "energia" in n or "consum" in n or "kwh" in n,
    }
    resolved = {}
    for canon, cands in CANDIDATES.items():
        hit = next((low[c] for c in cands if c in low), None)
        if hit is None:  # último recurso: correspondência por parecença
            hit = next((f for f in fields if heur[canon](f.lower())), None)
        if hit is None:
            raise RuntimeError(
                f"Não encontrei o campo '{canon}' no dataset. Campos disponíveis: {fields}. "
                "Atualiza o CANDIDATES deste script."
            )
        resolved[canon] = hit
    return resolved


def fetch_consumos(f: dict[str, str]) -> list[dict]:
    """Agrega no servidor: SELECT mês, distrito, SUM(energia) GROUP BY ambos."""
    select = f"{f['month']} as month, {f['distrito']} as distrito, sum({f['energia']}) as kwh"
    group_by = f"{f['month']},{f['distrito']}"
    rows: list[dict] = []
    offset = 0
    while True:
        js = get(
            f"{BASE}/api/explore/v2.1/catalog/datasets/{DATASET}/records",
            {"select": select, "group_by": group_by, "limit": PAGE, "offset": offset},
        )
        batch = js.get("results", [])
        rows.extend(batch)
        if len(batch) < PAGE:
            return rows
        offset += PAGE
        time.sleep(0.2)  # simpatia com o servidor


MONTH_RE = re.compile(r"(\d{4}-\d{2})")


def norm_rows(raw: list[dict]) -> list[dict]:
    """Normaliza o que a API devolve (datas, strings, nulos) e ordena."""
    out = []
    for r in raw:
        m = MONTH_RE.search(str(r.get("month", "")))
        if not m:
            continue
        distrito = str(r.get("distrito", "")).strip()
        if not distrito:
            continue
        kwh = r.get("kwh")
        kwh = float(kwh) if kwh not in (None, "") else 0.0
        if kwh > 0:
            out.append({"month": m.group(1), "distrito": distrito, "kwh": kwh})
    out.sort(key=lambda r: (r["month"], r["distrito"]))
    return out


def main() -> int:
    f = resolve_fields()
    print(f"Campos no dataset: mês={f['month']} · distrito={f['distrito']} · energia={f['energia']}")
    rows = norm_rows(fetch_consumos(f))
    if not rows:
        print("A API não devolveu linhas", file=sys.stderr)
        return 1

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with CSV_PATH.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    months = sorted({r["month"] for r in rows})
    dists = sorted({r["distrito"] for r in rows})
    print(f"{len(rows)} linhas gravadas em {CSV_PATH} · {months[0]} → {months[-1]} · {len(dists)} distritos")
    return 0


if __name__ == "__main__":
    sys.exit(main())