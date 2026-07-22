"""Coletor B3 - arquivo unico v2."""
from __future__ import annotations
import re
import os
import io
import csv
import json
import time
import zipfile
import datetime
import traceback

import requests

try:
    import feedparser
except Exception:
    feedparser = None

OUTPUT_DIR = os.environ.get("COLLECTOR_OUTPUT", "output")
TICKER_RE = re.compile(r"\b([A-Z]{4}\d{1,2})\b")
HEADERS = {"User-Agent": "Mozilla/5.0"}


def extrair_tickers(texto):
    if not texto:
        return []
    return sorted(set(TICKER_RE.findall(texto.upper())))


def agora_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def limpar_html(html):
    return re.sub(r"<[^>]+>", "", html or "").replace("&nbsp;", " ").strip()


def escrever_json(caminho, dados):
    os.makedirs(os.path.dirname(caminho) or ".", exist_ok=True)
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)


class Source:
    name = "base"
    output = "base.json"

    def collect(self):
        raise NotImplementedError


FEEDS = [
    ("InfoMoney - Mercados", "https://www.infomoney.com.br/mercados/feed/"),
    ("InfoMoney - FIIs", "https://www.infomoney.com.br/onde-investir/fundos-imobiliarios/feed/"),
    ("Money Times", "https://www.moneytimes.com.br/feed/"),
    ("Brazil Journal", "https://braziljournal.com/feed/"),
    ("Valor Investe", "https://valorinveste.globo.com/rss/valorinveste/"),
    ("Suno Noticias", "https://www.suno.com.br/noticias/feed/"),
    ("InvestNews", "https://investnews.com.br/feed/"),
]


class NoticiasRSS(Source):
    name = "noticias"
    output = "noticias.json"

    def collect(self):
        if feedparser is None:
            raise RuntimeError("feedparser nao instalado")
        itens = []
        for nome, url in FEEDS:
            try:
                d = feedparser.parse(url)
            except Exception:
                continue
            for e in d.entries[:40]:
                titulo = getattr(e, "title", "") or ""
                resumo = limpar_html(getattr(e, "summary", "") or "")
                publicado = None
                pp = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
                if pp:
                    publicado = datetime.datetime.fromtimestamp(
                        time.mktime(pp), datetime.timezone.utc).isoformat()
                itens.append({
                    "fonte": nome, "titulo": titulo,
                    "url": getattr(e, "link", "") or "", "publicado": publicado,
                    "tickers": extrair_tickers(titulo + " " + resumo),
                    "resumo": resumo[:280],
                })
        itens.sort(key=lambda x: x["publicado"] or "", reverse=True)
        return itens


FNET_BASE = "https://fnet.bmfbovespa.com.br/fnet/publico"


class FNETFatosRelevantes(Source):
    name = "eventos_fnet"
    output = "eventos.json"

    def collect(self):
        params = {
            "d": 1, "s": 0, "l": 200, "o[0][dataEntrega]": "desc",
            "idCategoriaDocumento": 0, "idTipoDocumento": 0,
            "idEspecieDocumento": 0, "situacao": "A", "tipoFundo": 1,
        }
        r = requests.get(f"{FNET_BASE}/pesquisarGerenciadorDocumentosDados",
                         params=params,
                         headers={**HEADERS, "Accept": "application/json",
                                  "X-Requested-With": "XMLHttpRequest"},
                         timeout=40)
        r.raise_for_status()
        data = r.json().get("data", [])
        itens = []
        for doc in data:
            _id = doc.get("id")
            fundo = doc.get("descricaoFundo") or doc.get("denominacaoSocial") or ""
            itens.append({
                "id": _id, "fundo": fundo,
                "cnpj": doc.get("cnpjFundo") or doc.get("cnpj"),
                "categoria": doc.get("categoriaDocumento"),
                "tipo": doc.get("tipoDocumento"),
                "especie": doc.get("especieDocumento"),
                "data_referencia": doc.get("dataReferencia"),
                "data_entrega": doc.get("dataEntrega"),
                "tickers": extrair_tickers(fundo),
                "url": f"{FNET_BASE}/exibirDocumento?id={_id}&cvm=true" if _id else None,
            })
        return itens


CVM_INF = "https://dados.cvm.gov.br/dados/FII/DOC/INF_MENSAL/DADOS"


def _achar_col(campos, *chaves):
    for c in campos:
        cl = c.lower()
        if all(k in cl for k in chaves):
            return c
    return None


def _abrir_csv(z, nome):
    raw = z.read(nome)
    try:
        texto = raw.decode("latin-1")
    except Exception:
        texto = raw.decode("utf-8", "ignore")
    return list(csv.DictReader(io.StringIO(texto), delimiter=";"))


