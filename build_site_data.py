# /// script
# requires-python = ">=3.10"
# dependencies = ["pandas>=2.0"]
# ///
"""
Lê data/omie_precos.csv e gera site/data/stats.json com todas as estatísticas
que a página (site/index.html) desenha. O CSV completo é grande demais para o
browser, por isso o agregado é calculado aqui.

Se existir data/eredes_consumos.csv (criado pelo fetch_eredes.py), acrescenta
ao JSON a secção "eredes" com o consumo por distrito; a página esconde a
secção correspondente se não houver dados.

Uso:  uv run build_site_data.py

Convenções:
  - "dia" = dia de entrega do OMIE (hora de Madrid), igual ao dos boletins do MIBEL.
  - Perfis por hora/horas do dia = hora de Lisboa.
  - Preços em €/MWh (a página converte para c€/kWh se o utilizador quiser).
  - Consumo por distrito = energia ativa faturada (E-REDES), em GWh.
"""
from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

CSV = Path("data/omie_precos.csv")
ER_CSV = Path("data/eredes_consumos.csv")
OUT = Path("site/data/stats.json")
TZ = "Europe/Lisbon"
EPS = 0.005  # tolerância para considerar PT == ES


def num(v, nd=2):
    """float arredondado ou None (JSON não aceita NaN)."""
    if v is None or pd.isna(v):
        return None
    return round(float(v), nd)


def nums(series, nd=2):
    return [num(v, nd) for v in series]


def window_stats(df: pd.DataFrame, start, end=None) -> float | None:
    sel = df[df["dt"] >= start]
    if end is not None:
        sel = sel[sel["dt"] < end]
    return num(sel["price_pt"].mean()) if len(sel) else None


