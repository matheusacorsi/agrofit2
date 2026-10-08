# orcreator-agrofit

Base de produtos registrados do **Agrofit (MAPA)** em formato **Parquet**, atualizada
sozinha uma vez por mês, para o ORCreator (Power Apps Code App) consultar.

- **Fonte:** dados abertos do MAPA — dataset
  [Sistema de Agrotóxicos Fitossanitários – Agrofit](https://dados.agricultura.gov.br/dataset/sistema-de-agrotoxicos-fitossanitarios-agrofit),
  recurso *Produto Formulado* (CSV, ~390 MB, ~280 mil linhas).
- **Resultado:** `agrofit/produtos.parquet` (~0,3 MB, um registro por produto) e
  `agrofit/meta.json` (data da atualização e contagem).
- **Já vem com uma versão inicial** (4.403 produtos, extraída da base atual do ORCreator),
  então o app funciona assim que o repositório for publicado, antes da primeira atualização.

## Estrutura

```
agrofit/produtos.parquet        ← lido pelo app
agrofit/meta.json               ← lido pelo app (indicador "Agrofit atualizado em…")
scripts/atualiza_agrofit.py     ← baixa o CSV, agrega por produto, grava o Parquet
tests/test_atualiza_agrofit.py  ← testes (sem rede)
.github/workflows/atualiza-agrofit.yml  ← a automação mensal
requirements.txt
```

O CSV bruto **nunca** vai para o repositório (passa do limite de 100 MB por arquivo do
GitHub): ele é baixado a cada execução, processado e descartado.

---

## Passo a passo no GitHub

### 1. Criar o repositório

1. Em <https://github.com/new>:
   - **Repository name:** `orcreator-agrofit`
   - **Public** (necessário: o app lê por `raw.githubusercontent.com` sem credenciais; os dados
     são abertos do governo). Se a Corteva exigir repositório privado/organização, veja a
     seção *Repositório privado* no fim.
   - **Não** marque *Add a README*, *.gitignore* nem *license* (o repositório precisa nascer vazio
     para o primeiro envio não dar conflito).
2. Clique em **Create repository**.

### 2. Enviar os arquivos

**Opção A — linha de comando (recomendada; preserva a pasta oculta `.github`).**
Na pasta `orcreator-agrofit`:

```bash
git init -b main
git add .
git commit -m "Versao inicial: Agrofit em Parquet + automacao mensal"
git remote add origin https://github.com/<SEU-USUARIO>/orcreator-agrofit.git
git push -u origin main
```

O GitHub pede login no primeiro `push` (navegador ou *Personal Access Token*; o *GitHub Desktop*
também serve: *File → Add local repository* → *Publish repository*).

**Opção B — pelo navegador.** *Add file → Upload files* e arraste **o conteúdo** da pasta.
Atenção: o envio pelo navegador costuma **ignorar a pasta oculta `.github`**. Depois do
upload, crie o workflow à mão: *Add file → Create new file*, digite no nome
`.github/workflows/atualiza-agrofit.yml` e cole o conteúdo do arquivo.

### 3. Permitir que a automação grave no repositório

*Settings → Actions → General*:

- **Actions permissions:** *Allow all actions and reusable workflows*.
- **Workflow permissions:** marque **Read and write permissions** → *Save*.
  (Sem isso o passo "Publicar" falha com erro 403 ao fazer `git push`.)

### 4. (Opcional) Guardar a URL do CSV numa variável

O link direto do recurso no portal do MAPA pode mudar sem aviso. Em vez de editar código,
troque-o numa *variable*: *Settings → Secrets and variables → Actions → aba **Variables** →
New repository variable*:

- **Name:** `AGROFIT_CSV_URL`
- **Value:** o link novo do CSV *Produto Formulado* (clique com o botão direito em *Download*
  no portal do MAPA → copiar endereço do link).

Se a variável não existir ou estiver vazia, vale o link padrão que está no script.

### 5. Rodar a primeira vez agora (sem esperar o dia 1)

*Actions → **Atualizar Agrofit** → Run workflow* → *Run workflow*. Leva alguns minutos
(download de ~390 MB). Conferir:

- o job termina verde e, se houve diferença, aparece um commit
  `dados(agrofit): atualizacao de AAAA-MM-DD`;
- `agrofit/meta.json` agora tem `verificado_em` preenchido.

> Se o download falhar com **403/timeout**, veja *Se o portal do MAPA bloquear o GitHub* abaixo.

### 6. Ligar o app ao repositório

1. Abra no navegador (troque usuário/repositório) e confira que aparece o JSON:
   `https://raw.githubusercontent.com/<SEU-USUARIO>/orcreator-agrofit/main/agrofit/meta.json`
2. No projeto do app, em `connectors/github-raw-agrofit.swagger.json`, troque
   `"basePath": "/SEU-USUARIO/SEU-REPOSITORIO/main"` por
   `"basePath": "/<SEU-USUARIO>/orcreator-agrofit/main"`.
3. Importe/atualize esse conector no portal (*Power Apps → Conectores personalizados*) e
   adicione a fonte de dados ao app (`pa app add data-source`), como no passo 1 de `docs/MIGRACAO.md §8`.

> O `raw.githubusercontent.com` guarda cache de alguns minutos: depois de uma atualização, o
> app pode levar ~5 min para enxergar o arquivo novo.

---

## Como funciona a agenda (e o fallback dos 4 dias)

O workflow dispara nos **dias 1, 2, 3 e 4 de cada mês, às 09:00 UTC (06:00 em Brasília)**.
O script só trabalha se o mês ainda não tiver uma verificação bem-sucedida:

| Situação | O que acontece |
|---|---|
| Dia 1 funcionou | `meta.json` ganha `verificado_em` do mês; dias 2, 3 e 4 só imprimem "mês já verificado" e saem. |
| Dia 1 falhou (portal fora do ar, link mudou…) | Vira **aviso** (workflow verde, nada publicado); o dia 2 tenta de novo, e assim até o dia 3. |
| Dia 4 também falhou | Aí o workflow fica **vermelho** e o GitHub envia e-mail ao dono do repositório. |
| Falhou em todos | O Parquet do mês anterior continua publicado; o app segue funcionando com ele. |

Dentro de cada execução há ainda 3 tentativas de download (esperas de 30 s e 90 s).

**Conteúdo igual ao do mês anterior:** o Parquet não é regravado; só `verificado_em` muda
(`atualizado_em` — a data que o app mostra — só muda quando o conteúdo muda). Esse commit
mensal também mantém o repositório "ativo": o GitHub desativa agendamentos de repositórios
públicos sem atividade por 60 dias.

**Proteções contra um CSV ruim** (o Parquet anterior fica intacto e o job falha):
colunas esperadas ausentes; menos de 1.000 produtos; queda de mais de 20% no número de
produtos frente à versão publicada (use *Run workflow → forcar* se a queda for real).

## Rodar na sua máquina

```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Linux/Mac: .venv/bin/pip
python scripts/atualiza_agrofit.py                    # baixa e atualiza agrofit/
python scripts/atualiza_agrofit.py --csv agrofit.csv  # usa um CSV já baixado
python -m unittest discover -s tests                  # testes
```

Depois de gerar localmente: `git add agrofit && git commit -m "dados(agrofit): atualizacao manual" && git push`.

## Se o portal do MAPA bloquear o GitHub

Sites do governo às vezes recusam IPs de provedores de nuvem (o GitHub roda nos EUA). Sintoma:
o job falha em todas as tentativas com `403 Forbidden` ou `timeout`, mesmo com o link correto.
Alternativas, da mais simples à mais robusta:

1. **Manual 1x ao mês:** baixe o CSV no seu computador (rede da empresa), rode
   `python scripts/atualiza_agrofit.py --csv <arquivo>.csv` e faça `git push` do resultado.
2. **Runner próprio:** instale um *self-hosted runner* numa máquina/servidor da empresa e troque
   `runs-on: ubuntu-latest` por `runs-on: self-hosted` no workflow.

## Repositório privado

O conector atual não usa credenciais, então só funciona com repositório **público**. Se precisar
privado: o `raw.githubusercontent.com` passa a exigir um *token* (cabeçalho `Authorization`), que
deve ser configurado no conector personalizado (autenticação por chave de API) — não coloque o
token no código do app.

## Contrato dos arquivos (o que o app espera)

`agrofit/produtos.parquet` — compressão **snappy**, um grupo de linhas, uma linha por produto
(`NR_REGISTRO` + `MARCA_COMERCIAL`):

| coluna | tipo |
|---|---|
| `nr_registro`, `marca_comercial`, `formulacao`, `ingrediente_ativo`, `titular_registro`, `classe`, `modo_de_acao`, `classe_toxicologica`, `classe_ambiental`, `organico`, `situacao` | string (nulos permitidos) |
| `culturas`, `alvos` | lista de string (até 60 itens cada; `alvos` = nome científico da praga) |

`agrofit/meta.json` — `atualizado_em` (ISO-8601 UTC; data em que o *conteúdo* mudou — é a que o app
exibe), `verificado_em`, `produtos`, `linhas_no_csv_original`, `fonte`, `url_csv`, `hash_conteudo`,
`schema_versao`. O app só usa `atualizado_em` e `produtos`; mudar o nome ou o tipo de uma coluna
exige mudar o app junto (aumente `SCHEMA_VERSAO`).
