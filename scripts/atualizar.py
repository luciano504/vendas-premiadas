# -*- coding: utf-8 -*-
"""
VENDAS PREMIADAS — carga VR -> Supabase.

Para cada campanha ativa em cv_campanhas:
  1. descobre as rodadas a carregar (mês corrente; rodadas antigas só uma vez,
     depois de fechadas);
  2. consulta no VR a quantidade vendida por loja, dia e grupo de itens
     (total e a parte vendida em oferta) e regrava cv_vendas;
  3. grava a base da meta (mês anterior e/ou mesmo mês do ano passado) em cv_base;
  4. grava a descrição dos códigos usados em cv_produtos.
Os pontos NÃO são calculados aqui: o app multiplica pela regra atual, então
mudar um peso no app recalcula a campanha na hora, sem esperar o workflow.
"""
import os, io, sys, json, time, calendar
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode
import requests
import pandas as pd

VR_URL = os.environ["VR_URL"]
SB_URL = os.environ["SUPABASE_URL"].rstrip("/")
SB_KEY = os.environ["SUPABASE_KEY"]
H = {"apikey": SB_KEY, "Authorization": "Bearer " + SB_KEY, "Content-Type": "application/json"}
DIAS_PARA_FECHAR = 3          # rodada é reconsultada até 3 dias depois de acabar (vendas tardias)
LOJAS_VALIDAS = {1, 2, 3, 4, 5, 8, 9}


def hoje_recife():
    return (datetime.now(timezone.utc) - timedelta(hours=3)).date()


# ----------------------------------------------------------------- VR
def query_vr(sql):
    body = urlencode({"sql_query": sql, "export_type": "csv"})
    ult = None
    for att in range(1, 6):
        try:
            r = requests.post(VR_URL, data=body, timeout=300,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
            r.raise_for_status()
            txt = r.text.lstrip("﻿").lstrip()
            ini = txt[:200].lower()
            if "streamcsv" in ini:                 # resultado vazio quebra o index.php
                return pd.DataFrame()
            if "login" in ini and "<" in ini[:5]:
                raise RuntimeError("O App SQL do VR pediu LOGIN — o VR_URL não está passando pela tela de login")
            if "fatal error" in ini or ini.startswith("<"):
                raise RuntimeError("VR devolveu HTML/erro: " + txt[:160].replace("\n", " "))
            df = pd.read_csv(io.StringIO(txt))
            df.columns = [str(c).lstrip("﻿").strip().lower() for c in df.columns]
            return df
        except Exception as e:
            ult = e
            if "LOGIN" in str(e):
                break
            time.sleep(6 * att)
    raise ult


def ids_sql(lst):
    return ",".join(str(int(x)) for x in lst) or "0"


def vendas_vr(cfg, ini, fim):
    """Quantidade por loja/dia/grupo entre ini e fim (inclusive)."""
    grupos = [g for g in cfg.get("grupos", []) if g.get("codigos")]
    if not grupos:
        return pd.DataFrame(columns=["loja", "dia", "grupo", "qtd", "qtd_oferta", "valor"])
    todos = [c for g in grupos for c in g["codigos"]]
    caso = "case " + " ".join(
        f"when vi.id_produto in ({ids_sql(g['codigos'])}) then '{str(g['id']).replace(chr(39), '')}'"
        for g in grupos) + " end"
    lojas = [l for l in cfg.get("lojas", []) if int(l) in LOJAS_VALIDAS]
    sql = f"""
        select v.id_loja as loja, date(v.data) as dia, {caso} as grupo,
               sum(vi.quantidade) as qtd,
               sum(case when vi.oferta::text in ('true','t','1') then vi.quantidade else 0 end) as qtd_oferta,
               sum(vi.valortotal) as valor
        from pdv.vendaitem vi
        join pdv.venda v on v.id = vi.id_venda
        where vi.id_produto in ({ids_sql(todos)})
          and v.id_loja in ({ids_sql(lojas)})
          and vi.cancelado <> '1' and v.cancelado <> '1'
          and date(v.data) between '{ini}' and '{fim}'
        group by 1, 2, 3
        union all
        select 0, date '{ini}', 'x', 0, 0, 0
    """
    df = query_vr(sql)
    if df.empty:
        return pd.DataFrame(columns=["loja", "dia", "grupo", "qtd", "qtd_oferta", "valor"])
    df = df[pd.to_numeric(df["loja"], errors="coerce").fillna(0) > 0].copy()
    df["loja"] = df["loja"].astype(int)
    for c in ("qtd", "qtd_oferta", "valor"):
        df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).round(3)
    df["dia"] = df["dia"].astype(str).str[:10]
    return df


