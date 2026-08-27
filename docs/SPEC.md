# SPEC — Produto e arquitetura

> A arquitetura oficial está em [ARCHITECTURE.md](ARCHITECTURE.md) e prevalece
> sobre descrições legadas. Itens ainda não migrados estão explicitamente
> registrados em [TASKS.md](TASKS.md).

## Arquitetura

Aplicação Streamlit em Python, dividida em módulos de domínio. A interface chama
serviços; OCR e regras não dependem da UI. O processamento continua síncrono para
o volume atual, mas persistência e identidade ficam fora do processo Streamlit.

```text
GitHub -> Streamlit UI/Services -> Rules + OCR + Scoring
                    |                 |
                    +-> Supabase Auth |
                    +-> PostgreSQL <--+
                    +-> Supabase Storage
                    +-> Google Sheets (espelho opcional)
```

Supabase PostgreSQL é a fonte de verdade para dados estruturados. Supabase Storage
é a fonte de verdade para binários persistentes. O filesystem do Streamlit é
temporário e nunca participa da recuperação de estado após reinício.

Migrações de schema devem ser explícitas e compatíveis com PostgreSQL. SQLite,
`create_all` no startup e volumes locais permanecem somente durante a transição e
nos testes que ainda não foram portados.

A navegação usa um registro estável de `st.Page` com `st.navigation(position="hidden")` como roteador e uma barra própria de `st.page_link`; o projeto requer Streamlit `>=1.61.1` com o extra `auth`. O registro fixo preserva o hash e a URL no login, logout e refresh, enquanto guards impedem o acesso a rotas não autorizadas. A barra mostra as rotas do papel mais **Recursos** e **Minha conta**, sem controles na sidebar ou gaveta móvel. **Recursos** é registrada uma única vez e liberada para os dois papéis, como **Minha conta**: o registro concatena as listas de administrador e de aluno, e uma mesma URL nas duas viraria entrada duplicada.

## Interface e responsividade

- A página pública **Entrar**, cadastro, recuperação, logout e sessões usam
  Supabase Auth. A aplicação nunca recebe hashes nem grava senhas em tabelas
  próprias.
- O UUID de `auth.users.id` identifica o usuário em todos os dados pertencentes
  a ele. A sessão autenticada, e não um ID fornecido por widget/query string,
  determina o ator.
- **Minha conta** mostra a identidade obtida do Supabase e permite logout e os
  fluxos de conta suportados pelo Auth.
- O breakpoint móvel de referência é `max-width: 768px`.
- Nesse breakpoint, grupos de colunas da interface são apresentados em uma única coluna, na ordem de leitura.
- Botões, inputs, seletores, links de ação e controles equivalentes têm alvo de toque com altura mínima de `44px`.
- Formulários, tabelas e mídia usam a largura disponível; a navegação superior permanece acessível sem depender de sidebar.

## Google Sheets

- PostgreSQL/ledger é a fonte de verdade; Sheets é somente um espelho
  unidirecional.
- A sincronização materializa leaderboard e ledger, encerra a transação de leitura e só então chama a API externa.
- Cria abas ausentes, sobrescreve snapshots e compara o conteúdo antes de escrever; reexecução idêntica é no-op.
- Alterações confirmadas disparam uma tentativa imediata. Falha externa não reverte o commit e pode ser reconciliada por `sync-google-sheets`/agendador.
- O cliente usa `valueInputOption=RAW`, timeout de 15 segundos, retry curto com backoff/jitter para 429/5xx e exclusão mútua por planilha no processo.
- Autenticação usa Application Default Credentials. Em ambiente local, `GOOGLE_APPLICATION_CREDENTIALS` pode apontar para chave de uma conta de serviço dedicada e montada somente leitura.
- O escopo é apenas `spreadsheets`; a planilha deve existir e ser compartilhada diretamente com a conta. Não é necessário escopo Drive.
- O leaderboard espelhado não inclui usuário nem IDs internos. Como permissões são por arquivo, a planilha que contém o ledger é administrativa.

## Modelo de dados

