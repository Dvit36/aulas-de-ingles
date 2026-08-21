# PRD — Atividades de Inglês e Leaderboard

## Problema

A equipe controla comprovações de atividades de inglês por prints e consolida pontos manualmente em uma planilha. O processo é lento, sujeito a duplicidade e não mantém uma trilha de auditoria confiável.

## Usuários

- **Aluno:** consulta catálogo, pontuação, posição e progresso; envia comprovantes; acompanha análise e decisões.
- **Administrador:** revisa envios, corrige unidades e gerencia contas e catálogo. O ledger continua sendo a fonte interna da pontuação.

## Fluxos principais

1. Sem autenticação, a pessoa acessa diretamente a página pública **Entrar** pela navegação superior.
2. Na página **Entrar**, o usuário informa e-mail e senha; não existe cadastro público. O modo demo permanece somente para desenvolvimento.
3. Depois da autenticação, a navegação mostra as rotas permitidas pelo papel e **Minha conta**, que concentra identidade e logout.
4. O aluno autenticado escolhe uma atividade, informa campos exigidos e envia imagens, PDF, DOCX ou TXT pela mesma caixa.
5. O servidor valida o arquivo, armazena-o com nome aleatório, executa OCR local e aplica regras configuráveis.
6. Evidência inequívoca e de alta confiança é aprovada; conteúdo subjetivo ou evidência ambígua vai para revisão; arquivo inválido ou duplicata exata comprovada é rejeitado.
7. A aprovação cria unidades ou uma transação imutável no ledger. O leaderboard é sempre recalculado a partir do ledger.
8. O administrador decide pendências sem preencher justificativa; a decisão e o ator continuam registrados na auditoria. Reunião em inglês é uma atividade comum.
9. Depois do commit local, a aplicação pode espelhar leaderboard e ledger em uma planilha Google administrativa; falhas externas não revertem o lançamento.

## Regras de negócio

- O catálogo inicial é o definido no briefing; a planilha legada não sobrescreve esse catálogo.
- Duolingo/BeConfident: cada print de conclusão único e aprovado vale uma unidade. A cada cinco unidades ainda não usadas, são concedidos exatamente 5 pontos.
- `combo` é apenas texto da interface: nunca define a quantidade de lições.
- Uma unidade pertence a, no máximo, um grupo premiado.
- Atividades de pontuação direta usam a pontuação vigente no momento da aprovação. Alterações futuras no catálogo não modificam o ledger.
- Resumos/anotações são validados apenas estruturalmente. O MVP não afirma avaliar qualidade ou veracidade e encaminha conteúdo subjetivo à revisão.
- Reunião em inglês permanece no catálogo como atividade comum de 30 pontos.
- Horários e datas mostrados nas imagens não são fonte confiável. O recebimento usa relógio do servidor.
- Duplicata SHA-256 pode ser rejeitada automaticamente. Similaridade perceptual apenas sinaliza revisão.
- Rejeição ou cancelamento não concede pontos.
- Toda decisão administrativa e alteração relevante gera auditoria.

## Catálogo inicial

| Código | Atividade | Regra inicial |
|---|---|---:|
| `duolingo_beconfident` | Duolingo/BeConfident | 5 pontos / 5 lições |
| `impact_summary` | Impact + resumo em português | 10 pontos |
| `video_fun_summary` | Video FUN + resumo em português | 10 pontos |
| `youtube_lesson_notes` | Videoaula do YouTube + anotações | 12 pontos |
| `cambridge_basic` | Cambridge English Basic | 10 pontos |
| `cambridge_independent` | Cambridge English Independent | 15 pontos |
| `cambridge_proficient` | Cambridge English Proficient | 20 pontos |
| `write_improve_beginner` | Write & Improve Beginner | 10 pontos |
| `write_improve_intermediate` | Write & Improve Intermediate | 15 pontos |
| `write_improve_advanced` | Write & Improve Advanced | 20 pontos |
| `write_improve_fun` | Write & Improve Just for Fun | 7 pontos |
| `write_improve_business` | Write & Improve For Business | 25 pontos |
| `english_meeting` | Reunião em inglês | 30 pontos |

