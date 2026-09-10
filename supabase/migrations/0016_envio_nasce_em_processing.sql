-- O aluno podia declarar o próprio envio aprovado. Fechar.
--
-- Medido em produção, sob claims da conta `teste`, antes desta migração:
--
--   PASSOU    inserir envio JÁ aprovado          -> ('approved_manual', 0)
--   PASSOU    inserir envio aprovado COM pontos  -> ('approved_manual', 999)
--   RECUSADO  inserir envio no nome de outro aluno
--
-- `submissoes_insercao` conferia só `student_id = auth.uid()`, sem dizer nada
-- sobre status. E `submissoes_cancelamento` tinha `with check` só de dono, sem
-- restringir para onde a linha pode ir — o aluno movia o próprio envio de
-- `processing` para `approved_manual`.
--
-- ISSO NÃO VIRAVA PONTO, e é importante ser preciso: o ledger é a fonte de
-- verdade e continua fechado ao aluno, então nada de `points_awarded` alcança
-- o leaderboard. O dano é outro. `points_awarded` é desenhado no cartão do
-- envio por `_render_submission_cards`, que serve às duas telas — então um
-- envio forjado apareceria como "aprovado, 999 pontos concedidos" **na aba
-- Envios que o mentor lê**. Não é roubo de pontos; é mentira no registro que
-- sustenta a decisão de quem revisa.
--
-- PARA ONDE O ALUNO PODE MOVER
--
-- O pipeline roda sob o papel dele e move o envio de `processing` para
-- `needs_review` ou `rejected`, conforme a análise. Cancelar é dele por
-- direito. Aprovar não: aprovação é ato de quem revisa, ou da função que vier
-- a premiar.
--
-- Consequência a registrar: com OCR ligado, `duolingo_beconfident` é
-- `auto_approvable` e o pipeline tentaria `approved_auto` dentro da transação
-- do aluno. Isto passa a barrar essa tentativa aqui, com mensagem de política,
-- em vez de barrá-la três linhas depois no insert do ledger. Não é regressão:
-- o caminho já estava fechado, e continua fechado até a premiação ganhar
-- função própria.
drop policy if exists submissoes_insercao on public.submissions;
create policy submissoes_insercao on public.submissions
    for insert with check (
        student_id = auth.uid()
        and status = 'processing'
        and coalesce(points_awarded, 0) = 0
    );

drop policy if exists submissoes_cancelamento on public.submissions;
create policy submissoes_cancelamento on public.submissions
    for update using (
        student_id = auth.uid()
        and status in ('processing', 'needs_review')
    )
    with check (
        student_id = auth.uid()
        and status in ('processing', 'needs_review', 'rejected', 'cancelled')
        and coalesce(points_awarded, 0) = 0
    );

-- `points_awarded = 0` no `with check` é seguro porque o `using` já limita a
-- linha de origem a `processing` ou `needs_review`, e nenhuma das duas tem
-- pontos concedidos. Envio aprovado o aluno não alcança de forma nenhuma.
--
-- `submissoes_edicao_admin` continua como está: quem revisa move para onde
-- precisar.
