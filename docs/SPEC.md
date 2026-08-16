# SPEC — Arquitetura do MVP

## Arquitetura

Aplicação monolítica Streamlit em Python, dividida em módulos de domínio. A interface chama serviços transacionais; serviços usam SQLAlchemy; OCR/regras e armazenamento não dependem da UI. O processamento é síncrono, adequado ao baixo volume.

```text
Streamlit UI -> Auth/Services -> Rules + OCR + Scoring
                         |               |
                         +-> SQLAlchemy <-+
                         +-> uploads persistentes
                         +-> Google Sheets (espelho pós-commit, opcional)
```

`DATABASE_URL` seleciona SQLite hoje e permite um dialeto Postgres no futuro sem alterar regras de domínio. Para SQLite são habilitados foreign keys, busy timeout e WAL.

A navegação usa `st.Page` com `st.navigation(position="hidden")` como roteador e uma barra própria de `st.page_link` sempre visível; o projeto requer Streamlit `>=1.50`. Assim, o celular não converte o menu em uma gaveta lateral. Sem autenticação, o roteador abre apenas **Entrar**. Depois da autenticação, monta as rotas do papel (`student` ou `admin`) e acrescenta **Minha conta**, sem controles na sidebar.

## Interface e responsividade

- A página pública **Entrar** é a única que inicia autenticação. Em produção, o clique chama `st.login("google")`; no demo, o `selectbox` apenas escolhe a identidade e o submit explícito persiste `demo_user_id` em `st.session_state`.
- **Minha conta** mostra nome, e-mail e papel. O logout OIDC usa `st.logout()`; o logout demo remove `demo_user_id` e reinicia a execução pública.
- O breakpoint móvel de referência é `max-width: 768px`.
- Nesse breakpoint, grupos de colunas da interface são apresentados em uma única coluna, na ordem de leitura.
- Botões, inputs, seletores, links de ação e controles equivalentes têm alvo de toque com altura mínima de `44px`.
- Formulários, tabelas e mídia usam a largura disponível; a navegação superior permanece acessível sem depender de sidebar.

## Google Sheets

- SQLite/ledger é a fonte de verdade; Sheets é somente um espelho unidirecional.
- A sincronização materializa leaderboard e ledger, encerra a transação de leitura e só então chama a API externa.
- Cria abas ausentes, sobrescreve snapshots e compara o conteúdo antes de escrever; reexecução idêntica é no-op.
- Alterações confirmadas disparam uma tentativa imediata. Falha externa não reverte o commit e pode ser reconciliada por `sync-google-sheets`/agendador.
- O cliente usa `valueInputOption=RAW`, timeout de 15 segundos, retry curto com backoff/jitter para 429/5xx e exclusão mútua por planilha no processo.
- Autenticação usa Application Default Credentials. Em ambiente local, `GOOGLE_APPLICATION_CREDENTIALS` pode apontar para chave de uma conta de serviço dedicada e montada somente leitura.
- O escopo é apenas `spreadsheets`; a planilha deve existir e ser compartilhada diretamente com a conta. Não é necessário escopo Drive.
- O leaderboard espelhado não inclui e-mail nem IDs internos. Como permissões são por arquivo, a planilha que contém o ledger é administrativa.

## Modelo de dados

- `users`: e-mail único, nome, papel, ativo.
- `activities`: código único, nome, pontos atuais, limiar de unidades, requisitos e configuração JSON, ativo.
- `submissions`: aluno, atividade, recebimento no servidor, campos textuais, OCR consolidado, plataforma, confiança, unidades, estado, justificativa e snapshot da regra.
- `submission_images`: chave aleatória, MIME real, dimensões, tamanho, SHA-256 e pHash.
- `rule_checks`: resultado individual, obrigatoriedade, score e detalhes.
- `duplicate_matches`: imagem comparada, tipo (`exact`/`similar`) e distância perceptual.
- `approved_evidence`: claim SHA-256 único criado atomicamente antes da pontuação; fecha corridas concorrentes de reenvio exato.
- `lesson_units`: unidades aprovadas, únicas por submissão/índice.
- `lesson_batches` e `lesson_batch_units`: grupos de cinco; restrição única em `unit_id` impede reutilização.
- `ledger_transactions`: pontos assinados e imutáveis, tipo, origem e `source_key` única.
- `meetings`: reunião, data, descrição, aluno e administrador confirmador.
- `audit_logs`: ator, ação, entidade, antes/depois e motivo.
- `import_runs` e `import_records`: relatório e chaves externas idempotentes.

Totais não são persistidos como fonte de verdade. `submissions.points_awarded` é apenas o efeito daquela decisão; leaderboard e saldo vêm de `SUM(ledger_transactions.points)`.

## Estados e transições

Estados mínimos: `processing`, `approved_auto`, `needs_review`, `approved_manual`, `rejected`, `cancelled`.

- Nova submissão começa em `processing`.
- `processing -> rejected`: apenas arquivo inválido ou duplicata exata comprovada.
- `processing -> approved_auto`: todas as regras obrigatórias passam, confiança total atinge o limiar e a atividade permite autoaprovação.
- `processing -> needs_review`: ambiguidade, baixa confiança, pHash semelhante ou conteúdo subjetivo.
- `needs_review -> approved_manual | rejected | cancelled`: somente administrador.
- Estados aprovados/rejeitados são terminais no MVP. Correção posterior deve ser uma transação compensatória auditada, não mutação silenciosa do histórico.

