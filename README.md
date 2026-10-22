# ⚡ Preço da eletricidade em Portugal

Página estática que acompanha diariamente o preço da eletricidade no mercado
grossista ibérico (OMIE, zona Portugal) e o consumo mensal por distrito
(E-REDES open data): melhores horas para consumir, médias diárias e mensais,
preços negativos, comparativo com Espanha e mapas de calor.

**→ [bernardoalmeida25.github.io/portuguese-electricity-prices](https://bernardoalmeida25.github.io/portuguese-electricity-prices/)**

## Dados e fontes

| Dado | Fonte | Cobertura |
|---|---|---|
| Preços PT/ES do mercado diário (€/MWh) | [OMIE](https://www.omie.es) — ficheiro `marginalpdbc` | jan-2023 → hoje |
| Consumo mensal por distrito (kWh) | [Portal Open Data E-REDES](https://e-redes.opendatasoft.com) | nov-2020 → último mês faturado |

Os dados de consumo da E-REDES estão licenciados **CC BY 4.0** — ao
reutilizá-los, cita *"E-REDES – Distribuição de eletricidade, Portal Open Data
E-REDES"*. Os preços do OMIE estão sujeitos às condições de uso do OMIE.

## Estrutura

```
├── main.py                # descarrega os preços OMIE → data/omie_precos.csv
├── fetch_eredes.py        # descarrega consumos por distrito → data/eredes_consumos.csv
├── build_site_data.py     # agrega tudo → site/data/stats.json + CSVs de download
├── data/                  # estado do histórico (CSVs seguidos em git)
├── site/
│   ├── index.html         # a página (PT/EN, €/MWh ↔ c€/kWh)
│   └── data/              # gerado pelo build (ignorado; criado em cada deploy)
└── .github/workflows/update-site.yml
```

## Uso local

Precisas de [uv](https://docs.astral.sh/uv/) e de Python ≥ 3.10
(`uv run` trata das dependências declaradas no cabeçalho de cada script).

```bash
uv run main.py             # preços OMIE (continua a partir do último dia no CSV)
uv run fetch_eredes.py     # consumos por distrito (refaz sempre o histórico)
uv run build_site_data.py  # gera site/data/stats.json + CSVs de download
uv run python -m http.server -d site   # ver em http://localhost:8000
```

Nota: a página faz `fetch()` do `stats.json`, por isso tem de ser servida por
HTTP — abrir o `index.html` com `file://` não funciona.

## Automação (GitHub Actions)

Dois workflows:

1. **[`update-data.yml`](.github/workflows/update-data.yml)** — *recolha*: todos os
   dias às 12:30 UTC (13:30 no inverno / 14:30 no verão em Portugal — logo a
   seguir à publicação do preço D+1 pelo OMIE, ~13h), corre os três scripts e
   faz commit dos CSVs em `data/`. O E-REDES pode falhar sem bloquear o resto
   (é mensal).
2. **[`deploy-site.yml`](.github/workflows/deploy-site.yml)** — *publicação*:
   rebuilda os agregados e publica `site/` no Pages. Dispara em push para
   `main` que mexa em `site/**` e quando a recolha termina com sucesso
   (`workflow_run` — necessário porque os commits feitos com o
   `GITHUB_TOKEN` não disparam eventos `push`).

Para ativar: **Settings → Pages → Build and deployment → Source: GitHub
Actions**. Se o `site/data/` já estava seguido em git, remove-o do índice:

```bash
git rm -r --cached site/data
```

## Notas e limitações

- "Dia" = dia de entrega do OMIE (hora de Madrid); horas do dia = hora de Lisboa.
- Até setembro de 2025 o mercado diário tinha preços horários; depois passou a
  períodos de 15 minutos. As médias são calculadas sobre todos os períodos.
- O preço do mercado grossista é **igual para todo o país** (uma só zona de
  preço) — não existe preço por distrito. A parte distrital é o consumo.
- Os consumos da E-REDES são *faturados* (não em tempo real), com semanas de
  atraso; meses recentes podem ainda ser revistos por acertos de faturação.
- O "custo estimado" por distrito = consumo do mês × preço médio mensal OMIE —
  uma aproximação (nem todo o consumo é comprado no mercado diário e o perfil
  horário de cada distrito não é ponderado).
- O toggle PT/EN do site é só do lado do cliente: os motores de busca indexam a
  versão portuguesa.

## Licença

O código ainda não tem licença — acrescenta uma se quiseres que outros possam
reutilizá-lo. Os dados têm as condições indicadas na secção "Dados e fontes".