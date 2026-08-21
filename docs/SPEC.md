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

A navegação usa um registro estável de `st.Page` com `st.navigation(position="hidden")` como roteador e uma barra própria de `st.page_link`; o projeto requer Streamlit `>=1.61.1` com o extra `auth`. O registro fixo preserva o hash e a URL no login, logout e refresh, enquanto guards impedem o acesso a rotas não autorizadas. A barra mostra apenas as rotas do papel e **Minha conta**, sem controles na sidebar ou gaveta móvel.

## Interface e responsividade

- A página pública **Entrar** valida senha Argon2 no servidor. O primeiro administrador é criado idempotentemente por variáveis `BOOTSTRAP_ADMIN_*`; não há cadastro público.
- Sessões usam token opaco aleatório, hash SHA-256 no banco, expiração e versão revogável. Um componente v2 bidirecional mantém somente o token e sua validade em `localStorage`, com handshake e confirmação antes de liberar a área privada; papel, usuário e senha nunca ficam no navegador. O banco continua sendo a autoridade para validade e revogação.
- **Minha conta** mostra identidade, troca de senha e logout. Senha temporária bloqueia todas as outras rotas até a troca.
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
- O leaderboard espelhado não inclui usuário nem IDs internos. Como permissões são por arquivo, a planilha que contém o ledger é administrativa.

## Modelo de dados

- `users`: nome de usuário único, nome, papel, ativo. Bancos anteriores tinham a coluna `email`; a migração 2 a renomeia para `username` preservando os valores, então contas migradas continuam entrando com o identificador que já usavam.
- `activities`: código único, nome, pontos atuais, limiar de unidades, requisitos e configuração JSON, ativo. O limiar vale para qualquer atividade: `1` pontua a cada aprovação, acima de `1` acumula unidades até fechar um grupo.
- `submissions`: aluno, atividade, recebimento no servidor, campos textuais, OCR consolidado, plataforma, confiança, unidades, estado, observação administrativa histórica opcional e snapshot da regra. Novas decisões não exigem observação textual.
- `submission_images`: chave aleatória, MIME real, dimensões, tamanho, SHA-256 e pHash.
- `rule_checks`: resultado individual, obrigatoriedade, score e detalhes.
- `duplicate_matches`: imagem comparada, tipo (`exact`/`similar`) e distância perceptual.
- `approved_evidence`: claim SHA-256 único criado atomicamente antes da pontuação; fecha corridas concorrentes de reenvio exato.
- `approved_file_evidence`: claim SHA-256 único de evidência genérica; estende a mesma garantia a PDF, DOCX e TXT.
- `lesson_units`: unidades aprovadas, únicas por submissão/índice.
- `lesson_batches` e `lesson_batch_units`: grupos de cinco; restrição única em `unit_id` impede reutilização.
- `ledger_transactions`: pontos assinados e imutáveis, tipo, origem e `source_key` única.
- `meetings`: preservada somente para registros históricos anteriores à remoção do fluxo especial.
- `submission_files`: metadados e texto extraído de imagens/PDF/DOCX/TXT.
- `auth_sessions`: sessões opacas, expiração e revogação.
- `goal_configuration`: linha única com a meta de lições por semana e o autor da última alteração. Só orienta a interface; nenhuma pontuação depende dela.
- `reminder_configuration` e `email_attempts`: configuração, deduplicação e auditoria de e-mail.
- `audit_logs`: ator, ação, entidade, antes/depois e motivo histórico opcional; novas operações administrativas podem registrar `NULL` nesse campo.
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
- O agrupamento seleciona unidades não usadas em ordem de aprovação, em blocos do tamanho definido por `activities.unit_threshold`, dentro da transação. O grupo é identificado pelo código da atividade, então unidades de atividades distintas nunca se combinam.

## Segurança

- Produção usa autenticação local fechada e HTTPS; hashes Argon2 e tokens de sessão nunca são registrados em logs.
- Modo demo exige `DEMO_AUTH_ENABLED=true`, confirmação explícita na página **Entrar** e falha no startup se combinado com `APP_ENV=production`.
- A camada de serviço repete a autorização; ocultar controles na UI não é considerado proteção suficiente.
- Upload é limitado por arquivo, quantidade, bytes agregados, páginas e formatos reais JPEG/PNG/WEBP/PDF/DOCX/TXT. DOCX tem limites de membros/expansão/compressão e PDF tem orçamento de pixels antes da renderização; tudo é validado antes de persistir e gravado com UUID e modo `0600`.
- CORS/XSRF permanecem habilitados. Logs não incluem bytes nem OCR/imagem completos.
- A aplicação deve ficar atrás de HTTPS em proxy reverso ou acessível por VPN/Tailscale; isso protege o token opaco e as credenciais em trânsito.

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

- Imagem Python 3.12 slim, Streamlit `>=1.61.1` com Authlib, dependências locais e health check em `/_stcore/health`.
- Compose com aplicação e scheduler independente, porta 8501 e volumes compartilhados de banco/uploads.
- Configuração por `.env`; senhas do administrador inicial e SMTP ficam somente no ambiente/secret manager.
- Máquina alvo: 2–4 CPUs, 8 GB RAM e SSD. Sem workers distribuídos.
- Em VPS: firewall, proxy Caddy/Nginx com TLS ou Tailscale/VPN, backups externos e atualização controlada da imagem.
