# TASKS — Plano verificável

> As seções 1–7 registram a implementação do MVP legado. A arquitetura oficial
> mudou em 26 de agosto de 2026; produção só será aderente após concluir a seção
> 8. Consulte [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Descoberta e documentação

- [x] Inventariar workspace e preservar materiais originais.
- [x] Inspecionar planilha, fórmulas, datas, alunos e totais.
- [x] Inspecionar screenshots e separar exemplos por plataforma.
- [x] Registrar regras, limitações e critérios em `PRD.md` e `SPEC.md`.

## 2. Fundação e persistência

- [x] Criar configuração por ambiente e validações de produção.
- [x] Implementar modelos SQLAlchemy, SQLite WAL e criação/seed idempotentes.
- [x] Implementar estados, autorização, auditoria e chaves de integridade.

## 3. Processamento de submissões

- [x] Validar e armazenar uploads com nomes aleatórios.
- [x] Integrar RapidOCR/ONNX com cache de recurso do Streamlit.
- [x] Implementar legibilidade, plataforma, conclusão e confiança.
- [x] Implementar SHA-256, pHash e comparação intra/interalunos.
- [x] Implementar campos obrigatórios, idioma e similaridade textual.

## 4. Pontuação e administração

- [x] Criar ledger imutável e leaderboard por período.
- [x] Formar grupos transacionais de cinco unidades sem reutilização.
- [x] Implementar aprovação, rejeição, correção de unidades e ajustes auditados.
- [x] Implementar CRUD básico de alunos e catálogo sem efeito retroativo.

## 5. Interface

- [x] Criar login local fechado, bootstrap idempotente, senha temporária e sessões revogáveis.
- [x] Criar dashboard/formulário/histórico/progresso do aluno.
- [x] Criar fila, detalhe da análise, decisões e telas administrativas.
- [x] Criar leaderboard visual, ledger e downloads XLSX.
- [x] Substituir a sidebar por navegação nativa na barra superior.
- [x] Expor somente **Entrar** antes do login e rotas do papel + **Minha conta** depois do login.
- [x] Concluir responsividade até `768px`, com colunas empilhadas, barra superior sem gaveta lateral e controles de toque de no mínimo `44px`.
- [x] Criar espelho idempotente e opcional no Google Sheets após commit.

## 6. Legado e operação

- [x] Implementar importação idempotente e relatório JSON.
- [x] Criar `.env.example`, `.gitignore`, Dockerfile e Compose persistente.
- [x] Documentar execução local/VPS, HTTPS/VPN, backup e restauração.

## 7. Verificação

- [x] Testar pontos, grupos de cinco e dupla pontuação.
- [x] Testar SHA-256, pHash e regras com fixtures representativas.
- [x] Testar transições, permissões, aprovação/rejeição e lotes de pontos.
- [x] Aceitar PDF, DOCX e TXT com validação real e carregamento seletivo.
- [x] Criar históricos visuais de aluno/admin e gestão com arquivamento.
- [x] Criar lembretes SMTP em dry-run e scheduler independente.
- [x] Remover rota especial de reuniões e exportação CSV.
- [x] Testar importação repetida, persistência e leaderboard.
- [x] Executar suite offline, corrigir falhas e validar startup/health check.
- [x] Testar criação, atualização e no-op do espelho Sheets com gateway local falso.
- [x] Validar navegação, formulários, tabelas e ações em `768px`, `390px` e desktop, incluindo ausência de overflow horizontal.

## 8. Migração para Supabase e Supabase Storage

- [x] Registrar a decisão oficial em `AGENTS.md`, `ARCHITECTURE.md`, PRD, SPEC e README.
- [x] Implementar o carregamento e a validação das variáveis/Secrets reservadas
  para Supabase (Auth, PostgreSQL e Storage), mantendo apenas exemplos sem credenciais no Git.
- [x] Criar migrações PostgreSQL para perfis, domínio, ledger e metadados de
  arquivos, usando UUID de `auth.users.id` como identidade.
- [x] Habilitar e testar RLS em toda tabela pertencente a usuário, incluindo
  políticas administrativas separadas.
- [x] Substituir cadastro, login, logout, recuperação e sessão locais por
  Supabase Auth; remover hashes e tokens próprios do schema ativo.
- [x] Implementar adaptador privado do Supabase Storage com upload, download,
  exclusão compensatória e URL presigned de curta duração.
- [x] Alterar submissões para processar em memória/temporário, limpar em caso de
  sucesso ou erro, enviar o binário ao Storage e gravar apenas metadados/`storage_key`.
- [x] Alterar leituras e downloads para resolver o registro sob autorização/RLS
  antes de acessar a `storage_key`; rejeitar chaves arbitrárias vindas da interface.
- [x] Implementar idempotência e reconciliação de objetos órfãos sem duplicar
  arquivos.
- [x] Criar migração verificável de usuários, SQLite e uploads existentes para
  Supabase, com relatório, checksums, dry-run e estratégia de rollback.
- [x] Desativar `GITHUB_BACKUP_*` e remover os comandos/acionamentos que enviavam
  banco ou uploads ao GitHub.
- [x] Remover o módulo e os testes históricos de GitHub backup depois de usar o
  que for necessário na migração controlada dos arquivos antigos.
- [x] Atualizar Google Sheets, scheduler e exports para usar PostgreSQL sem
  depender de volumes locais.
- [x] Adicionar testes de isolamento entre alunos, RLS, presigned URLs,
  compensação no Storage e limpeza de temporários.
- [ ] Executar ensaio completo de deploy/redeploy no Streamlit Cloud e comprovar
  que dados e arquivos permanecem disponíveis sem filesystem local.
  *(Pendente: verificado localmente contra os serviços reais — runtime sobe no
  PostgreSQL, RLS 9/9, Storage 10/10, ciclo de conta ponta a ponta —, falta o
  redeploy real no Cloud.)*
- [ ] Configurar e testar backup/restauração de Supabase (Auth, PostgreSQL e Storage) fora do GitHub.
- [x] Remover da documentação operacional os passos legados após a migração.
- [ ] Declarar produção aderente somente depois do ensaio de deploy acima.
