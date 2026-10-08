"""Atualiza `agrofit/produtos.parquet` e `agrofit/meta.json` a partir do CSV aberto do MAPA.

Fonte: dados abertos do MAPA, dataset "Sistema de Agrotoxicos Fitossanitarios -
Agrofit", recurso "Produto Formulado". O CSV bruto repete o produto uma vez por
combinacao cultura x praga (~390 MB, ~280 mil linhas); aqui ele vira UM registro
por produto (NR_REGISTRO + MARCA_COMERCIAL), com culturas e alvos agregados — o
formato que o ORCreator le (contrato em README.md).

Uso:
    python scripts/atualiza_agrofit.py                       # baixa e atualiza
    python scripts/atualiza_agrofit.py --so-se-mes-pendente  # pula se o mes ja foi verificado
    python scripts/atualiza_agrofit.py --csv agrofit.csv     # usa um CSV ja baixado
    python scripts/atualiza_agrofit.py --forcar              # aceita queda grande de produtos

Codigos de saida: 0 ok (inclui "nada a fazer" e falha tolerada), 1 falha de
download/leitura, 2 CSV fora do esperado (colunas/volume), 3 queda suspeita.
Em qualquer falha os arquivos publicados anteriormente NAO sao tocados.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import requests

RAIZ = Path(__file__).resolve().parent.parent
PASTA_PADRAO = RAIZ / "agrofit"

URL_FONTE = "https://dados.agricultura.gov.br/dataset/sistema-de-agrotoxicos-fitossanitarios-agrofit"
# Link direto do recurso "Produto Formulado". O orgao pode troca-lo sem aviso:
# sobrescreva pela *variable* AGROFIT_CSV_URL do repositorio, sem mexer no codigo.
URL_CSV_PADRAO = (
    "https://dados.agricultura.gov.br/dataset/6c913699-e82e-4da3-a0a1-fb6c431e367f/"
    "resource/d30b30d7-e256-484e-9ab8-cd40974e1238/download/agrofitprodutosformulados.csv"
)

SCHEMA_VERSAO = 1
LIMITE_LISTA = 60  # culturas/alvos guardados por produto (conferencia, nao bula)
MIN_PRODUTOS = 1000  # abaixo disso o CSV esta quebrado/truncado
QUEDA_MAXIMA = 0.20  # queda > 20% frente a versao publicada exige --forcar
TENTATIVAS = 3
ESPERA_ENTRE_TENTATIVAS_S = (30, 90)

# Campos que descrevem o PRODUTO (constantes em todas as linhas dele).
CAMPOS_PRODUTO = {
    "MARCA_COMERCIAL": "marca_comercial",
    "FORMULACAO": "formulacao",
    "INGREDIENTE_ATIVO": "ingrediente_ativo",
    "TITULAR_DE_REGISTRO": "titular_registro",
    "CLASSE": "classe",
    "MODO_DE_ACAO": "modo_de_acao",
    "CLASSE_TOXICOLOGICA": "classe_toxicologica",
    "CLASSE_AMBIENTAL": "classe_ambiental",
    "ORGANICOS": "organico",
    "SITUACAO": "situacao",
}
COLUNAS_OBRIGATORIAS = ["NR_REGISTRO", "CULTURA", "PRAGA_NOME_CIENTIFICO", *CAMPOS_PRODUTO]
COLUNAS_TEXTO = ["nr_registro", *CAMPOS_PRODUTO.values()]

SCHEMA_PARQUET = pa.schema(
    [(c, pa.string()) for c in COLUNAS_TEXTO]
    + [("culturas", pa.list_(pa.string())), ("alvos", pa.list_(pa.string()))]
)


class ErroAgrofit(Exception):
    """Falha esperada; `codigo` e' o codigo de saida do processo."""

    def __init__(self, mensagem: str, codigo: int = 1):
        super().__init__(mensagem)
        self.codigo = codigo


# ------------------------------------------------------------------- download


