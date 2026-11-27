# RAG-MCP — Execution Guide

> Documento canônico de continuidade do ciclo atual.  
> Qualquer novo chat/agente deve ler este arquivo antes de alterar código e atualizá-lo ao avançar.

## 1. Objetivo do ciclo

Concluir as melhorias atuais sem redesenhar o produto:

1. melhorar hybrid retrieval para termos técnicos, conceitos exatos, nomes de livros e padrões;
2. manter adaptive `top_k` útil para não devolver ruído só porque o caller pediu um teto alto;
3. usar query decomposition determinística apenas quando perguntas compostas realmente se beneficiam;
4. validar retrieval com avaliações reproduzíveis e comportamento real no PostgreSQL/PROD;
5. finalizar a UI para ficar o mais fiel possível às referências numeradas em `docs/screens/`;
6. corrigir bugs concretos levantados pela auditoria independente com a menor mudança possível.

## 2. Regras obrigatórias

- KISS/YAGNI: menor correção funcional possível.
- Não criar framework, serviço, camada, lock distribuído ou schema novo sem necessidade demonstrada.
- Não inventar limites globais ou política de produto.
- Defaults continuam configuráveis; evitar hardcode de decisões.
- Confirmar cada finding da auditoria no HEAD antes de alterar.
- Para bugs: teste/reprodução primeiro quando viável; depois correção mínima.
- Rodar testes relevantes antes de commit/push.
- Commits pequenos e coesos.
- Não sobrescrever trabalho de outro worktree/branch.
- Decisão de contrato, privacidade/retenção, rollback/migração, política GPU ou comportamento global: parar e consultar André.

## 3. Estado dos worktrees

Baseline observado no início desta continuidade: `312e173`.

### UI

- worktree: `/rag-mcp/rag-mcp`
- branch: `feat/ui-match-docs-screens`
- trabalho retomado de outro chat.
- contrato visual: `tests/unit/test_static_ui_contract.py`
- estado após retomada: **22/22 testes do contrato visual passando**.
- suíte unitária completa da branch: PASS.
- commit UI: `5c8eaee` (push realizado).
- arquivos principais alterados:
  - `README.md`
  - `app/static/app.js`
  - `app/static/index.html`
  - `app/static/style.css`
  - `docs/architecture.md`
  - `tests/unit/test_static_ui_contract.py`

### Retrieval

- worktree: `/rag-mcp/rag-mcp-retrieval`
- branch: `fix/retrieval-eval-followup`
- baseada em `origin/main`
- WIP:
  - `app/search.py`
  - `tests/unit/test_retrieval_fusion.py`
- correção local atual: adaptive `top_k` nunca pode ultrapassar teto pedido/configurado.
- validação já executada: **18 testes de retrieval-fusion passando**.

## 4. Contrato visual

Referências oficiais:

- `docs/screens/01_logo.png`
- `docs/screens/02_dashboard.png`
- `docs/screens/03_import.png`
- `docs/screens/04_documents.png`
- `docs/screens/05_search.png`
- `docs/screens/06_settings.png`
- `docs/screens/07_background.png`

### Exceção explícita aprovada

A UI deve seguir as telas numeradas o mais fielmente possível **exceto o fundo**.

O fundo runtime continua sendo o atual do RAG-MCP, usando `app/static/branding/rag-mcp-background.png`; não copiar o fundo estrelado/de referência.

### Fidelidade visual obrigatória

Regra reforçada por André em 26/11/2026:

- as telas de `docs/screens/01..06` são **contrato visual literal**, não apenas inspiração;
- copiar ícones/formas, proporções, hierarquia, tipografia, espaçamento, chips/badges e densidade visual o mais fielmente possível;
- não substituir um ícone 3D/cube/chip por um caractere Unicode genérico; usar SVG/CSS equivalente;
- **não usar um ícone apenas parecido**: cada ícone da referência deve ser reproduzido individualmente; `Model Runtime`, `Embedding Model`, `Reranker`, `Classification` e `Resident-Model Behavior` têm símbolos distintos;
- cards KPI do Dashboard devem manter a densidade vertical da referência: curtos/compactos, sem espaço morto; aumentar legibilidade pela fonte, não aumentando a altura do card;
- a tipografia deve manter a legibilidade/tamanho aparente das referências; evitar texto excessivamente pequeno;
- header/topo deve seguir a composição da referência, inclusive escala de título/subtítulo/status;
- única exceção continua sendo o fundo: manter `app/static/branding/rag-mcp-background.png`.

Pipeline/UI checkpoint 26/11/2026:

