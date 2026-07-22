"""Coletor B3 - versao de arquivo unico."""
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
        params = {"d": 0, "s": 0, "l": 300, "o[0][dataEntrega]": "desc", "tipoFundo": 1}
        r = requests.get(f"{FNET_BASE}/pesquisarGerenciadorDocumentosDados",
                         params=params, headers={**HEADERS, "Accept": "application/json"}, timeout=40)
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
        for nome in z.namelist():
            n = nome.lower()
            if "geral" in n or "ativo_passivo" in n:
                self._ler_pl(z, nome, reg)
            if "complemento" in n or "geral" in n:
                self._ler_cotas(z, nome, reg)
        saida = []
        for cnpj, ref in reg.items():
            pl, cotas = ref.get("patrimonio"), ref.get("cotas")
            vp = (pl / cotas) if (pl and cotas) else None
            saida.append({
                "cnpj": cnpj, "fundo": ref.get("nome"), "data_ref": ref.get("data"),
                "patrimonio_liquido": pl, "num_cotas": cotas,
                "vp_cota": round(vp, 4) if vp else None,
            })
        return saida

    def _baixar(self, ano):
        r = requests.get(f"{CVM_INF}/inf_mensal_fii_{ano}.zip", headers=HEADERS, timeout=90)
        if r.status_code == 200 and r.content[:2] == b"PK":
            return r.content
        return None

    def _ler_pl(self, z, nome, reg):
        linhas = _abrir_csv(z, nome)
        if not linhas:
            return
        campos = linhas[0].keys()
        col_cnpj = _achar_col(campos, "cnpj")
        col_data = _achar_col(campos, "data", "refer") or _achar_col(campos, "data", "compet")
        col_nome = _achar_col(campos, "denomin") or _achar_col(campos, "nome")
        col_pl = _achar_col(campos, "patrim", "liqui")
        if not (col_cnpj and col_pl):
            return
        for ln in linhas:
            cnpj = ln.get(col_cnpj)
            pl = _num(ln.get(col_pl))
            data = ln.get(col_data) if col_data else None
            if not cnpj or pl is None:
                continue
            atual = reg.get(cnpj, {})
            if not atual.get("data") or (data or "") >= atual["data"]:
                atual["data"] = data or atual.get("data")
                atual["patrimonio"] = pl
                if col_nome:
                    atual["nome"] = ln.get(col_nome)
                reg[cnpj] = atual

    def _ler_cotas(self, z, nome, reg):
        linhas = _abrir_csv(z, nome)
        if not linhas:
            return
        campos = linhas[0].keys()
        col_cnpj = _achar_col(campos, "cnpj")
        col_cotas = (_achar_col(campos, "numero", "cota") or _achar_col(campos, "quantidade", "cota")
                     or _achar_col(campos, "total", "cota"))
        if not (col_cnpj and col_cotas):
            return
        for ln in linhas:
            cnpj = ln.get(col_cnpj)
            cotas = _num(ln.get(col_cotas))
            if not cnpj or not cotas:
                continue
            atual = reg.get(cnpj, {})
            atual["cotas"] = cotas
            reg[cnpj] = atual


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
