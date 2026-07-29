"""Coletor B3 - noticias + P/VP de FII (CVM) + fatos relevantes interpretados pelo Gemini."""
import re, os, io, csv, json, time, zipfile, datetime, traceback
import requests
try:
    import feedparser
except Exception:
    feedparser = None

OUTPUT_DIR = "output"
TICKER_RE = re.compile(r"\b([A-Z]{4}\d{1,2})\b")
UA = {"User-Agent": "Mozilla/5.0"}

# ---- config Gemini (chave via secret do GitHub: GEMINI_API_KEY) ----
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-1.5-flash"
FATOS_DIAS = 3          # janela: fatos relevantes dos ultimos N dias
FATOS_MAX = 120         # teto de documentos por rodada (controla custo/tempo)
FATOS_LOTE = 12         # documentos por chamada ao Gemini
TIPOS = ["RJ/Reestruturação","M&A/Controle","Venda de Ativo","Provento Extraordinário",
         "Recompra","Resultado/Guidance","Litígio/Regulatório","Governança","Emissão/Follow-on","Outro"]

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


# ==================== FATOS RELEVANTES + GEMINI ====================
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


def _gemini_json(prompt):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_KEY}"
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.3}}
    r = requests.post(url, json=body, timeout=180)
    r.raise_for_status()
    j = r.json()
    txt = j["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(txt)


def _prompt_lote(docs):
    itens = "\n\n".join(
        f"[{i}] Empresa: {d['empresa']} (CNPJ {d['cnpj']}) | Data: {d['data']}\n"
        f"Assunto: {d.get('assunto','')}\nTexto: {d.get('texto','')[:3500]}"
        for i, d in enumerate(docs))
    tipos = ", ".join(f'"{t}"' for t in TIPOS)
    return (
        "Voce e analista senior de special situations e eventos especiais no mercado de acoes brasileiro. "
        "Abaixo estao fatos relevantes divulgados na B3. Para CADA item, avalie se ha potencial de oportunidade "
        "de investimento (distressed, evento especial, assimetria de retorno) e classifique.\n"
        "Retorne SOMENTE um JSON no formato {\"itens\":[{...}]} onde cada objeto tem:\n"
        "- idx: o numero entre colchetes do item\n"
        f"- tipo_evento: um de [{tipos}]\n"
        "- relevancia: inteiro de 1 a 5 (5 = oportunidade forte/assimetrica; 1 = burocratico/irrelevante)\n"
        "- oportunidade: true ou false\n"
        "- ticker: o codigo de negociacao na B3 se souber (ex PETR4), senao \"\"\n"
        "- tese: 2 a 3 frases em portugues explicando o que aconteceu e por que e (ou nao) uma oportunidade\n\n"
        "Fatos relevantes:\n" + itens)


def coletar_fatos():
    if not GEMINI_KEY:
        raise RuntimeError("defina o secret GEMINI_API_KEY no GitHub")
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
    docs = []
    for ln in linhas:
        cat = (ln.get(c_cat) or "") if c_cat else ""
        if "fato relevante" not in cat.lower():
            continue
        data = _iso(ln.get(c_data)) if c_data else ""
        if data and data < limite:
            continue
        docs.append({
            "empresa": ln.get(c_emp) if c_emp else "",
            "cnpj": ln.get(c_cnpj) if c_cnpj else "",
            "data": data,
            "assunto": ln.get(c_ass) if c_ass else "",
            "link": ln.get(c_link) if c_link else "",
        })
    docs.sort(key=lambda d: d["data"], reverse=True)
    if len(docs) > FATOS_MAX:
        print(f"[fatos] {len(docs)} fatos na janela; limitando a {FATOS_MAX}")
        docs = docs[:FATOS_MAX]
    for d in docs:
        d["texto"] = _texto_pdf(d["link"])
    saida = []
    for i in range(0, len(docs), FATOS_LOTE):
        lote = docs[i:i + FATOS_LOTE]
        try:
            res = _gemini_json(_prompt_lote(lote))
            for it in (res.get("itens") or []):
                idx = it.get("idx")
                if idx is None or not isinstance(idx, int) or idx < 0 or idx >= len(lote):
                    continue
                d = lote[idx]
                saida.append({
                    "empresa": d["empresa"], "cnpj": d["cnpj"], "data": d["data"], "link": d["link"],
                    "tipo_evento": it.get("tipo_evento") or "Outro",
                    "relevancia": it.get("relevancia") if isinstance(it.get("relevancia"), int) else 1,
                    "oportunidade": bool(it.get("oportunidade")),
                    "ticker": (it.get("ticker") or "").upper(),
                    "tese": it.get("tese") or "",
                })
        except Exception as e:
            print("[fatos] erro no lote:", e)
            traceback.print_exc()
        time.sleep(1)  # respeita rate limit do Gemini
    saida.sort(key=lambda x: (x.get("relevancia") or 0), reverse=True)
    return saida


# ============================ RUNNER ============================
FONTES = [("noticias", "noticias.json", coletar_noticias),
          ("fii_pvp", "fii_pvp.json", coletar_fii_pvp),
          ("fatos", "findings.json", coletar_fatos)]


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