- pipeline 692 (`ec66701`, branch `fix/ui-dashboard-literal`): **FAILED**;
- pipeline 693 (`f849bf2`, `main`): **SUCCESS**;
- aprovação visual **não concedida**; o Dashboard ainda diverge da referência.

Regra de execução a partir deste ponto: abrir e medir `docs/screens/02_dashboard.png` diretamente e tratar cada elemento como contrato visual, sem inferência. A referência original mede **1672×941 px**. Medidas observadas para o Dashboard: sidebar ~214 px; divisor/header ~98 px; linha de KPIs ~115 px; painéis operacionais ~300 px; painel Model Runtime ~310 px. Não compactar nem expandir por preferência própria.

Dashboard — checklist literal de `docs/screens/02_dashboard.png`:

- [x] sidebar com ícones individuais: Home, Upload, Document, Search, Settings;
- [x] header com ícone Home antes de `Dashboard`, pill `Ready`, divisor e ícone de usuário;
- [x] KPI `Collections` usa **três losangos/camadas empilhados**, não cubo;
- [x] KPI `Documents` usa documento; `Ready` usa check em círculo; `Queue` usa camadas; `Search Requests` usa lupa; `Chunks` usa círculo pontilhado;
- [x] `Runtime Health` e `Recent Activity` usam traço de atividade; `Collection Status` usa camadas; `Model Runtime` usa microchip;
- [x] Runtime Health usa ícones por serviço (PostgreSQL, LM Studio/camadas, Worker/gear, Classification Queue/camadas);
- [x] Recent Activity usa ícones circulares por tipo e horário alinhado à direita;
- [x] Model Runtime: Embedding=camadas ciano; Reranker=camadas claras; Classification=estrela/sparkle; Resident=cubo;
- [x] dimensões restauradas pelas medidas da referência: KPI ~115 px, ops ~300 px, Model Runtime ~310 px;
- [x] pipeline 695 validou a implementação do pixel contract em branch;
- [x] pipeline 697 (`19fbff4`, `main`) publicou/deployou o pixel-contract do Dashboard;
- [ ] validação visual final pelo usuário; qualquer diferença restante deve ser corrigida pelo mesmo processo de recorte/medição da imagem, não por inferência.

Checkpoint Dashboard 26/11/2026:

- [x] KPI cards compactados para a densidade vertical da referência;
- [x] tipografia aumentada sem inflar os cards;
- [x] `Model Runtime`, `Embedding Model`, `Reranker`, `Classification` e `Resident-Model Behavior` usam ícones distintos;
- [x] pipeline 691 validou a revisão visual em cinco stages;
- [ ] validação visual final no PROD pelo usuário; se houver diferença, continuar ajustando contra `02_dashboard.png` sem reinterpretar o layout.

Import — checklist literal de `docs/screens/03_import.png`:

- [x] header usa ícone Upload e subtitle da referência;
- [x] cards `Destination`, `Ingestion Progress`, `Files` e `Ingestion Configuration` seguem a mesma ordem e densidade;
- [x] `Destination` usa Collection + New collection + Create Collection no mesmo eixo visual;
- [x] `Ingestion Progress` usa status, barra, percentual, contadores e Pause/Resume;
- [x] `Files` usa dropzone central e botões Select files / Select folder;
- [x] `Ingestion Configuration` usa Manual / Automatic (LLM) e painel explicativo;
- [x] pipeline 701 validou o pixel-contract em branch;
- [x] pipeline 703 (`2c049d9`, `main`) publicou/deployou o conjunto das quatro telas;
- [ ] validação visual final no PROD.

Documents — checklist literal de `docs/screens/04_documents.png`:

- [x] filtros no topo: collection, status, search e Refresh;
- [x] barra de seleção/ações em linha separada;
- [x] tabela com Document, Collection, Status, AI Suggestion, Progress, Size, Updated e Actions;
- [x] linhas usam file badge, progress bar, chips/status e ações compactas;
- [x] paginação inferior com 25 linhas por página;
- [x] pipeline 701 validou o pixel-contract em branch;
- [x] pipeline 703 (`2c049d9`, `main`) publicou/deployou o conjunto das quatro telas;
- [ ] validação visual final no PROD.

Search — checklist literal de `docs/screens/05_search.png`:

- [x] barra principal única com lupa, query, limpar e Search;
- [x] faixa Top-k / Reranker / Scope;
- [x] `Collection Scope` em duas colunas com regra default e toggles por collection;
- [x] resultado em duas colunas: Answer/evidence à esquerda e Retrieved Results à direita;
- [x] reason/score/fonte continuam usando dados reais do retrieval;
- [x] observabilidade técnica continua disponível no código, mas fora do layout principal aprovado;
- [x] pipeline 701 validou o pixel-contract em branch;
- [x] pipeline 703 (`2c049d9`, `main`) publicou/deployou o conjunto das quatro telas;
- [ ] validação visual final no PROD.

Settings — checklist literal de `docs/screens/06_settings.png`:

- [x] `Models & Runtime` em duas colunas: modelos à esquerda e estado de worker/queue à direita;
- [x] quatro linhas de modelo com ícones distintos;
- [x] `Collection Search Behavior` e `Directory Sources & Auto-Sync` lado a lado;
- [x] `System / Server` com quatro cards;
- [x] dados apresentados vêm do runtime/configuração real; nenhum número decorativo foi inventado;
- [x] controles avançados antigos permanecem no DOM para compatibilidade, mas fora do layout principal aprovado;
- [x] pipeline 701 validou o pixel-contract em branch;
- [x] pipeline 703 (`2c049d9`, `main`) publicou/deployou o conjunto das quatro telas;
- [ ] validação visual final no PROD.

### Revisão visual após deploy 703

Feedback do usuário com screenshots reais de PROD (`1920×1032`):

- Dashboard aprovado e passa a ser a **referência de escala tipográfica/legibilidade** também para as outras páginas;
- Import, Documents, Search e Settings estavam estruturalmente próximos, mas com tipografia/controles pequenos demais e espaço morto excessivo;
- correção obrigatória: manter fontes/controles legíveis como no Dashboard e obter densidade reduzindo padding/gaps, **nunca miniaturizando texto**;
- Documents tinha bug real de apresentação: qualquer extensão diferente de `.md` virava `PDF`; corrigir por extensão real (`PDF`, `EPUB`, `AZW3`, `MD`, `PY`, `JSON`, `DOCX`, etc.);
- depois desta correção, revalidar as quatro telas diretamente contra `03..06`, usando os screenshots de PROD como evidência de escala.
- [x] pipeline 706 validou em branch a correção de escala/legibilidade, file-type badges reais e overlap do Search.
- [x] pipeline 709 (`eda5bf3`, `main`) publicou/deployou a correção em PROD.

### Estado atual da UI

- Dashboard aprovado visualmente pelo usuário após o pixel-contract.
- Import, Documents, Search e Settings foram reestruturados pelo mesmo método contra `docs/screens/03..06`.
- Dados reais, reason/method/score e comportamentos existentes foram preservados; o layout principal foi alinhado às referências.
- Não introduzir dados decorativos/fakes para imitar screenshots.

## 5. Retrieval

### 5.1 Query-aware hybrid

Dogfooding após pipeline 697:

- `Strategy pattern`: lexical ~148 ms, `effective_top_k=4`;
- comparação PostgreSQL/DDD: lexical ~59 ms, decomposition ativa, `effective_top_k=5`;
- título `RAG with Python Cookbook`: lexical ~686 ms porque o acrônimo curto `RAG` ainda forçava full-text; correção concluída: acrônimo curto contido em título explícito é tratado como metadata, não como lexical obrigatório. Pipeline 699 (`beda68d`, `main`) PASS; no PROD o mesmo caso caiu de ~686 ms lexical para ~64 ms, mantendo `RAG with Python Cookbook` no topo e `effective_top_k=3`.


Já existe em `main`:

- intent detection;
- technical token / named concept detection;
- metadata/title/source-path lookup;
- lexical + dense fusion;
- lexical narrowing;
- deterministic decomposition;
- adaptive result count.

Commits já integrados:

- `1b1808d` — query-aware hybrid retrieval and decomposition
- `14c78be` — merge query-aware retrieval
- `60c06e2` — narrow query-aware lexical retrieval
- `99552f5` — merge latency fix

Pendências:

- [x] avaliação avançada executada no PROD: 12/12 casos com known-source recall = 1.0;
- [x] nomes de livros, termos técnicos, padrões e conceitos nomeados validados no PROD;
- [x] casos de usefulness menor revisados; sem miss de fonte, portanto sem mudança algorítmica por métrica heurística isolada;
- [ ] usar `EXPLAIN ANALYZE` antes de mexer em índice lexical;
- [x] reduzir o lexical query de OR entre todos os lexemas para `plainto_tsquery` (AND), preservando o mesmo GIN; motivação: dogfooding mostrou 0,6–3,5 s em termos amplos como `Strategy pattern`; validar no PROD antes de qualquer mudança de índice.
- [ ] adicionar poucos testes PostgreSQL reais de lexical/hybrid;
- [ ] não trocar heurística determinística por LLM planner sem evidência e aprovação.