- `profiles`: UUID igual a `auth.users.id`, nome, turma, papel e estado. Não
  contém senha, hash ou token de sessão.
- `activities`: código único, nome, pontos atuais, limiar de unidades, requisitos e configuração JSON, ativo. O limiar vale para qualquer atividade: `1` pontua a cada aprovação, acima de `1` acumula unidades até fechar um grupo.
- `submissions`: aluno, atividade, recebimento no servidor, campos textuais, OCR consolidado, plataforma, confiança, unidades, estado, observação administrativa histórica opcional e snapshot da regra. Novas decisões não exigem observação textual.
- `submission_images`: metadados, `storage_key` única, MIME real, dimensões, tamanho,
  SHA-256 e pHash; nunca contém os bytes da imagem.
- `rule_checks`: resultado individual, obrigatoriedade, score e detalhes.
- `duplicate_matches`: imagem comparada, tipo (`exact`/`similar`) e distância perceptual.
- `approved_evidence`: claim SHA-256 único criado atomicamente antes da pontuação; fecha corridas concorrentes de reenvio exato.
- `approved_file_evidence`: claim SHA-256 único de evidência genérica; estende a mesma garantia a PDF, DOCX e TXT.
- `lesson_units`: unidades aprovadas, únicas por submissão/índice.
- `lesson_batches` e `lesson_batch_units`: grupos de cinco; restrição única em `unit_id` impede reutilização.
- `ledger_transactions`: pontos assinados e imutáveis, tipo, origem e `source_key` única.
- `meetings`: preservada somente para registros históricos anteriores à remoção do fluxo especial.
- `submission_files`: metadados, `storage_key` e texto extraído de
  imagens/PDF/DOCX/TXT; nunca contém binário ou URL presigned persistida.
- Sessões pertencem ao Supabase Auth e não são duplicadas em `auth_sessions`.
- `resources`: título, link, descrição, posição e ativo. Conteúdo puro, sem referência de submissões ou ledger, então a lista é reescrita inteira a cada salvamento. Apenas `http`/`https` são aceitos, porque a lista é renderizada como HTML.
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

- Produção usa Supabase Auth e HTTPS. Senhas nunca passam por tabelas ou hashes
  mantidos pela aplicação.
- A camada de serviço repete a autorização e o Supabase aplica RLS; ocultar
  controles na UI não é proteção suficiente.
- Toda entidade de aluno tem `student_id`/`user_id` derivado de `auth.uid()`.
  Antes de leitura, mudança, exclusão ou URL assinada, a propriedade é validada.
- Upload é limitado por arquivo, quantidade, bytes agregados, páginas e formatos
  reais JPEG/PNG/WEBP/PDF/DOCX/TXT. DOCX tem limites de expansão/compressão e PDF
  tem orçamento de pixels antes da renderização.
- Buckets do Storage são privados. Access Keys e chaves de serviço ficam apenas no
  servidor em `st.secrets`/ambiente. O frontend recebe, quando necessário,
  somente URL presigned curta.
- CORS/XSRF permanecem habilitados. Logs não incluem segredos, bytes nem OCR ou
  imagem completos.

## Persistência e backup

- PostgreSQL armazena dados estruturados, OCR textual e metadados de arquivo.
- Supabase Storage armazena imagens, PDFs, documentos e demais binários.
- GitHub nunca recebe banco, uploads, exports ou backups com dados de alunos.
- Arquivos temporários ficam em memória ou diretório temporário e são removidos
  em `finally`/context manager.
- Backups, retenção e restauração de PostgreSQL e Storage devem ser configurados e
  ensaiados sem transformar o repositório Git em storage.

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

- Produção executa no Streamlit Cloud com Python 3.12, Streamlit `>=1.61.1`,
  dependências nativas para OCR e secrets configurados fora do Git.
- Supabase fornece Auth e PostgreSQL; Supabase Storage fornece storage de objetos.
- Configuração por variáveis de ambiente/`st.secrets`; chave de serviço do
  Supabase e credenciais do Supabase são exclusivamente server-side.
- Docker/SQLite/autenticação local podem existir durante desenvolvimento e
  migração, mas não definem a implantação oficial.
