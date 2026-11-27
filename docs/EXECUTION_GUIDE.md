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
- a tipografia deve manter a legibilidade/tamanho aparente das referências; evitar texto excessivamente pequeno;
- header/topo deve seguir a composição da referência, inclusive escala de título/subtítulo/status;
- única exceção continua sendo o fundo: manter `app/static/branding/rag-mcp-background.png`.

### Estado atual da UI

- Dashboard e estrutura principal já foram retrabalhados.
- Search foi retomado e agora possui:
  - Evidence Preview;
  - Ranked Results;
  - dados reais do resultado;
  - reason/method/score de seleção;
  - diagnostics e observabilidade existentes preservados.
- Settings usa dados reais para:
  - Collection Search Behavior;
  - Directory Sources & Auto-Sync;
  - System / Server.
- Não introduzir dados decorativos/fakes para imitar screenshots.

## 5. Retrieval

### 5.1 Query-aware hybrid

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

- [ ] rodar novamente avaliação avançada contra serviço real/PROD;
- [ ] validar nomes de livros, termos técnicos, padrões e conceitos nomeados;
- [ ] investigar individualmente cada caso fraco;
- [ ] usar `EXPLAIN ANALYZE` antes de mexer em SQL/índice lexical;
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
- [ ] rodar suíte relevante completa;
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

- [ ] validar casos compostos onde um lado antes era perdido;
- [ ] medir ganho versus latência;
- [ ] manter desligada em perguntas simples;
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

## 7. Auditoria independente — triagem

A auditoria original foi feita sobre `99aa3b6`; sempre reconfirmar no HEAD.

### Bugs locais que fazem sentido corrigir

- [ ] AUD-008 — atomicidade no source-sync update.
- [ ] AUD-010 — timeout configurável em parsers externos.
- [ ] AUD-011 — MCP get_document extraction em `asyncio.to_thread`.
- [ ] AUD-004 — erro transitório de filesystem não pode parecer deleção confirmada.
- [ ] AUD-007 — Qwen reranker não deve transformar massa residual inteira em `yes`.
- [ ] AUD-009 — `/ingest/start` não deve reprocessar `ready` como fluxo normal.
- [ ] AUD-012 — GC de blob órfão após source-sync, respeitando SHA compartilhado.
- [ ] AUD-019 — atualizar E2E para topologia atual.
- [ ] AUD-022 — poucos testes de integração lexical/hybrid reais.

### Reais, mas exigem decisão pequena antes

- [ ] AUD-013 — source sync versus documento em ingestão ativa.
- [ ] AUD-014 — movimento entre coleções com embeddings incompatíveis.
- [ ] AUD-015 — sync manual e background simultâneos da mesma source.
- [ ] AUD-016 — reindex de coleção com documento ativo.
- [ ] AUD-018 — GraphRAG opcional falhando depois do core ingest.
- [ ] AUD-003 — fila GPU: se requisito continua sendo fila única, reutilizar scheduler existente; nunca criar segundo scheduler.

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
4. [ ] concluir adaptive top_k + documentação;
5. [ ] rodar avaliação avançada real;
6. [ ] corrigir apenas misses comprovados do hybrid/decomposition;
7. [ ] executar testes PostgreSQL focados;
8. [ ] commit/push retrieval;
9. [ ] só então atacar bugs locais da auditoria, um pequeno grupo por vez.

## 9. Regra de handoff

Ao encerrar uma sessão/chat:

1. atualizar este arquivo;
2. registrar branch/worktree atual;
3. marcar checkboxes concluídos;
4. registrar último commit/push;
5. registrar testes executados e resultado;
6. registrar exatamente o próximo passo.

Um novo chat deve poder continuar apenas lendo este documento + `git status` + `git log`.

