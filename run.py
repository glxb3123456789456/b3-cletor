"""Coletor B3 - versao enxuta (noticias + P/VP de FII da CVM)."""
import re, os, io, csv, json, time, zipfile, datetime, traceback
import requests
try:
    import feedparser
except Exception:
    feedparser = None

OUTPUT_DIR = "output"
TICKER_RE = re.compile(r"\b([A-Z]{4}\d{1,2})\b")
UA = {"User-Agent": "Mozilla/5.0"}

FEEDS = [
    ("InfoMoney - Mercados", "https://www.infomoney.com.br/mercados/feed/"),
    ("InfoMoney - FIIs", "https://www.infomoney.com.br/onde-investir/fundos-imobiliarios/feed/"),
    ("Money Times", "https://www.moneytimes.com.br/feed/"),
    ("Brazil Journal", "https://braziljournal.com/feed/"),
    ("Valor Investe", "https://valorinveste.globo.com/rss/valorinveste/"),
    ("Suno Noticias", "https://www.suno.com.br/noticias/feed/"),
    ("InvestNews", "https://investnews.com.br/feed/"),
]
CVM_INF = "https://dados.cvm.gov.br/dados/FII/DOC/INF_MENSAL/DADOS"


def escrever(nome, dados):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, nome), "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)


def tickers(t):
    return sorted(set(TICKER_RE.findall((t or "").upper())))


def num(v):
    if v is None:
        return None
    v = str(v).strip()
    if not v:
        return None
    if "," in v and "." in v:
        v = v.replace(".", "").replace(",", ".")
    elif "," in v:
        v = v.replace(",", ".")
    try:
        return float(v)
    except ValueError:
        return None


def achar(campos, *chaves):
    for c in campos:
        cl = c.lower()
        if all(k in cl for k in chaves):
            return c
    return None


def coletar_noticias():
    if feedparser is None:
        raise RuntimeError("feedparser nao instalado")
    itens = []
    for fonte, url in FEEDS:
        try:
            d = feedparser.parse(url)
        except Exception:
            continue
        for e in d.entries[:40]:
            titulo = getattr(e, "title", "") or ""
            resumo = re.sub(r"<[^>]+>", "", getattr(e, "summary", "") or "").strip()
            pub = None
            pp = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
            if pp:
                pub = datetime.datetime.fromtimestamp(time.mktime(pp), datetime.timezone.utc).isoformat()
            itens.append({"fonte": fonte, "titulo": titulo, "url": getattr(e, "link", "") or "",
                          "publicado": pub, "tickers": tickers(titulo + " " + resumo), "resumo": resumo[:280]})
    itens.sort(key=lambda x: x["publicado"] or "", reverse=True)
    return itens


def coletar_fii_pvp():
    ano = datetime.date.today().year
    conteudo = _baixar(ano) or _baixar(ano - 1)
    if conteudo is None:
        raise RuntimeError("nao baixou informe CVM")
    z = zipfile.ZipFile(io.BytesIO(conteudo))
    reg = {}
    for nome in z.namelist():
        raw = z.read(nome)
        try:
            texto = raw.decode("latin-1")
        except Exception:
            texto = raw.decode("utf-8", "ignore")
        linhas = list(csv.DictReader(io.StringIO(texto), delimiter=";"))
        if not linhas:
            continue
        campos = list(linhas[0].keys())
        c_cnpj = achar(campos, "cnpj")
        if not c_cnpj:
            continue
        c_nome = achar(campos, "nome", "fund") or achar(campos, "denomin") or achar(campos, "nome")
        c_cotas = achar(campos, "cotas", "emitid") or achar(campos, "quantidade", "cota")
        c_pl = None
        for c in campos:
            cl = c.lower()
            if "patrim" in cl and "liqui" in cl and "cota" not in cl:
                c_pl = c
                break
        for ln in linhas:
            cnpj = ln.get(c_cnpj)
            if not cnpj:
                continue
            a = reg.get(cnpj, {})
            if c_nome and ln.get(c_nome):
                a["nome"] = ln.get(c_nome)
            if c_pl:
                v = num(ln.get(c_pl))
                if v is not None:
                    a["pl"] = v
            if c_cotas:
                v = num(ln.get(c_cotas))
                if v:
                    a["cotas"] = v
            reg[cnpj] = a
    saida = []
    for cnpj, a in reg.items():
        pl, cotas = a.get("pl"), a.get("cotas")
        vp = (pl / cotas) if (pl and cotas) else None
        saida.append({"cnpj": cnpj, "fundo": a.get("nome"),
                      "patrimonio_liquido": pl, "num_cotas": cotas,
                      "vp_cota": round(vp, 4) if vp else None})
    return saida


def _baixar(ano):
    r = requests.get(f"{CVM_INF}/inf_mensal_fii_{ano}.zip", headers=UA, timeout=90)
    if r.status_code == 200 and r.content[:2] == b"PK":
        return r.content
    return None


FONTES = [("noticias", "noticias.json", coletar_noticias),
          ("fii_pvp", "fii_pvp.json", coletar_fii_pvp)]


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    idx = {"gerado_em": datetime.datetime.now(datetime.timezone.utc).isoformat(), "fontes": {}}
    for nome, arq, fn in FONTES:
        info = {"arquivo": arq, "status": "ok", "itens": 0}
        try:
            dados = fn()
            escrever(arq, {"fonte": nome, "itens": len(dados), "dados": dados})
            info["itens"] = len(dados)
        except Exception as e:
            info["status"] = "erro"
            info["erro"] = str(e)
            traceback.print_exc()
        idx["fontes"][nome] = info
        print(nome, info["status"], info["itens"])
    escrever("index.json", idx)


if __name__ == "__main__":
    main()
