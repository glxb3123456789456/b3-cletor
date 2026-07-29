"""Coletor B3 - noticias + P/VP de FII (CVM) + fatos relevantes crus (CVM e FNET).

A interpretacao por IA (Gemini) NAO roda mais aqui: este coletor so BAIXA e publica
os fatos crus (com o texto ja extraido dos PDFs). Quem chama o Gemini e o dashboard,
sob demanda, quando o usuario aperta o botao 'Interpretar com IA'. Assim nenhuma
rodada automatica gasta tokens de IA - o coletor faz so o trabalho gratuito (download
e extracao de texto), 3x/dia.
"""
import re, os, io, csv, json, time, zipfile, datetime, traceback
import requests
try:
    import feedparser
except Exception:
    feedparser = None

OUTPUT_DIR = "output"
TICKER_RE = re.compile(r"\b([A-Z]{4}\d{1,2})\b")
UA = {"User-Agent": "Mozilla/5.0"}

FATOS_DIAS = 20         # janela: fatos relevantes dos ultimos N dias (ampla p/ compensar atraso do dado aberto da CVM)
FATOS_MAX = 120         # teto de documentos por rodada (controla tempo de download)

FEEDS = [
    ("InfoMoney - Mercados", "https://www.infomoney.com.br/mercados/feed/"),
    ("InfoMoney - FIIs", "https://www.infomoney.com.br/onde-investir/fundos-imobiliarios/feed/"),
    ("Money Times", "https://www.moneytimes.com.br/feed/"),
    ("Brazil Journal", "https://braziljournal.com/feed/"),
    ("Valor Investe", "https://valorinveste.globo.com/rss/valorinveste/"),
    ("Suno Noticias", "https://www.suno.com.br/noticias/feed/"),
    ("InvestNews", "https://investnews.com.br/feed/"),
]
CVM_FII = "https://dados.cvm.gov.br/dados/FII/DOC/INF_MENSAL/DADOS"
CVM_IPE = "https://dados.cvm.gov.br/dados/CIA_ABERTA/DOC/IPE/DADOS"