def descricoes_vr(codigos):
    if not codigos:
        return []
    df = query_vr(f"select id, descricaocompleta as descricao from produto where id in ({ids_sql(codigos)})"
                  " union all select 0, ''")
    if df.empty:
        return []
    df = df[pd.to_numeric(df["id"], errors="coerce").fillna(0) > 0]
    return [{"id": int(r.id), "descricao": str(r.descricao)} for r in df.itertuples()]


# ----------------------------------------------------------- Supabase
def sb_get(path):
    out, ini = [], 0
    while True:
        r = requests.get(f"{SB_URL}/rest/v1/{path}", headers={**H, "Range": f"{ini}-{ini + 999}"}, timeout=60)
        r.raise_for_status()
        bloco = r.json()
        out += bloco
        if len(bloco) < 1000:
            return out
        ini += 1000


def sb_delete(path):
    r = requests.delete(f"{SB_URL}/rest/v1/{path}", headers=H, timeout=60)
    if r.status_code >= 300:
        raise RuntimeError(f"DELETE {path}: {r.status_code} {r.text[:200]}")


def sb_upsert(tabela, linhas, conflito):
    for i in range(0, len(linhas), 500):
        r = requests.post(f"{SB_URL}/rest/v1/{tabela}?on_conflict={conflito}",
                          headers={**H, "Prefer": "resolution=merge-duplicates,return=minimal"},
                          data=json.dumps(linhas[i:i + 500]), timeout=60)
        if r.status_code >= 300:
            raise RuntimeError(f"UPSERT {tabela}: {r.status_code} {r.text[:200]}")


def sb_patch(path, dados):
    requests.patch(f"{SB_URL}/rest/v1/{path}", headers=H, data=json.dumps(dados), timeout=60)


# ------------------------------------------------------------ datas
def ultimo_dia(a, m):
    return calendar.monthrange(a, m)[1]


def somar_meses(d, n, fim_de_mes=False):
    tot = d.year * 12 + (d.month - 1) + n
    a, m = divmod(tot, 12)
    m += 1
    ud = ultimo_dia(a, m)
    return date(a, m, ud if fim_de_mes else min(d.day, ud))


def rodadas(cfg, hoje):
    """Lista de (rodada, ini, fim) da campanha até hoje."""
    ini = date.fromisoformat(cfg["inicio"])
    if cfg.get("recorrencia", "mensal") == "mensal":
        fim_camp = date.fromisoformat(cfg["fim"]) if cfg.get("fim") else None
        out, d = [], date(ini.year, ini.month, 1)
        while d <= hoje and (fim_camp is None or d <= fim_camp):
            f = date(d.year, d.month, ultimo_dia(d.year, d.month))
            if fim_camp and f > fim_camp:
                f = fim_camp
            out.append((d.strftime("%Y-%m"), max(d, ini), f))
            d = f + timedelta(days=1)
            d = date(d.year, d.month, 1)
        return out
    fim = date.fromisoformat(cfg["fim"])
    return [("unica", ini, fim)] if ini <= hoje else []


def janela_base(modo, rodada, ini, fim, recorrencia):
    meses = -1 if modo == "anterior" else -12
    if recorrencia == "mensal":
        b_ini = somar_meses(date(ini.year, ini.month, 1), meses)
        return b_ini, date(b_ini.year, b_ini.month, ultimo_dia(b_ini.year, b_ini.month))
    eh_fim = fim.day == ultimo_dia(fim.year, fim.month)
    return somar_meses(ini, meses), somar_meses(fim, meses, fim_de_mes=eh_fim)


