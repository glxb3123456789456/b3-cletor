# Coletor B3

Coletor de dados e notícias da B3 que roda fora do navegador (num agendador do
GitHub), busca fontes que bloqueiam CORS (CVM, FNET) ou que precisam de
processamento, e publica arquivos JSON prontos para o dashboard consumir.

A ideia central: **cada fonte é um módulo isolado**. Você agrega uma fonte nova
sem tocar no resto do sistema.

## Por que ele existe

O dashboard roda no navegador e só consegue chamar APIs que liberam CORS (brapi,
mfinance, AwesomeAPI, Banco Central). CVM e FNET não liberam - e é onde estão o
P/VP dos FIIs e os fatos relevantes. Um script no servidor não tem essa trava:
ele busca, limpa e republica os dados com CORS aberto.

## Arquitetura

```
b3-coletor/
  run.py                      ponto de entrada (roda todas as fontes)
  requirements.txt
  .github/workflows/coletor.yml   agendador (3x/dia) que roda e publica os JSONs
  collector/
    core.py                   classe base Source + runner + utilidades
    registry.py               LISTA das fontes ativas (edite aqui)
    sources/
      noticias_rss.py         notícias (InfoMoney, Valor, Brazil Journal, ...)
      fnet.py                 fatos relevantes / comunicados (FNET B3)
      cvm_fii_pvp.py          patrimônio e VP por cota de FII (informe CVM)
      cvm_dividendos.py       rendimentos/dividendos (em construção)
  output/                     JSONs gerados e publicados
    noticias.json
    eventos.json
    fii_pvp.json
    dividendos.json
    index.json                metadados: última atualização e status por fonte
```

## Como publicar (passo a passo)

1. Crie um repositório no GitHub (pode ser privado) e suba estes arquivos.
2. Em **Settings > Actions > General**, garanta que os workflows têm permissão de
   escrita (Read and write permissions).
3. O workflow `coletor-b3` roda sozinho 3x por dia e também pelo botão **Run
   workflow** (aba Actions). A cada execução ele regrava `output/*.json` e faz
   commit.
4. Para o dashboard ler os JSONs com CORS liberado, use uma destas opções:
   - **raw.githubusercontent.com** (mais simples): a URL
     `https://raw.githubusercontent.com/SEU_USUARIO/SEU_REPO/main/output/noticias.json`
     já responde com CORS aberto. Requer repo público.
   - **GitHub Pages**: ative Pages apontando para a branch/pasta e sirva o
     `output/` (funciona com repo público).
   - Repo privado: exponha via um proxy simples (Cloudflare Worker) ou torne
     apenas a pasta output pública com Pages.

## Como o dashboard usa

Cada JSON tem o formato:

```json
{ "fonte": "noticias", "gerado_em": "...", "itens": 123, "dados": [ ... ] }
```

Esquema dos registros:

- **noticias.json**: `{fonte, titulo, url, publicado, tickers:[], resumo}`
- **eventos.json**: `{id, fundo, cnpj, categoria, tipo, data_entrega, tickers:[], url}`
- **fii_pvp.json**: `{cnpj, fundo, data_ref, patrimonio_liquido, num_cotas, vp_cota}`
  O dashboard calcula **P/VP = preço_atual / vp_cota**.
- **dividendos.json**: (em construção)

No dashboard, basta um `fetch()` nessas URLs e cruzar por `ticker` (ou `cnpj`,
no caso do P/VP - ver "de-para" abaixo).

## Como adicionar uma fonte nova

1. Crie `collector/sources/minha_fonte.py`:

   ```python
   from ..core import Source, extrair_tickers

   class MinhaFonte(Source):
       name = "minha_fonte"
       output = "minha_fonte.json"
       def collect(self):
           # busca e devolve uma lista de dicts normalizados
           return [ {"titulo": "...", "tickers": [...]} ]
   ```

2. Registre em `collector/registry.py` (import + adicione à lista `FONTES`).

Pronto. O runner e o agendador já cuidam de executar, gravar o JSON e registrar
o status.

## Notas de primeira execução (importante)

- **FNET**: os nomes dos campos do JSON e o parâmetro `tipoFundo` podem variar.
  Rode uma vez, confira `output/eventos.json` e, se algum campo vier vazio,
  ajuste os `.get()` em `sources/fnet.py`.
- **CVM P/VP**: os nomes das colunas dos CSVs da CVM mudam de tempos em tempos.
  O código detecta colunas por palavra-chave; se `vp_cota` vier nulo, ajuste as
  chaves em `_achar_col()` dentro de `sources/cvm_fii_pvp.py`.
- **De-para CNPJ ↔ ticker**: CVM e FNET identificam fundos por CNPJ/nome, não por
  ticker. Para cruzar com o dashboard (que usa ticker), monte uma tabela de-para
  (o cadastro `cad_fii.csv` da CVM + a lista de FIIs da mfinance, que tem ticker e
  nome, resolvem a maioria por casamento de nome). Isso está no Roadmap.

## Roadmap

- Tabela de-para CNPJ ↔ ticker (cruzando cad_fii.csv da CVM com a lista mfinance).
- `cvm_dividendos`: ler documentos de "Rendimentos" da FNET e montar a série de
  proventos por ativo.
- Feed unificado (notícias + eventos) ordenado por data, com filtro por ticker.
- Mais portais de notícias e, se quiser, fontes de research/relatórios.
