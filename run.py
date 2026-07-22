"""Coletor B3 - arquivo unico v3 (parser decimal corrigido)."""
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