def main() -> int:
    if not CSV.exists():
        print(f"Não encontrei {CSV}", file=sys.stderr)
        return 1

    df = pd.read_csv(CSV)
    if df.empty:
        print("CSV vazio", file=sys.stderr)
        return 1

    df["ts"] = pd.to_datetime(df["start_utc"], utc=True)
    lis = df["ts"].dt.tz_convert(TZ)
    df["hour"] = lis.dt.hour
    df["hhmm"] = lis.dt.strftime("%H:%M")
    df["month"] = df["date"].str[:7]
    df["dt"] = pd.to_datetime(df["date"])
    df["hours"] = df["resolution_min"] / 60.0
    df["spread"] = df["price_pt"] - df["price_es"]
    df = df.sort_values(["date", "period"]).reset_index(drop=True)

    last_date = df["date"].max()
    last_dt = pd.Timestamp(last_date)
    first_date = df["date"].min()

    # ------------------------------------------------------------ diário
    daily = (
        df.groupby("date")
        .agg(avg=("price_pt", "mean"), mn=("price_pt", "min"), mx=("price_pt", "max"))
        .reset_index()
        .sort_values("date")
    )
    daily_idx = pd.to_datetime(daily["date"])
    daily["ma7"] = daily.set_index(daily_idx)["avg"].rolling("7D", min_periods=4).mean().values

    # ------------------------------------------------------------ KPIs
    last_row = daily.iloc[-1]
    prev_avg = None
    if len(daily) > 1 and (last_dt - pd.Timestamp(daily.iloc[-2]["date"])).days == 1:
        prev_avg = float(daily.iloc[-2]["avg"])

    avg7 = window_stats(df, last_dt - pd.Timedelta(days=6))
    avg30 = window_stats(df, last_dt - pd.Timedelta(days=29))
    # mesmo período do ano anterior (só se houver cobertura razoável)
    py_start = last_dt - pd.Timedelta(days=29) - pd.DateOffset(years=1)
    py_end = last_dt + pd.Timedelta(days=1) - pd.DateOffset(years=1)
    py_sel = df[(df["dt"] >= py_start) & (df["dt"] < py_end)]
    avg30_py = num(py_sel["price_pt"].mean()) if py_sel["date"].nunique() >= 20 else None

    i_max = df["price_pt"].idxmax()
    i_min = df["price_pt"].idxmin()
    d_max = daily.loc[daily["avg"].idxmax()]
    d_min = daily.loc[daily["avg"].idxmin()]
    last30 = df[df["dt"] >= last_dt - pd.Timedelta(days=29)]
    nonpos30 = float(last30.loc[last30["price_pt"] <= 0, "hours"].sum())

    last365_daily = daily[pd.to_datetime(daily["date"]) > last_dt - pd.Timedelta(days=365)]
    pct_cheaper = None
    if len(last365_daily) >= 30:
        pct_cheaper = num((last365_daily["avg"] < last_row["avg"]).mean() * 100, 0)

    kpis = {
        "last_date": last_date,
        "last_avg": num(last_row["avg"]),
        "prev_avg": num(prev_avg),
        "avg7": avg7,
        "avg30": avg30,
        "avg30_prev_year": avg30_py,
        "max_day": {"date": d_max["date"], "avg": num(d_max["avg"])},
        "min_day": {"date": d_min["date"], "avg": num(d_min["avg"])},
        "max_period": {"date": df.loc[i_max, "date"], "time": df.loc[i_max, "hhmm"], "price": num(df.loc[i_max, "price_pt"])},
        "min_period": {"date": df.loc[i_min, "date"], "time": df.loc[i_min, "hhmm"], "price": num(df.loc[i_min, "price_pt"])},
        "nonpositive_hours_30d": num(nonpos30, 1),
        "pct_days_cheaper_than_last": pct_cheaper,
    }

    # ------------------------------------------------------------ mensal
    monthly = df.groupby("month").agg(avg=("price_pt", "mean"), days=("date", "nunique")).reset_index()
    monthly["full"] = [
        bool(d >= pd.Period(m).days_in_month) for m, d in zip(monthly["month"], monthly["days"])
    ]
    mdf = monthly.copy()
    mdf["year"] = mdf["month"].str[:4].astype(int)
    mdf["m"] = mdf["month"].str[5:7].astype(int)
    years = sorted(mdf["year"].unique())[-4:]
    yoy = {}
    for y in years:
        sub = mdf[mdf["year"] == y].set_index("m")["avg"]
        yoy[str(y)] = [num(sub.get(m)) for m in range(1, 13)]

    # ------------------------------------------------------------ perfis
    def hour_profile(sel: pd.DataFrame):
        p = sel.groupby("hour")["price_pt"].mean().reindex(range(24))
        return nums(p)

    d365 = df[df["dt"] > last_dt - pd.Timedelta(days=365)]
    d30 = df[df["dt"] > last_dt - pd.Timedelta(days=30)]
    wk = d365["dt"].dt.dayofweek
    profile = {
        "last30": hour_profile(d30),
        "last365": hour_profile(d365),
        "weekday365": hour_profile(d365[wk < 5]),
        "weekend365": hour_profile(d365[wk >= 5]),
    }
    dow_daily = (
        d365.groupby(["date"])["price_pt"].mean().reset_index().assign(
            dow=lambda x: pd.to_datetime(x["date"]).dt.dayofweek
        )
    )
    dow = nums(dow_daily.groupby("dow")["price_pt"].mean().reindex(range(7)))

    # ------------------------------------------------------------ heatmap hora x mês
    months_all = sorted(df["month"].unique())[-24:]
    hm = (
        df[df["month"].isin(months_all)]
        .groupby(["month", "hour"])["price_pt"]
        .mean()
        .unstack("hour")
        .reindex(index=months_all, columns=range(24))
    )
    heatmap = {"months": months_all, "values": [nums(hm.loc[m]) for m in months_all]}

    # ------------------------------------------------------------ preços <= 0
    df["nonpos_h"] = (df["price_pt"] <= 0) * df["hours"]
    npm = df.groupby("month").agg(h=("nonpos_h", "sum"), total=("hours", "sum")).reset_index()
    nonpositive = {
        "months": npm["month"].tolist(),
        "hours": nums(npm["h"], 1),
        "share_pct": nums(npm["h"] / npm["total"] * 100, 1),
        "total_hours": num(df["nonpos_h"].sum(), 1),
    }

    # ------------------------------------------------------------ PT vs ES
    df["sep"] = (df["spread"].abs() > EPS).astype(float)
    sp = df.groupby("month").agg(
        sep=("sep", "mean"),
        abs_spread=("spread", lambda s: s.abs().mean()),
        mean_spread=("spread", "mean"),
    ).reset_index()
    spread = {
        "months": sp["month"].tolist(),
        "separation_pct": nums(sp["sep"] * 100, 1),
        "mean_abs_spread": nums(sp["abs_spread"]),
        "mean_spread": nums(sp["mean_spread"]),
    }

    # ------------------------------------------------------------ E-REDES: consumo por distrito
    # Opcional: só existe se o fetch_eredes.py já tiver corrido.
    eredes = None
    if ER_CSV.exists():
        er = pd.read_csv(ER_CSV, dtype={"month": str, "distrito": str})
        er["month"] = er["month"].astype(str).str[:7]
        er = er.groupby(["month", "distrito"], as_index=False)["kwh"].sum()
        er["gwh"] = er["kwh"] / 1e6

        # custo estimado: consumo do mês × preço médio mensal OMIE
        # (o preço é igual para todo o país; aproximação porque nem todo o
        # consumo é comprado no mercado diário e o perfil horário não pesa)
        price_m = df.groupby("month")["price_pt"].mean()  # €/MWh
        er = er.merge(price_m.rename("price_eur_mwh"), how="left", left_on="month", right_index=True)
        er["cost_eur"] = er["kwh"] / 1000 * er["price_eur_mwh"]  # kWh→MWh × €/MWh

        months_er = sorted(er["month"].unique())
        hm_months = months_er[-24:]
        # distritos ordenados por consumo total do período (mais para menos)
        tot = er[er["month"].isin(hm_months)].groupby("distrito")["gwh"].sum()
        districts = tot.sort_values(ascending=False).index.tolist()
        piv = (
            er[er["month"].isin(hm_months)]
            .pivot_table(index="distrito", columns="month", values="gwh", aggfunc="sum")
            .reindex(index=districts, columns=hm_months)
        )

        # mês de referência do ranking: o mais recente que já tem todos os
        # distritos (o último mês pode ainda estar a ser faturado)
        counts = er.groupby("month")["distrito"].nunique()
        rank_month = [m for m in months_er if counts[m] == counts.max()][-1]
        gwh_last = er[er["month"] == rank_month].groupby("distrito")["gwh"].sum()
        py_month = f"{int(rank_month[:4]) - 1}{rank_month[4:]}"  # mesmo mês, ano anterior
        gwh_py = er[er["month"] == py_month].set_index("distrito")["gwh"]
        price_rank = price_m.get(rank_month, float("nan"))
        ranking = []
        for d in gwh_last.sort_values(ascending=False).index:
            yoy = None
            if d in gwh_py.index and gwh_py[d] > 0:
                yoy = num((gwh_last[d] / gwh_py[d] - 1) * 100, 1)
            cost = num(gwh_last[d] * price_rank / 1000, 1) if pd.notna(price_rank) else None
            ranking.append({"distrito": d, "gwh": num(gwh_last[d], 1), "cost_meur": cost, "yoy_pct": yoy})

        eredes = {
            "months": hm_months,
            "districts": districts,
            "heatmap_gwh": [[num(v, 1) for v in piv.loc[d]] for d in districts],
            "ranking_month": rank_month,
            "price_rank_month": num(price_rank),
            "total_cost_meur": num(gwh_last.sum() * price_rank / 1000, 0),
            "ranking": ranking,
            "total_24m_gwh": num(tot.sum(), 0),
        }

    # ------------------------------------------------------------ último dia
    rows = df[df["date"] == last_date].sort_values("ts").reset_index(drop=True)
    res = int(rows["resolution_min"].iloc[0])
    win = max(1, int(180 / res))  # janela de 3 horas
    latest = {
        "date": last_date,
        "resolution_min": res,
        "times": rows["hhmm"].tolist(),
        "pt": nums(rows["price_pt"]),
        "es": nums(rows["price_es"]),
        "avg": num(rows["price_pt"].mean()),
        "min": num(rows["price_pt"].min()),
        "max": num(rows["price_pt"].max()),
        "cheap": None,
        "expensive": None,
    }
    if len(rows) >= win:
        roll = rows["price_pt"].rolling(win).mean()

        def win_info(end_idx):
            start_idx = int(end_idx) - win + 1
            end_ts = (rows["ts"].iloc[int(end_idx)] + pd.Timedelta(minutes=res)).tz_convert(TZ)
            return {
                "start": rows["hhmm"].iloc[start_idx],
                "end": end_ts.strftime("%H:%M"),
                "avg": num(roll.iloc[int(end_idx)]),
                "from_idx": start_idx,
                "to_idx": int(end_idx),
            }

        latest["cheap"] = win_info(roll.idxmin())
        latest["expensive"] = win_info(roll.idxmax())

    # ------------------------------------------------------------ rankings
    def rank(frame):
        return [{"date": r["date"], "avg": num(r["avg"])} for _, r in frame.iterrows()]

    rankings = {"expensive": rank(daily.nlargest(5, "avg")), "cheap": rank(daily.nsmallest(5, "avg"))}

    out = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "first_date": first_date,
            "last_date": last_date,
            "days": int(df["date"].nunique()),
            "rows": int(len(df)),
        },
        "kpis": kpis,
        "daily": {
            "date": daily["date"].tolist(),
            "avg": nums(daily["avg"]),
            "min": nums(daily["mn"]),
            "max": nums(daily["mx"]),
            "ma7": nums(daily["ma7"]),
        },
        "monthly": {
            "month": monthly["month"].tolist(),
            "avg": nums(monthly["avg"]),
            "full": monthly["full"].tolist(),
        },
        "yoy": yoy,
        "profile": profile,
        "dow": dow,
        "heatmap": heatmap,
        "nonpositive": nonpositive,
        "spread": spread,
        "eredes": eredes,
        "latest": latest,
        "rankings": rankings,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    # cópia do CSV completo para download na página
    shutil.copyfile(CSV, OUT.parent / CSV.name)
    if ER_CSV.exists():
        # CSV de consumos para download na página, já com o custo estimado
        # (meses anteriores a 2023 ficam sem preço/custo: o OMIE só é recolhido desde 2023-01)
        er[["month", "distrito", "kwh", "price_eur_mwh", "cost_eur"]].to_csv(
            OUT.parent / ER_CSV.name, index=False, float_format="%.2f", na_rep=""
        )
    print(f"{OUT} ({OUT.stat().st_size / 1024:.0f} KB) · {out['meta']['days']} dias · {first_date} → {last_date}"
          + (" · consumos E-REDES incluídos" if eredes else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())