### 5.2 Adaptive top_k

Decisão de produto já inferida do objetivo atual:

- `top_k` é teto/máximo quando adaptive selection estiver habilitado;
- retornar menos quando a curva de relevância cair;
- nunca retornar acima do teto pedido/configurado.

Estado:

- [x] corrigido bug do mínimo poder ultrapassar teto menor;
- [x] adicionados testes de regressão;
- [x] 18 testes de retrieval-fusion passam;
- [x] suíte unitária relevante completa executada e verde;
- [x] documentar contrato MCP/API como teto;
- [ ] commit/push isolado.

### 5.3 Query decomposition

Manter:

- determinística;
- restrita a comparações/diferenças explícitas;
- poucas subqueries;
- pequeno conjunto por subquery;
- dedup/fusion/rerank depois.

Pendências:

- [x] casos compostos validados; ambos os lados recuperados em Strategy/Aggregate Root e PostgreSQL/DDD;
- [x] ganho/latência medidos em PROD; decomposition ficou restrita aos casos compostos;
- [x] confirmado no PROD: decomposition permanece desligada em perguntas simples;
- [ ] só expandir heurística se caso de avaliação real demonstrar necessidade.

## 6. Protocolo de avaliação retrieval

Base atual: `examples/evaluation/retrieval_advanced.json`.

Cobrir:

- título exato de livro;
- acrônimo/token técnico;
- design pattern/conceito nomeado;
- conceito DDD;
- questão técnica de banco;
- conceito de arquitetura;
- comparação entre duas fontes;
- consulta semântica simples que não deveria pagar custo híbrido/decomposition desnecessário.

Capturar:

- known-source recall;
- reciprocal rank;
- effective top_k;
- motivo de hybrid trigger/skip;
- motivo de decomposition trigger/skip;
- dense/lexical/rerank/total timing;
- P50/P95 quando houver amostra suficiente;
- casos falhos e documentos devolvidos.

Não escolher SLO P95/P99 sem André.

## 6.1 Resultado da avaliação avançada no PROD

Execução com `top_k=20` em todos os 12 casos de `examples/evaluation/retrieval_advanced.json`:

- known-source recall: **12/12 = 1.0**;
- adaptive `effective_top_k`: **3–7**, nunca 20;
- decomposition: ativa apenas em comparações/diferenças explícitas;
- perguntas simples: decomposition desativada;
- `Strategy pattern`: fonte correta, effective_top_k=4;
- `Aggregate Root`: fonte correta, effective_top_k=3;
- PostgreSQL isolation: fonte correta, effective_top_k=3;
- Software Architecture in Practice: fonte correta, effective_top_k=4;
- compound Strategy/DDD e PostgreSQL/DDD: ambos os lados recuperados;
- `compound-rag` teve usefulness heurística baixa, mas source recall=1.0; não alterar arquitetura sem falha real de retrieval.

Decisão: retrieval/hybrid/adaptive/decomposition estão funcionalmente aprovados para este ciclo; próximos ajustes só com miss reproduzível ou evidência de regressão.

## 7. Auditoria independente — triagem

A auditoria original foi feita sobre `99aa3b6`; sempre reconfirmar no HEAD.

### Bugs locais que fazem sentido corrigir

- [x] AUD-008 — source-sync change agora usa transação explícita para DELETE chunks + UPDATE document; cache é removido após commit.
- [x] AUD-010 — timeout configurável em `ingest.external_parser_timeout` (default 60s; env `RAG_EXTERNAL_PARSER_TIMEOUT` pode sobrescrever) aplicado aos parsers externos.
- [x] AUD-011 — MCP `get_document` move leitura de cache/extração para `asyncio.to_thread`.
- [x] AUD-004 — qualquer erro durante scan de source marca o scan incompleto e pula a fase de deleções naquele ciclo.
- [x] AUD-007 — Qwen reranker não converte massa residual não observada em `yes`; top-logprobs somente com `no` retornam 0.0.
- [x] AUD-009 — `/ingest/start` com IDs explícitos aceita apenas documentos `staged`; `ready` exige fluxo de reindex dedicado.
- [x] AUD-012 — source-sync coleta blobs antigos e remove apenas paths sem qualquer documento restante apontando para eles.
- [x] AUD-019 — E2E atualizado para `docker-prod01.lab:8000` e LM Studio `192.168.1.10:1234`, com override por ambiente.
- [ ] AUD-022 — poucos testes de integração lexical/hybrid reais.