def escrever(nome, dados):
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(os.path.join(OUTPUT_DIR, nome), "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=1)


def ler_output(nome):
    """Le um JSON ja publicado em output/ (a Action faz checkout do repo antes de rodar)."""
    p = os.path.join(OUTPUT_DIR, nome)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None
    return None


def _fato_key(d):
    """Chave estavel de um fato relevante (mesma logica do dashboard)."""
    return d.get("link") or f"{d.get('empresa','')}|{d.get('data','')}|{d.get('tipo_evento','')}"


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


def _iso(s):
    """Normaliza data para AAAA-MM-DD (aceita AAAA-MM-DD... ou DD/MM/AAAA)."""
    if not s:
        return ""
    s = str(s).strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return m.group(0)
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return s[:10]


# ============================ NOTICIAS ============================
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


# ============================ P/VP FII ============================
def coletar_fii_pvp():
    ano = datetime.date.today().year
    conteudo = _baixar_zip(f"{CVM_FII}/inf_mensal_fii_{ano}.zip") or _baixar_zip(f"{CVM_FII}/inf_mensal_fii_{ano-1}.zip")
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
        saida.append({"cnpj": cnpj, "fundo": a.get("nome"), "patrimonio_liquido": pl,
                      "num_cotas": cotas, "vp_cota": round(vp, 4) if vp else None})
    return saida


def _baixar_zip(url):
    r = requests.get(url, headers=UA, timeout=90)
    if r.status_code == 200 and r.content[:2] == b"PK":
        return r.content
    return None


# ==================== FATOS RELEVANTES (CVM IPE, texto cru) ====================
def _baixar_ipe(ano):
    for url in (f"{CVM_IPE}/ipe_cia_aberta_{ano}.csv", f"{CVM_IPE}/ipe_cia_aberta_{ano}.zip"):
        try:
            r = requests.get(url, headers=UA, timeout=90)
        except Exception:
            continue
        if r.status_code != 200 or not r.content:
            continue
        if r.content[:2] == b"PK":
            z = zipfile.ZipFile(io.BytesIO(r.content))
            raw = z.read(z.namelist()[0])
        else:
            raw = r.content
        try:
            texto = raw.decode("latin-1")
        except Exception:
            texto = raw.decode("utf-8", "ignore")
        return list(csv.DictReader(io.StringIO(texto), delimiter=";"))
    return None


def _texto_pdf(link):
    if not link:
        return ""
    try:
        r = requests.get(link, headers=UA, timeout=60, allow_redirects=True)
        if r.status_code != 200 or not r.content or r.content[:4] != b"%PDF":
            return ""
        from pypdf import PdfReader
        rd = PdfReader(io.BytesIO(r.content))
        txt = " ".join((p.extract_text() or "") for p in rd.pages[:6])
        return re.sub(r"\s+", " ", txt).strip()[:5000]
    except Exception:
        return ""


def _carregar_textos(arq):
    """Reaproveita o texto ja extraido na rodada anterior (evita rebaixar PDFs a cada run)."""
    prev = ler_output(arq)
    m = {}
    if prev and isinstance(prev, dict):
        for d in (prev.get("dados") or []):
            if d.get("texto"):
                m[_fato_key(d)] = d["texto"]
    return m


def _marcar_historico(items, visto_nome):
    """Marca visto_em (primeira vez que cada item apareceu) para o selo 'novo' no dashboard."""
    hoje = datetime.date.today().isoformat()
    visto = ler_output(visto_nome) or {}
    for it in items:
        k = _fato_key(it)
        prim = (visto.get(k) or {}).get("primeiro") or hoje
        visto[k] = {"primeiro": prim, "ultimo": hoje}
        it["visto_em"] = prim
    corte = (datetime.date.today() - datetime.timedelta(days=60)).isoformat()
    visto = {k: v for k, v in visto.items() if (v.get("ultimo") or "") >= corte}
    escrever(visto_nome, visto)
    return items


def coletar_fatos_raw():
    """Fatos relevantes de companhias abertas (acoes/units) - CVM IPE. Sem IA: so texto cru."""
    ano = datetime.date.today().year
    linhas = _baixar_ipe(ano) or _baixar_ipe(ano - 1)
    if not linhas:
        raise RuntimeError("nao baixou IPE da CVM")
    campos = list(linhas[0].keys())
    escrever("ipe_colunas.json", campos)  # debug: nomes reais das colunas
    c_cat = achar(campos, "categoria")
    c_emp = achar(campos, "nome", "compan") or achar(campos, "compan") or achar(campos, "denomin")
    c_cnpj = achar(campos, "cnpj")
    c_data = achar(campos, "data", "entrega") or achar(campos, "data", "refer")
    c_ass = achar(campos, "assunto")
    c_link = achar(campos, "link", "download") or achar(campos, "link")
    limite = (datetime.date.today() - datetime.timedelta(days=FATOS_DIAS)).isoformat()
    cats = {}
    fatos_all = []
    for ln in linhas:
        cat = (ln.get(c_cat) or "") if c_cat else ""
        cats[cat] = cats.get(cat, 0) + 1
        if "fato relevante" not in cat.lower():
            continue
        data = _iso(ln.get(c_data)) if c_data else ""
        fatos_all.append({
            "empresa": ln.get(c_emp) if c_emp else "",
            "cnpj": ln.get(c_cnpj) if c_cnpj else "",
            "data": data,
            "assunto": ln.get(c_ass) if c_ass else "",
            "link": ln.get(c_link) if c_link else "",
            "categoria": cat,
        })
    _datas = sorted([d["data"] for d in fatos_all if d["data"]])
    escrever("fatos_debug.json", {
        "total_linhas": len(linhas),
        "com_fato_relevante": len(fatos_all),
        "data_min": _datas[0] if _datas else None,
        "data_max": _datas[-1] if _datas else None,
        "janela_desde": limite,
        "amostra_categorias": sorted(cats.keys())[:30],
    })
    docs = [d for d in fatos_all if (not d["data"] or d["data"] >= limite)]
    docs.sort(key=lambda d: d["data"], reverse=True)
    if len(docs) > FATOS_MAX:
        print(f"[fatos] {len(docs)} fatos na janela; limitando a {FATOS_MAX}")
        docs = docs[:FATOS_MAX]
    cache = _carregar_textos("fatos_raw.json")
    for d in docs:
        d["texto"] = cache.get(_fato_key(d)) or _texto_pdf(d["link"])
    return _marcar_historico(docs, "fatos_visto.json")


# ============ FATOS/COMUNICADOS DE FUNDOS via FNET (FII/Fiagro/FI-Infra e, à parte, FIDC) ==
FNET_BASE = "https://fnet.bmfbovespa.com.br/fnet/publico"
FNET_URL = FNET_BASE + "/pesquisarGerenciadorDocumentosDados"
FNET_DIAS = 12          # janela curta (FNET tem volume alto de docs)
FNET_PAGINAS = 40       # teto de paginas (100 docs cada) por rodada
FNET_MAX = 90           # teto de docs por balde (fundos e fidc separados)
FNET_CATEGORIAS = ("fato relevante", "comunicado ao mercado")  # filtra pelo texto da categoria
_FNET_CACHE = {"docs": None}  # o FNET so e buscado uma vez por rodada (os dois baldes reusam)


def _eh_fidc(d):
    """FIDC (direitos creditorios) nao e negociado como FII/Fiagro - vai para pagina propria."""
    nome = (d.get("empresa") or "").upper()
    return ("DIREITOS CRED" in nome) or ("FIDC" in nome) or ("FIDIC" in nome)


def _fnet_headers():
    return {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": FNET_BASE + "/abrirGerenciadorDocumentosCVM",
    }


def _fnet_get(rec, *keys):
    if isinstance(rec, dict):
        for k in keys:
            if k in rec and rec[k] not in (None, ""):
                return rec[k]
    return None


def _fnet_pagina(start, length, data_ini, data_fim, tentativas=2):
    params = {"d": 1, "s": start, "l": length,
              "o[0][dataEntrega]": "desc",
              "dataInicial": data_ini, "dataFinal": data_fim}
    erro = None
    for _ in range(tentativas):
        try:
            r = requests.get(FNET_URL, params=params, headers=_fnet_headers(), timeout=60)
        except Exception as e:
            erro = f"excecao: {e}"; time.sleep(2); continue
        if r.status_code != 200:
            erro = f"HTTP {r.status_code}: {r.text[:180]}"; time.sleep(2); continue
        try:
            return r.json(), None
        except Exception as e:
            erro = f"json invalido: {e} / {r.text[:160]}"; time.sleep(1); continue
    return None, erro


def _fnet_texto(doc_id):
    for url in (f"{FNET_BASE}/exibirDocumento?id={doc_id}&cvm=true",
                f"{FNET_BASE}/exibirDocumento?id={doc_id}"):
        try:
            r = requests.get(url, headers=_fnet_headers(), timeout=60)
        except Exception:
            continue
        if r.status_code != 200 or not r.content:
            continue
        c = r.content
        if c[:4] != b"%PDF":            # as vezes o FNET devolve o PDF em base64
            try:
                import base64
                dec = base64.b64decode(c, validate=False)
                if dec[:4] == b"%PDF":
                    c = dec
            except Exception:
                pass
        if c[:4] == b"%PDF":
            try:
                from pypdf import PdfReader
                rd = PdfReader(io.BytesIO(c))
                txt = " ".join((p.extract_text() or "") for p in rd.pages[:6])
                return re.sub(r"\s+", " ", txt).strip()[:5000]
            except Exception:
                continue
        try:                            # fallback: documento em HTML
            html = c.decode("utf-8", "ignore")
            txt = re.sub(r"<[^>]+>", " ", html)
            txt = re.sub(r"\s+", " ", txt).strip()
            if len(txt) > 120:
                return txt[:5000]
        except Exception:
            continue
    return ""


def _fnet_coletar_todos():
    """Busca no FNET (uma vez por rodada) os fatos relevantes e comunicados de fundos,
    extrai o texto e devolve a lista ja com FII/Fiagro/Infra e FIDC juntos. Sem IA."""
    if _FNET_CACHE.get("docs") is not None:
        return _FNET_CACHE["docs"]
    hoje = datetime.date.today()
    data_ini = (hoje - datetime.timedelta(days=FNET_DIAS)).strftime("%d/%m/%Y")
    data_fim = hoje.strftime("%d/%m/%Y")
    limite_iso = (hoje - datetime.timedelta(days=FNET_DIAS)).isoformat()
    cats, docs = {}, []
    total, erro, amostra, paginas, falhas = None, None, None, 0, 0
    parar = False
    for p in range(FNET_PAGINAS):
        j, e = _fnet_pagina(p * 100, 100, data_ini, data_fim)
        if e:
            erro = e
            falhas += 1
            if falhas >= 3:             # FNET instavel: para e fica com o que ja pegou
                break
            continue                    # pula esta pagina e tenta a proxima
        falhas = 0
        paginas += 1
        if total is None:
            total = j.get("recordsTotal") or j.get("recordsFiltered")
        linhas = j.get("data") or []
        if amostra is None and linhas:
            amostra = linhas[:2]
        if not linhas:
            break
        for rec in linhas:
            cat = _fnet_get(rec, "categoriaDocumento", "categoria") or ""
            cats[cat] = cats.get(cat, 0) + 1
            data = _iso(_fnet_get(rec, "dataEntrega", "dataReferencia") or "")
            if data and data < limite_iso:
                parar = True            # ordem desc: passou da janela
                continue
            if not any(k in cat.lower() for k in FNET_CATEGORIAS):
                continue
            doc_id = _fnet_get(rec, "id")
            docs.append({
                "empresa": _fnet_get(rec, "descricaoFundo", "nomePregao", "denominacaoSocial") or "",
                "cnpj": _fnet_get(rec, "cnpjFundo", "cnpj") or "",
                "data": data,
                "assunto": _fnet_get(rec, "tipoDocumento", "especieDocumento") or "",
                "categoria": cat,
                "pregao": _fnet_get(rec, "nomePregao", "codSegNegociacao") or "",
                "link": f"{FNET_BASE}/exibirDocumento?id={doc_id}&cvm=true" if doc_id else "",
                "_id": doc_id,
            })
        if parar:
            break
    n_fidc = sum(1 for d in docs if _eh_fidc(d))
    escrever("fnet_debug.json", {
        "recordsTotal": total, "paginas_lidas": paginas,
        "janela": [data_ini, data_fim], "docs_filtrados": len(docs),
        "docs_fundos": len(docs) - n_fidc, "docs_fidc": n_fidc,
        "amostra_categorias": sorted(cats.keys())[:40],
        "erro": erro, "amostra_registros": amostra,
    })
    docs.sort(key=lambda d: d["data"] or "", reverse=True)
    # limita cada balde separadamente para nenhum sufocar o outro
    fundos = [d for d in docs if not _eh_fidc(d)][:FNET_MAX]
    fidc = [d for d in docs if _eh_fidc(d)][:FNET_MAX]
    sel = fundos + fidc
    cache = {}
    cache.update(_carregar_textos("fatos_fundos_raw.json"))
    cache.update(_carregar_textos("fatos_fidc_raw.json"))
    for d in sel:
        d["texto"] = cache.get(_fato_key(d)) or (_fnet_texto(d["_id"]) if d.get("_id") else "")
    _FNET_CACHE["docs"] = sel
    return sel


def coletar_fatos_fundos_raw():
    """FII/Fiagro/FI-Infra (exclui FIDC) - FNET. Sem IA: so texto cru."""
    docs = [d for d in _fnet_coletar_todos() if not _eh_fidc(d)]
    return _marcar_historico(docs, "fatos_fundos_visto.json")


def coletar_fatos_fidc_raw():
    """FIDC (fundos de direitos creditorios) - FNET. Sem IA: so texto cru. Pagina propria no painel."""
    docs = [d for d in _fnet_coletar_todos() if _eh_fidc(d)]
    return _marcar_historico(docs, "fatos_fidc_visto.json")


# ============================ RUNNER ============================
# So fontes gratuitas rodam aqui (download/extracao). A IA (Gemini) roda no dashboard,
# sob demanda, lendo estes arquivos *_raw.json.
FONTES = [("noticias", "noticias.json", coletar_noticias),
          ("fii_pvp", "fii_pvp.json", coletar_fii_pvp),
          ("fatos_raw", "fatos_raw.json", coletar_fatos_raw),
          ("fatos_fundos_raw", "fatos_fundos_raw.json", coletar_fatos_fundos_raw),
          ("fatos_fidc_raw", "fatos_fidc_raw.json", coletar_fatos_fidc_raw)]


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
