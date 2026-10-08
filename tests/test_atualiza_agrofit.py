"""Testes do atualizador (sem rede): python -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import atualiza_agrofit as ag  # noqa: E402

CAB = (
    "NR_REGISTRO;MARCA_COMERCIAL;FORMULACAO;INGREDIENTE_ATIVO;TITULAR_DE_REGISTRO;CLASSE;MODO_DE_ACAO;"
    "CLASSE_TOXICOLOGICA;CLASSE_AMBIENTAL;ORGANICOS;SITUACAO;CULTURA;PRAGA_NOME_CIENTIFICO"
)


def linha(nr, marca, cultura, praga, ativo="ia (100 g/L)"):
    return f"{nr};{marca};SC;{ativo};Empresa;Fungicida;Contato;III;III;NAO;TRUE;{cultura};{praga}"


def csv_com(n_produtos: int, extra: list[str] | None = None) -> str:
    linhas = [CAB]
    for i in range(n_produtos):
        linhas.append(linha(10000 + i, f"Produto {i}", "Soja", "Phakopsora pachyrhizi"))
    linhas.extend(extra or [])
    return "\r\n".join(linhas) + "\r\n"


def args(**kw):
    base = dict(csv=None, url=None, saida=None, so_se_mes_pendente=False, forcar=False, tolerar_falha=False)
    base.update(kw)
    return Namespace(**base)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.saida = self.dir / "agrofit"
        self.addCleanup(self.tmp.cleanup)

    def escreve_csv(self, texto: str, codificacao="utf-8") -> Path:
        p = self.dir / "agrofit.csv"
        p.write_bytes(texto.encode(codificacao))
        return p

    def roda(self, csv_path, agora="2026-11-01T09:00:00+00:00", **kw):
        return ag.executa(args(csv=str(csv_path), saida=str(self.saida), **kw), agora=agora)


class Destilacao(Base):
    def test_um_registro_por_produto_com_listas_agregadas(self):
        texto = csv_com(
            1200,
            [
                linha(1, "Alfa", "Soja", "Spodoptera frugiperda"),
                linha(1, "Alfa", "Milho", "Spodoptera frugiperda"),
                linha(1, "Alfa", "Soja", "Helicoverpa armigera"),
                linha(2, "Beta", "Cana", ""),
                linha("", "SemRegistro", "Soja", "x"),  # sem NR_REGISTRO: descartado
            ],
        )
        produtos, linhas = ag.destila(self.escreve_csv(texto))
        self.assertEqual(linhas, 1200 + 5)
        alfa = next(p for p in produtos if p["marca_comercial"] == "Alfa")
        self.assertEqual(alfa["culturas"], ["Milho", "Soja"])
        self.assertEqual(alfa["alvos"], ["Helicoverpa armigera", "Spodoptera frugiperda"])
        beta = next(p for p in produtos if p["marca_comercial"] == "Beta")
        self.assertEqual(beta["alvos"], [])
        self.assertFalse(any(p["marca_comercial"] == "SemRegistro" for p in produtos))
        self.assertEqual(len(produtos), 1200 + 2)

    def test_lista_truncada_em_60(self):
        extra = [linha(7, "Muitos", f"Cultura {i}", f"Alvo {i}") for i in range(100)]
        produtos, _ = ag.destila(self.escreve_csv(csv_com(0, extra)))
        self.assertEqual(len(produtos[0]["culturas"]), 60)
        self.assertEqual(len(produtos[0]["alvos"]), 60)

    def test_cai_para_cp1252_quando_nao_e_utf8(self):
        extra = [linha(5, "Nematóide", "Soja", "Meloidogyne incognita", ativo="Nematóides")]
        produtos, _ = ag.destila(self.escreve_csv(csv_com(0, extra), "cp1252"))
        self.assertEqual(produtos[0]["ingrediente_ativo"], "Nematóides")

    def test_utf8_com_bom_e_acentos(self):
        extra = [linha(5, "Ação", "Soja", "x", ativo="Fórmula")]
        produtos, _ = ag.destila(self.escreve_csv("﻿" + csv_com(0, extra)))
        self.assertEqual(produtos[0]["marca_comercial"], "Ação")

    def test_coluna_ausente_e_erro_claro(self):
        texto = csv_com(3).replace("PRAGA_NOME_CIENTIFICO", "PRAGA_RENOMEADA")
        with self.assertRaises(ag.ErroAgrofit) as e:
            ag.destila(self.escreve_csv(texto))
        self.assertIn("PRAGA_NOME_CIENTIFICO", str(e.exception))
        self.assertEqual(e.exception.codigo, 2)


class Publicacao(Base):
    def test_gera_parquet_legivel_com_o_schema_do_contrato(self):
        self.assertEqual(self.roda(self.escreve_csv(csv_com(1200))), 0)
        tabela = pq.read_table(self.saida / "produtos.parquet")
        self.assertEqual(tabela.num_rows, 1200)
        self.assertEqual(
            tabela.column_names,
            ["nr_registro", "marca_comercial", "formulacao", "ingrediente_ativo", "titular_registro", "classe",
             "modo_de_acao", "classe_toxicologica", "classe_ambiental", "organico", "situacao", "culturas", "alvos"],
        )
        for coluna in ("culturas", "alvos"):
            tipo = tabela.schema.field(coluna).type
            self.assertTrue(ag.pa.types.is_list(tipo) and ag.pa.types.is_string(tipo.value_type), coluna)
        self.assertEqual(pq.ParquetFile(self.saida / "produtos.parquet").metadata.row_group(0).column(0).compression, "SNAPPY")
        meta = json.loads((self.saida / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["produtos"], 1200)
        self.assertEqual(meta["atualizado_em"], "2026-11-01T09:00:00+00:00")
        self.assertEqual(meta["verificado_em"], meta["atualizado_em"])

    def test_mesmo_conteudo_so_renova_a_verificacao(self):
        csv_path = self.escreve_csv(csv_com(1200))
        self.roda(csv_path, agora="2026-11-01T09:00:00+00:00")
        antes = (self.saida / "produtos.parquet").read_bytes()
        self.roda(csv_path, agora="2026-12-01T09:00:00+00:00")
        meta = json.loads((self.saida / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["atualizado_em"], "2026-11-01T09:00:00+00:00")  # conteudo igual: mantem
        self.assertEqual(meta["verificado_em"], "2026-12-01T09:00:00+00:00")
        self.assertEqual((self.saida / "produtos.parquet").read_bytes(), antes)

    def test_conteudo_novo_atualiza_a_data(self):
        self.roda(self.escreve_csv(csv_com(1200)), agora="2026-11-01T09:00:00+00:00")
        self.roda(self.escreve_csv(csv_com(1200, [linha(99, "Novo", "Soja", "x")])), agora="2026-12-02T09:00:00+00:00")
        meta = json.loads((self.saida / "meta.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["atualizado_em"], "2026-12-02T09:00:00+00:00")
        self.assertEqual(meta["produtos"], 1201)

    def test_so_se_mes_pendente_pula_o_mes_ja_verificado(self):
        csv_path = self.escreve_csv(csv_com(1200))
        self.roda(csv_path, agora="2026-11-01T09:00:00+00:00")
        # dia 2: CSV inexistente nao importa, pois nem tenta
        r = ag.executa(args(csv=str(self.dir / "nao-existe.csv"), saida=str(self.saida), so_se_mes_pendente=True),
                       agora="2026-11-02T09:00:00+00:00")
        self.assertEqual(r, 0)
        # mes seguinte: volta a tentar (e falha por CSV inexistente)
        r = ag.executa(args(csv=str(self.dir / "nao-existe.csv"), saida=str(self.saida), so_se_mes_pendente=True),
                       agora="2026-12-01T09:00:00+00:00")
        self.assertEqual(r, 1)

    def test_csv_pequeno_demais_nao_publica(self):
        self.assertEqual(self.roda(self.escreve_csv(csv_com(50))), 2)
        self.assertFalse((self.saida / "produtos.parquet").exists())

    def test_queda_suspeita_preserva_o_anterior_e_forcar_libera(self):
        self.roda(self.escreve_csv(csv_com(2000)), agora="2026-11-01T09:00:00+00:00")
        antes = (self.saida / "produtos.parquet").read_bytes()
        self.assertEqual(self.roda(self.escreve_csv(csv_com(1200)), agora="2026-12-01T09:00:00+00:00"), 3)
        self.assertEqual((self.saida / "produtos.parquet").read_bytes(), antes)
        self.assertEqual(self.roda(self.escreve_csv(csv_com(1200)), agora="2026-12-01T09:00:00+00:00", forcar=True), 0)
        self.assertEqual(pq.read_table(self.saida / "produtos.parquet").num_rows, 1200)

    def test_tolerar_falha_vira_aviso(self):
        r = ag.executa(args(csv=str(self.dir / "nao-existe.csv"), saida=str(self.saida), tolerar_falha=True))
        self.assertEqual(r, 0)
        self.assertFalse(self.saida.exists())


class Download(Base):
    def test_tentativas_e_falha_final(self):
        import requests

        with mock.patch.object(ag.requests, "get", side_effect=requests.ConnectionError("sem rede")) as get:
            pausas = []
            with self.assertRaises(ag.ErroAgrofit):
                ag.baixa_csv("https://x/y.csv", self.dir / "a.csv", dormir=pausas.append)
        self.assertEqual(get.call_count, ag.TENTATIVAS)
        self.assertEqual(pausas, [30, 90])

    def test_download_ok_grava_o_arquivo(self):
        resp = mock.MagicMock()
        resp.__enter__.return_value = resp
        resp.iter_content.return_value = [b"abc", b"def"]
        with mock.patch.object(ag.requests, "get", return_value=resp):
            ag.baixa_csv("https://x/y.csv", self.dir / "a.csv", dormir=lambda s: None)
        self.assertEqual((self.dir / "a.csv").read_bytes(), b"abcdef")


if __name__ == "__main__":
    unittest.main()