## Escopo do MVP

- Área pública com **Entrar**, autenticação local fechada, troca obrigatória de senha temporária e modo demo local opcional.
- Papéis `student` e `admin`, allowlist e autorização também na camada de serviço.
- Cadastro/edição de usuários e catálogo.
- Submissão de imagens/documentos, extração seletiva, validações, duplicidade e fila de revisão.
- Ledger, grupos de cinco, atividades comuns, leaderboard geral/por período e progresso individual.
- Histórico visual/auditoria, exportação XLSX, sincronização opcional com Google Sheets e importação idempotente da planilha legada.
- Gestão ativa/inativa/arquivada e infraestrutura de lembretes SMTP preservada em processo separado, sem página administrativa na navegação.
- Navegação superior: rotas públicas antes do login; para administradores, **Visão geral**, **Envios**, **Alunos**, **Catálogo** e **Minha conta**. **Relatórios** e **Lembretes** não possuem rota visível.
- Interface web responsiva: em até `768px`, colunas empilhadas e controles interativos com altura mínima de `44px`.
- SQLite WAL, uploads persistentes, Docker Compose, health check, backup documentado e testes offline.

## Fora do escopo

- Aplicativo móvel nativo, notificações, processamento assíncrono distribuído ou múltiplas organizações.
- Avaliação semântica/gerativa de resumos, biometria ou prova de autoria da atividade.
- Antifraude perfeito, recuperação automática de desastres e alta disponibilidade.
- Redis, Celery, Kubernetes, microsserviços e Postgres no MVP.

## Limitações e suposições

- O login identifica quem enviou, mas prints sem nome não provam quem realizou a atividade.
- OCR e heurísticas podem errar; limiares são conservadores e casos duvidosos vão para revisão.
- O volume esperado é cerca de 15 alunos; processamento síncrono numa única máquina é suficiente.
- A planilha real foi analisada apenas localmente e permanece fora do repositório,
  sem modificações, para preservar dados pessoais.
- Os quatro screenshots específicos citados no briefing não estavam anexados. O workspace contém 35 JPEGs alternativos (21 Duolingo e 14 BeConfident), usados para inspeção e testes representativos; nenhum contém `combo x40` ou `combo x51`.
- E-mails iniciais e administradores serão fornecidos por variáveis de ambiente; em desenvolvimento, usuários demo são criados pelo seed.

## Critérios de aceitação

1. Aluno autorizado envia uma ou mais imagens e recebe um estado rastreável.
2. A imagem é validada e processada por OCR local carregado uma vez por processo Streamlit.
3. Exemplos representativos de Duolingo e BeConfident são classificados corretamente.
4. Cada conclusão válida equivale a uma unidade, independentemente de `combo`.
5. Cinco unidades únicas geram uma única transação de 5 pontos.
6. Reenvio byte a byte idêntico não gera pontos.
7. Baixa confiança e hash apenas semelhante entram na fila administrativa.
8. Aprovação manual atualiza o leaderboard na mesma transação lógica.
9. Rejeição não altera o ledger.
10. Administrador pode ajustar pontos somente por transação compensatória auditada.
11. Banco e uploads sobrevivem à reinicialização de containers.
12. Importar novamente a mesma planilha não duplica transações.
13. Testes automatizados obrigatórios passam sem internet ou API externa.
14. O README contém o caminho completo da execução local e por Docker/VPS.
15. Com a integração habilitada, um commit atualiza o snapshot do Sheets; uma falha da API preserva o dado local e pode ser reconciliada sem duplicação.
16. Usuário deslogado vê apenas **Entrar** na navegação superior; após login, vê as rotas do seu papel e **Minha conta**.
17. Refresh preserva a sessão opaca válida e a URL atual; logout, expiração ou troca de senha revogam a sessão.
18. Em viewport de até `768px`, colunas são empilhadas, conteúdo não exige zoom e controles de toque têm pelo menos `44px` de altura.
