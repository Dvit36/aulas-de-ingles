-- Liga o lote de lições ao lançamento que o pagou.
--
-- Sem esta coluna não há como responder "qual lançamento pagou este grupo de
-- cinco lições?" a não ser por correlação frágil de data e valor. O ledger é
-- imutável e o lote é o fato que o originou: a rastreabilidade entre os dois
-- precisa ser explícita.
--
-- ON DELETE RESTRICT porque apagar o lançamento deixaria o lote órfão — e o
-- ledger, de todo modo, não aceita exclusão.
alter table public.lesson_batches
    add column if not exists ledger_transaction_id uuid
        references public.ledger_transactions (id) on delete restrict;

-- Um lançamento paga um único lote: é o que impede pagamento em dobro.
create unique index if not exists lesson_batches_lancamento_unico
    on public.lesson_batches (ledger_transaction_id)
    where ledger_transaction_id is not null;