def _num(v):
    if v is None:
        return None
    v = str(v).strip().replace(".", "").replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


def _dkey(s):
    if not s:
        return ""
    s = str(s)
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return m.group(3) + m.group(2) + m.group(1)
    return s


class CVMFiiPVP(Source):
    name = "fii_pvp"
    output = "fii_pvp.json"

    def collect(self):
        ano = datetime.date.today().year
        conteudo = self._baixar(ano) or self._baixar(ano - 1)
        if conteudo is None:
            raise RuntimeError("nao foi possivel baixar o informe da CVM")
        z = zipfile.ZipFile(io.BytesIO(conteudo))
        reg = {}
        debug = {}
        for nome in z.namelist():
            linhas = _abrir_csv(z, nome)
            if not linhas:
                continue
            campos = list(linhas[0].keys())
            debug[nome] = campos
            col_cnpj = _achar_col(campos, "cnpj")
            if not col_cnpj:
                continue
            col_data = _achar_col(campos, "data", "refer") or _achar_col(campos, "data", "compet")
            col_nome = (_achar_col(campos, "denomin") or _achar_col(campos, "nome", "fund")
                        or _achar_col(campos, "nome"))
            col_vp = (_achar_col(campos, "valor", "patrimon", "cota")
                      or _achar_col(campos, "patrimon", "cota"))
            col_pl = None
            for c in campos:
                cl = c.lower()
                if "patrim" in cl and ("liqui" in cl or "líqui" in cl) and "cota" not in cl:
                    col_pl = c
                    break
            col_cotas = (_achar_col(campos, "cotas", "emitid") or _achar_col(campos, "numero", "cota")
                         or _achar_col(campos, "total", "cota") or _achar_col(campos, "quantidade", "cota"))
            for ln in linhas:
                cnpj = ln.get(col_cnpj)
                if not cnpj:
                    continue
                data = ln.get(col_data) if col_data else None
                a = reg.get(cnpj, {})
                if col_nome and ln.get(col_nome):
                    a["nome"] = ln.get(col_nome)
                mais_novo = (not a.get("data")) or (_dkey(data) >= _dkey(a.get("data")))
                if mais_novo:
                    if data:
                        a["data"] = data
                    if col_pl:
                        v = _num(ln.get(col_pl))
                        if v is not None:
                            a["patrimonio"] = v
                    if col_vp:
                        v = _num(ln.get(col_vp))
                        if v is not None:
                            a["vp_direto"] = v
                    if col_cotas:
                        v = _num(ln.get(col_cotas))
                        if v:
                            a["cotas"] = v
                reg[cnpj] = a
        escrever_json(os.path.join(OUTPUT_DIR, "cvm_colunas.json"), debug)
        saida = []
        for cnpj, a in reg.items():
            pl, cotas, vpd = a.get("patrimonio"), a.get("cotas"), a.get("vp_direto")
            vp = vpd if (vpd and vpd > 0) else ((pl / cotas) if (pl and cotas) else None)
            saida.append({
                "cnpj": cnpj, "fundo": a.get("nome"), "data_ref": a.get("data"),
                "patrimonio_liquido": pl, "num_cotas": cotas,
                "vp_cota": round(vp, 4) if vp else None,
            })
        return saida

    def _baixar(self, ano):
        r = requests.get(f"{CVM_INF}/inf_mensal_fii_{ano}.zip", headers=HEADERS, timeout=90)
        if r.status_code == 200 and r.content[:2] == b"PK":
            return r.content
        return None


class CVMDividendos(Source):
    name = "dividendos"
    output = "dividendos.json"

    def collect(self):
        return []


FONTES = [NoticiasRSS(), FNETFatosRelevantes(), CVMFiiPVP(), CVMDividendos()]


def run(sources):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    indice = {"gerado_em": agora_iso(), "fontes": {}}
    for src in sources:
        info = {"arquivo": src.output, "status": "ok", "itens": 0}
        try:
            registros = src.collect()
            escrever_json(os.path.join(OUTPUT_DIR, src.output), {
                "fonte": src.name, "gerado_em": agora_iso(),
                "itens": len(registros), "dados": registros,
            })
            info["itens"] = len(registros)
        except Exception as e:
            info["status"] = "erro"
            info["erro"] = str(e)
            traceback.print_exc()
        indice["fontes"][src.name] = info
        print(f"[{src.name}] {info['status']} - {info['itens']} itens")
    escrever_json(os.path.join(OUTPUT_DIR, "index.json"), indice)
    return indice


if __name__ == "__main__":
    run(FONTES)