# ------------------------------------------------------------- carga
def carregar(camp, hoje, resumo):
    cfg = camp["config"]
    cid = camp["id"]
    rec = cfg.get("recorrencia", "mensal")
    modos = {"anterior": ["anterior"], "ano_passado": ["ano_passado"],
             "maior": ["anterior", "ano_passado"], "nenhuma": []}.get(cfg.get("meta", {}).get("modo", "anterior"), ["anterior"])
    fechadas = {r["rodada"] for r in sb_get(f"cv_rodadas?select=rodada&campanha_id=eq.{cid}&fechada=eq.true")}
    bases = {(b["rodada"], b["modo"]) for b in sb_get(f"cv_base?select=rodada,modo&campanha_id=eq.{cid}")}
    reconsultar = os.environ.get("RECARREGAR") == "1"

    for rod, ini, fim in rodadas(cfg, hoje):
        fechar = hoje > fim + timedelta(days=DIAS_PARA_FECHAR)
        if rod not in fechadas or reconsultar:
            ate = min(fim, hoje)
            df = vendas_vr(cfg, ini, ate)
            sb_delete(f"cv_vendas?campanha_id=eq.{cid}&dia=gte.{ini}&dia=lte.{ate}")
            linhas = [{"campanha_id": cid, "loja": int(r.loja), "dia": r.dia, "grupo": str(r.grupo),
                       "qtd": float(r.qtd), "qtd_oferta": float(r.qtd_oferta), "valor": float(r.valor)}
                      for r in df.itertuples()]
            if linhas:
                sb_upsert("cv_vendas", linhas, "campanha_id,loja,dia,grupo")
            sb_upsert("cv_rodadas", [{"campanha_id": cid, "rodada": rod, "fechada": fechar,
                                      "carregado_em": datetime.now(timezone.utc).isoformat()}],
                      "campanha_id,rodada")
            resumo.append(f"| {camp['nome']} | {rod} | vendas {ini:%d/%m}–{ate:%d/%m} | {len(linhas)} linhas |")

        for modo in modos:
            if (rod, modo) in bases and not reconsultar:
                continue
            b_ini, b_fim = janela_base(modo, rod, ini, fim, rec)
            if b_fim >= hoje:
                continue
            df = vendas_vr(cfg, b_ini, b_fim)
            dias = (b_fim - b_ini).days + 1
            sb_delete(f"cv_base?campanha_id=eq.{cid}&rodada=eq.{rod}&modo=eq.{modo}")
            agg = df.groupby(["loja", "grupo"], as_index=False)[["qtd", "qtd_oferta"]].sum() if len(df) else df
            linhas = [{"campanha_id": cid, "rodada": rod, "modo": modo, "loja": int(r.loja), "grupo": str(r.grupo),
                       "qtd": float(r.qtd), "qtd_oferta": float(r.qtd_oferta), "dias": dias,
                       "ini": str(b_ini), "fim": str(b_fim)} for r in agg.itertuples()]
            # loja sem venda na base também fica registrada (meta = mínimo)
            for l in cfg.get("lojas", []):
                if not any(x["loja"] == int(l) for x in linhas):
                    linhas.append({"campanha_id": cid, "rodada": rod, "modo": modo, "loja": int(l),
                                   "grupo": "_vazio", "qtd": 0, "qtd_oferta": 0, "dias": dias,
                                   "ini": str(b_ini), "fim": str(b_fim)})
            sb_upsert("cv_base", linhas, "campanha_id,rodada,modo,loja,grupo")
            resumo.append(f"| {camp['nome']} | {rod} | base {modo} {b_ini:%d/%m/%y}–{b_fim:%d/%m/%y} | {len(linhas)} linhas |")


def main():
    hoje = hoje_recife()
    camps = sb_get("cv_campanhas?select=id,nome,ativa,config&ativa=eq.true&order=id")
    print(f"{len(camps)} campanha(s) ativa(s) — hoje {hoje}")
    resumo = ["| Campanha | Rodada | Carga | Linhas |", "|---|---|---|---|"]
    falhas = 0
    codigos = set()
    for c in camps:
        try:
            for g in c["config"].get("grupos", []):
                codigos.update(int(x) for x in g.get("codigos", []))
            carregar(c, hoje, resumo)
            sb_patch(f"cv_campanhas?id=eq.{c['id']}",
                     {"ultima_carga": datetime.now(timezone.utc).isoformat(), "ultimo_erro": None})
        except Exception as e:
            falhas += 1
            msg = str(e)[:300]
            print(f"ERRO na campanha {c['id']}: {msg}")
            resumo.append(f"| {c['nome']} | — | **ERRO** {msg} | — |")
            sb_patch(f"cv_campanhas?id=eq.{c['id']}", {"ultimo_erro": msg})
    try:
        prods = descricoes_vr(sorted(codigos))
        if prods:
            sb_upsert("cv_produtos", prods, "id")
    except Exception as e:
        print("Descrições não carregadas:", str(e)[:200])

    print("\n".join(resumo))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("## Vendas Premiadas\n\n" + "\n".join(resumo) + "\n")
    if falhas and falhas == len(camps):
        sys.exit(1)


if __name__ == "__main__":
    main()