def baixa_csv(url: str, destino: Path, dormir=time.sleep) -> None:
    """Baixa em streaming (o arquivo e' grande demais para a memoria) com tentativas."""
    ultimo = None
    for n in range(1, TENTATIVAS + 1):
        try:
            print(f"Baixando ({n}/{TENTATIVAS}): {url}")
            cab = {"User-Agent": "Mozilla/5.0 (compatible; orcreator-agrofit/1.0)", "Accept": "text/csv,*/*"}
            with requests.get(url, stream=True, timeout=(30, 120), headers=cab) as r:
                r.raise_for_status()
                with open(destino, "wb") as f:
                    for pedaco in r.iter_content(chunk_size=1024 * 1024):
                        if pedaco:
                            f.write(pedaco)
            print(f"Baixado: {destino.stat().st_size / 1048576:.0f} MB")
            return
        except (requests.RequestException, OSError) as erro:
            ultimo = erro
            print(f"  falhou: {erro}", file=sys.stderr)
            if n < TENTATIVAS:
                dormir(ESPERA_ENTRE_TENTATIVAS_S[min(n - 1, len(ESPERA_ENTRE_TENTATIVAS_S) - 1)])
    raise ErroAgrofit(f"nao foi possivel baixar o CSV apos {TENTATIVAS} tentativas ({ultimo})", 1)


# -------------------------------------------------------------------- leitura


def _limpa(valor):
    if valor is None:
        return None
    texto = " ".join(str(valor).split())
    return texto or None


def _destila_com(caminho: Path, codificacao: str, limite_lista: int):
    csv.field_size_limit(10_000_000)  # EMPRESA_PAIS_TIPO passa de 100 kB numa celula
    produtos: dict[str, dict] = {}
    linhas = 0
    with open(caminho, encoding=codificacao, newline="") as f:
        leitor = csv.DictReader(f, delimiter=";")
        faltam = [c for c in COLUNAS_OBRIGATORIAS if c not in (leitor.fieldnames or [])]
        if faltam:
            raise ErroAgrofit(f"CSV sem as colunas esperadas: {', '.join(faltam)}", 2)
        for linha in leitor:
            linhas += 1
            registro = _limpa(linha.get("NR_REGISTRO"))
            marca = _limpa(linha.get("MARCA_COMERCIAL"))
            if not registro or not marca:
                continue
            chave = f"{registro}|{marca}"
            p = produtos.get(chave)
            if p is None:
                p = {"nr_registro": registro, "culturas": set(), "alvos": set()}
                for origem, destino in CAMPOS_PRODUTO.items():
                    p[destino] = _limpa(linha.get(origem))
                produtos[chave] = p
            cultura = _limpa(linha.get("CULTURA"))
            if cultura and len(p["culturas"]) < limite_lista:
                p["culturas"].add(cultura)
            alvo = _limpa(linha.get("PRAGA_NOME_CIENTIFICO"))
            if alvo and len(p["alvos"]) < limite_lista:
                p["alvos"].add(alvo)
    lista = []
    for p in produtos.values():
        p["culturas"] = sorted(p["culturas"])
        p["alvos"] = sorted(p["alvos"])
        lista.append(p)
    # ordem estavel: o mesmo conteudo gera sempre o mesmo hash/arquivo
    lista.sort(key=lambda p: (p["nr_registro"], p["marca_comercial"] or ""))
    return lista, linhas


def destila(caminho: Path, limite_lista: int = LIMITE_LISTA):
    """CSV do MAPA -> (lista de produtos, linhas lidas). UTF-8 primeiro; cp1252 se nao for UTF-8."""
    try:
        return _destila_com(Path(caminho), "utf-8-sig", limite_lista)
    except UnicodeDecodeError:
        print("CSV nao esta em UTF-8; relendo como cp1252.", file=sys.stderr)
        return _destila_com(Path(caminho), "cp1252", limite_lista)


# ------------------------------------------------------------------- gravacao


