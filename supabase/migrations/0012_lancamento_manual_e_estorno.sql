-- Motivo do lançamento manual, e o par estorno/original.
--
-- `create_points_adjustment` já existia e gravava com `kind = 'adjustment'`,
-- mas com descrição fixa e aceitando pontos negativos. Duas coisas faltavam.
--
-- `reason` é o texto que sustenta a decisão meses depois, e que o **aluno lê**
-- na tela dele: o lançamento manual aparece marcado, com o motivo visível.
-- Fica separado de `description`, que é rótulo genérico do lançamento e não
-- foi escrito para ninguém ler.
--
-- `reverses_id` transforma o estorno num par consultável, e não em dois
-- lançamentos que só um humano relaciona. O original nunca é tocado — os
-- gatilhos `ledger_sem_update` e `ledger_sem_delete` garantem isso, e é
-- justamente por o ledger ser imutável que a correção precisa apontar para
-- trás.
--
-- A restrição de unicidade é a segunda camada: a aplicação recusa estornar
-- duas vezes o mesmo lançamento, e o banco recusa também. Sem ela, dois
-- cliques viram dois créditos, e o ledger não tem como desfazer.
--
-- O estorno de um estorno é barrado só na aplicação: exprimir "o alvo não
-- pode ter `reverses_id` preenchido" como constraint exigiria consultar outra
-- linha, que `check` não faz.
alter table public.ledger_transactions
    add column reason text,
    add column reverses_id uuid references public.ledger_transactions (id);

create unique index ledger_um_estorno_por_lancamento
    on public.ledger_transactions (reverses_id)
    where reverses_id is not null;

comment on column public.ledger_transactions.reason is
    'Motivo do lançamento manual, obrigatório para kind = adjustment. Visível '
    'para o aluno na tela dele.';

comment on column public.ledger_transactions.reverses_id is
    'Lançamento que este estorna. Um estorno por lançamento, garantido pelo '
    'índice unico parcial.';