### Reais, mas exigem decisão pequena antes

- [x] AUD-013 — source-sync adia alteração/deleção de documento em `ACTIVE_STATUSES`; próximo ciclo aplica quando a ingestão terminar.
- [x] AUD-014 — já resolvido no HEAD: move de documento `ready` rejeita coleção destino com modelo/dimensão incompatíveis; integração existente cobre o caso.
- [x] AUD-015 — syncs da mesma source são serializados por `asyncio.Lock` no processo único atual; sem lock distribuído desnecessário.
- [x] AUD-016 — reindex consulta/locka documentos ativos e retorna 409 antes de apagar chunks.
- [x] AUD-018 — falha de enriquecimento GraphRAG é fail-open após core ingest; `graph_indexed_at` fica stale e o graph worker tenta novamente.
- [x] AUD-003 — `runtime_scheduler.run()` existente agora é a FIFO única das operações de Search, embedding de Ingest e rerank; ingest preserva perfil residente embedding+reranker. Nenhum segundo scheduler criado.

### Nota de escopo do runtime GPU

- fila única fechada neste ciclo para **ingest/query/rerank**, conforme decisão já registrada;
- [x] pipeline 721 (`d99b562`, `main`) publicou/deployou a fila única; busca composta em PROD manteve Strategy + DDD, `effective_top_k=5`, usefulness high.
- autoconfig com LLM pesado continua como item separado porque descarregar o LLM e restaurar imediatamente o perfil residente embedding+reranker exige política explícita de pós-job; não misturar essa decisão neste patch.

### Não alterar sem decisão/medição explícita

- [ ] AUD-017 — rollback/migração.
- [ ] AUD-020 — thresholds P95/P99.
- [ ] AUD-021 — SQL lexical: medir com `EXPLAIN ANALYZE` antes.
- [ ] AUD-005 — retenção/redaction de queries.
- [ ] AUD-023 — fallback de senha DB.
- [x] AUD-002 — direção definida: `top_k` é teto quando adaptive selection está ativo; falta documentação.

## 7.1 Contrato de CI/release

Decisão confirmada por André e alinhada ao padrão do REACH:

- todo pipeline de branch deve mostrar cinco stages: `validate -> tag -> release -> publish -> deploy`;
- a tag verde e a release usam **o mesmo identificador** `vYYYY.MM.NNNNNN`;
- o stage `release` não inventa outra versão: ele materializa o mesmo tag/version em um artefato imutável do SHA validado;
- a imagem Docker deve carregar esse mesmo `version` e o `commit` correspondente em metadata/env;
- em branch comum, `publish` e `deploy` podem ser gates sem efeitos externos;
- no `main`, `Publish GitHub` e `Deploy Production` continuam reais;
- o deploy de PROD deve consumir a release produzida pelo pipeline, não reconstruir identidade por conta própria.

Estado atual:

- [x] cinco stages definidos;
- [x] tag real em todo push de branch;
- [x] `Build Release` usa o mesmo cálculo de versão/tag;
- [x] Dockerfile recebe `RAG_BUILD_VERSION` e `RAG_BUILD_COMMIT` como build args/labels/env;
- [x] `deploy-prod.sh` valida/consome `release/manifest.json` e usa sua versão/commit no build;
- [x] testes de contrato cobrem tag == release version e metadata da imagem;
- [x] pipeline 683 validou os cinco stages em branch (`validate/tag/release/publish/deploy`).

## 8. Ordem de execução atual

1. [x] terminar e validar branch de UI `feat/ui-match-docs-screens`;
2. [x] commit/push da UI sem misturar retrieval;
3. [x] voltar à branch `fix/retrieval-eval-followup`;
4. [x] concluir adaptive top_k + documentação;
5. [x] rodar avaliação avançada real;
6. [x] corrigir apenas misses comprovados do hybrid/decomposition (nenhum miss restante nos 12 casos avançados);
7. [ ] executar testes PostgreSQL focados (local bloqueado sem credencial de DB de teste; validar pelo job Integration do CI);
8. [x] commit/push retrieval;
9. [x] iniciar bugs locais da auditoria em pequenos grupos; grupo 1 = AUD-008/010/011.

## 9. Regra de handoff

Ao encerrar uma sessão/chat:

1. atualizar este arquivo;
2. registrar branch/worktree atual;
3. marcar checkboxes concluídos;
4. registrar último commit/push;
5. registrar testes executados e resultado;
6. registrar exatamente o próximo passo.

Um novo chat deve poder continuar apenas lendo este documento + `git status` + `git log`.