def hash_conteudo(produtos: list[dict]) -> str:
    texto = json.dumps(produtos, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(texto.encode("utf-8")).hexdigest()


def grava_parquet(produtos: list[dict], destino: Path) -> None:
    colunas = {c: [p.get(c) for p in produtos] for c in COLUNAS_TEXTO}
    colunas["culturas"] = [list(p.get("culturas") or []) for p in produtos]
    colunas["alvos"] = [list(p.get("alvos") or []) for p in produtos]
    tabela = pa.table(colunas, schema=SCHEMA_PARQUET)
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(destino.suffix + ".tmp")
    pq.write_table(tabela, tmp, compression="snappy")  # snappy: o leitor do app (hyparquet) le sem extras
    os.replace(tmp, destino)


def grava_json(conteudo: dict, destino: Path) -> None:
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.with_suffix(destino.suffix + ".tmp")
    tmp.write_text(json.dumps(conteudo, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, destino)


def le_meta(pasta: Path) -> dict | None:
    try:
        return json.loads((pasta / "meta.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def agora_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def mes_ja_verificado(meta: dict | None, agora: str) -> bool:
    """True se a ultima verificacao bem-sucedida caiu no mesmo mes (UTC) de `agora`."""
    ultima = (meta or {}).get("verificado_em") or ""
    return bool(ultima) and ultima[:7] == agora[:7]


def aplica(produtos: list[dict], linhas: int, pasta: Path, forcar: bool, url: str, agora: str) -> str:
    """Valida e publica. Devolve 'atualizado' | 'sem_mudanca'. Nao toca nada se levantar ErroAgrofit."""
    if len(produtos) < MIN_PRODUTOS:
        raise ErroAgrofit(f"apenas {len(produtos)} produtos no CSV (minimo esperado {MIN_PRODUTOS}); download truncado?", 2)
    anterior = le_meta(pasta)
    n_ant = int((anterior or {}).get("produtos") or 0)
    if n_ant and len(produtos) < n_ant * (1 - QUEDA_MAXIMA) and not forcar:
        raise ErroAgrofit(
            f"queda suspeita: {len(produtos)} produtos contra {n_ant} publicados (>{QUEDA_MAXIMA:.0%}). "
            "Confira o CSV de origem; se a queda e' real, rode com --forcar.",
            3,
        )
    h = hash_conteudo(produtos)
    mudou = (anterior or {}).get("hash_conteudo") != h or not (pasta / "produtos.parquet").exists()
    if mudou:
        grava_parquet(produtos, pasta / "produtos.parquet")
    meta = {
        "schema_versao": SCHEMA_VERSAO,
        # data em que o CONTEUDO mudou; o app mostra esta no indicador do topo
        "atualizado_em": agora if mudou else (anterior or {}).get("atualizado_em", agora),
        # ultima conferencia bem-sucedida (mesmo sem mudanca) — alimenta a regra "1x por mes"
        "verificado_em": agora,
        "produtos": len(produtos),
        "linhas_no_csv_original": linhas,
        "fonte": URL_FONTE,
        "url_csv": url,
        "hash_conteudo": h,
    }
    grava_json(meta, pasta / "meta.json")
    return "atualizado" if mudou else "sem_mudanca"


# ----------------------------------------------------------------------- main


def executa(args, agora: str | None = None) -> int:
    agora = agora or agora_utc()
    pasta = Path(args.saida)
    url = args.url or os.environ.get("AGROFIT_CSV_URL") or URL_CSV_PADRAO

    if args.so_se_mes_pendente and mes_ja_verificado(le_meta(pasta), agora):
        print(f"Mes {agora[:7]} ja verificado em {le_meta(pasta)['verificado_em']}; nada a fazer.")
        return 0

    try:
        if args.csv:
            caminho = Path(args.csv)
            if not caminho.exists():
                raise ErroAgrofit(f"CSV nao encontrado: {caminho}", 1)
            produtos, linhas = destila(caminho)
        else:
            fd, tmp = tempfile.mkstemp(suffix=".csv")
            os.close(fd)  # Windows: handle aberto impede apagar depois
            caminho = Path(tmp)
            try:
                baixa_csv(url, caminho)
                produtos, linhas = destila(caminho)
            finally:
                caminho.unlink(missing_ok=True)  # o CSV bruto nunca fica no repositorio
        resultado = aplica(produtos, linhas, pasta, args.forcar, url, agora)
    except ErroAgrofit as erro:
        if args.tolerar_falha:
            print(f"::warning::Agrofit nao atualizado hoje: {erro}")
            return 0
        print(f"::error::{erro}", file=sys.stderr)
        return erro.codigo
    except (csv.Error, UnicodeDecodeError, OSError) as erro:
        msg = f"CSV ilegivel: {erro}"
        if args.tolerar_falha:
            print(f"::warning::Agrofit nao atualizado hoje: {msg}")
            return 0
        print(f"::error::{msg}", file=sys.stderr)
        return 1

    print(f"{resultado}: {len(produtos):,} produtos, {linhas:,} linhas no CSV.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--csv", help="usa este CSV local em vez de baixar")
    ap.add_argument("--url", help="URL do CSV (padrao: variavel AGROFIT_CSV_URL, depois o link do MAPA)")
    ap.add_argument("--saida", default=str(PASTA_PADRAO), help="pasta de saida (padrao: agrofit/)")
    ap.add_argument("--so-se-mes-pendente", action="store_true", help="pula se o mes (UTC) ja foi verificado com sucesso")
    ap.add_argument("--forcar", action="store_true", help="aceita queda grande no numero de produtos")
    ap.add_argument("--tolerar-falha", action="store_true", help="falha vira aviso e saida 0 (tentativas intermediarias dos dias 1-3)")
    return executa(ap.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