Toda transição é validada por máquina de estados e auditada. Operações de aprovação e ledger ocorrem na mesma transação de banco.

## OCR e motor de regras

- RapidOCR com ONNX Runtime, modelos empacotados/localmente disponíveis e sem API externa.
- O objeto OCR é criado por função `st.cache_resource`; testes podem injetar um OCR falso.
- OpenCV mede dimensões/variância do Laplaciano e prepara variantes de contraste; Pillow valida o conteúdo e normaliza orientação EXIF.
- O reconhecedor latino é usado para português/inglês. Texto consolidado é normalizado sem confundir números de `combo` com unidades.
- Plataforma é inferida por termos (`lição`, `XP`, `Duolingo`, `atividade concluída`, etc.) e sinais visuais apenas como apoio.
- Frases conclusivas incluem variantes de Duolingo e BeConfident. Cor isolada nunca basta para autoaprovar.
- Regras retornam `pass`, `fail` ou `review`, score e explicação. Configurações por atividade vivem no catálogo.
- Atividades com resumo/anotação verificam campos, tamanho mínimo, heurística simples de português e similaridade textual com entregas anteriores; continuam em revisão de conteúdo.

## Duplicidade e integridade

- SHA-256 sobre bytes originais identifica igualdade exata entre qualquer aluno e dentro da mesma submissão.
- ImageHash pHash gera candidatos visuais; distância abaixo do limiar configurado cria alerta, nunca rejeição automática.
- Resultados indicam se a correspondência é do mesmo aluno ou de outro.
- `source_key` única no ledger, unidade única por submissão/índice e participação única de unidade em lote impedem dupla pontuação no banco.
- Submissões usam versionamento otimista; duas decisões administrativas concorrentes não podem sobrescrever silenciosamente o mesmo estado.
- No SQLite, triggers bloqueiam `UPDATE` e `DELETE` no ledger. Correções são novos lançamentos compensatórios.
- O agrupamento seleciona unidades não usadas em ordem de aprovação, em blocos de cinco, dentro da transação.

## Segurança

- Produção usa `st.login`/OIDC (Google preferencial) somente após clique na página **Entrar**, com segredos fora do repositório, allowlist de e-mails, `email_verified` obrigatório e papel salvo no banco.
- Modo demo exige `DEMO_AUTH_ENABLED=true`, confirmação explícita na página **Entrar** e falha no startup se combinado com `APP_ENV=production`.
- A camada de serviço repete a autorização; ocultar controles na UI não é considerado proteção suficiente.
- Upload é limitado por bytes e formatos reais JPEG/PNG/WEBP, decodificado antes de persistir e gravado com UUID.
- CORS/XSRF permanecem habilitados. Logs não incluem bytes nem OCR/imagem completos.
- A aplicação deve ficar atrás de HTTPS em proxy reverso ou acessível por VPN/Tailscale; OIDC exige redirect URI HTTPS em produção.

## Persistência e backup

- SQLite em `/data/app.db` e uploads em `/data/uploads`, ambos volumes persistentes no Compose.
- Escritas usam sessões curtas; WAL melhora concorrência de leitura.
- Backup consistente: pausar brevemente novas escritas, executar backup online do SQLite, copiar uploads e registrar data/checksums; testar restauração periodicamente.
- Arquivos temporários ficam fora do código e são removidos após processamento.

## Importação da planilha

Arquivo analisado: `inputs/aulas ingles 7565.xlsx`, sheet `Página1`, 32 linhas x 14 colunas.

- `A1:B11`: catálogo legado. Há divergência (`Impact` = 15 no arquivo, 10 no briefing); o seed do briefing prevalece.
- Linha 16: `A=Pontuação`, `B=Nomes`, `C:N` são datas de 2026-08-04 a 2026-08-15.
- Linhas 17:31: 15 alunos. Célula numérica em uma data vira transação histórica `imported_daily_score`; `x`/vazio é ignorado.
- Existem 81 lançamentos numéricos, totalizando 585 pontos; os totais calculados por linha são usados para reconciliação, não importados novamente.
- Chave idempotente: namespace lógico + sheet + nome normalizado do aluno + data. Reexecução com mesmo valor é ignorada; valor conflitante é relatado e não altera o histórico automaticamente.
- Se outra planilha contiver apenas nome + total, cria-se uma transação `initial_balance` por aluno.
- Cada execução gera contagens de importados, ignorados e inconsistentes e pode salvar relatório JSON.

O importador nunca salva no arquivo fonte.

## Implantação

- Imagem Python 3.12 slim, Streamlit `>=1.50`, dependências locais e health check em `/_stcore/health`.
- Compose com um serviço, porta 8501 e volumes `app_data`/`app_uploads`.
- Configuração por `.env`; segredos OIDC em arquivo montado ou mecanismo de segredos da VPS.
- Máquina alvo: 2–4 CPUs, 8 GB RAM e SSD. Sem workers distribuídos.
- Em VPS: firewall, proxy Caddy/Nginx com TLS ou Tailscale/VPN, backups externos e atualização controlada da imagem.
