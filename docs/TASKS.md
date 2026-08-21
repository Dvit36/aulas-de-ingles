# TASKS — Plano verificável

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
