-- O envio nunca funcionou em produção. Esta migração destrava.
--
-- `submit_evidence` roda sob o papel do aluno e escreve em oito tabelas. Seis
-- recusam, medido sob claims reais: `rule_checks`, `duplicate_matches`,
-- `lesson_units`, `lesson_batches`, `approved_evidence` e
-- `ledger_transactions` — mais `audit_logs`, que `add_audit` grava em todo
-- envio. As políticas foram escritas para "aluno le o proprio, admin
-- escreve", e ninguem confrontou essa lista com a do codigo que escreve.
--
-- ESTA migração cobre só o que o envio percorre HOJE, com tudo caindo em
-- revisão humana: checks, duplicatas e auditoria. O caminho da aprovação
-- automática — unidade, lote, evidência e ledger — vem depois, em migração
-- própria, porque ali a função precisa **derivar** os pontos em vez de
-- recebê-los. Separadas de propósito: esta faz o aluno voltar a enviar hoje.
--
-- POR QUE FUNÇÃO, E NÃO POLÍTICA DE INSERT
--
-- Uma política do tipo "pode inserir se a submissão é sua" devolveria ao
-- aluno a capacidade de forjar as linhas a qualquer momento, de fora da
-- aplicação. A função é mais estreita: ela existe por uma janela só — a
-- submissão precisa ser dele **e** estar em `processing` —, e confere as
-- relações que o banco consegue conferir sozinho.
--
-- Vale dizer o que ela NÃO resolve, para ninguém confiar demais: o conteúdo
-- de um check é juízo da análise, e nenhum banco sabe se é verdadeiro. O que
-- fecha essa porta é a submissão não poder nascer aprovada — política, não
-- função, e assunto de outra migração.
--
-- FORMATO DO JSON. Silêncio aqui é o modo de falhar mais caro: uma chave
-- errada viraria zero linhas gravadas sem erro nenhum. Por isso todo campo
-- obrigatório entra com `->>` e cast explícito, e a coluna é `not null` do
-- outro lado — chave errada estoura, não some.
--
--   checks:     [{"name": "...", "outcome": "pass|fail|review",
--                 "required": true, "score": 0.0,
--                 "message": null, "details": {}}]
--   duplicatas: [{"file_id": "uuid", "matched_file_id": "uuid",
--                 "kind": "exact|similar", "distance": 0,
--                 "same_student": true}]
create or replace function public.registrar_analise_do_envio(
    envio      uuid,
    checks     jsonb default '[]'::jsonb,
    duplicatas jsonb default '[]'::jsonb
)
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    dono     uuid;
    situacao text;
    intrusos int;
begin
    select student_id, status::text into dono, situacao
    from public.submissions
    where id = envio;

    if dono is null then
        raise exception 'Envio nao encontrado' using errcode = '42704';
    end if;

    -- O administrador também passa: ele percorre o mesmo código ao reprocessar.
    if dono <> auth.uid() and not public.is_admin() then
        raise exception 'Envio de outro aluno' using errcode = '42501';
    end if;

    -- A janela. Fora de `processing` a análise já aconteceu, e regravar seria
    -- reescrever o histórico de uma decisão tomada.
    if situacao <> 'processing' then
        raise exception 'Envio fora da janela de analise' using errcode = '42501';
    end if;

    -- Relação que o banco consegue conferir: arquivo citado numa duplicata tem
    -- de ser arquivo deste envio. Sem isto a função aceitaria pendurar
    -- duplicata em arquivo alheio.
    select count(*) into intrusos
    from jsonb_array_elements(duplicatas) as d
    where (d->>'file_id') is not null
      and not exists (
          select 1 from public.submission_files f
          where f.id = (d->>'file_id')::uuid
            and f.submission_id = envio
      );
    if intrusos > 0 then
        raise exception 'Duplicata aponta arquivo de outro envio'
            using errcode = '42501';
    end if;

    insert into public.rule_checks
        (submission_id, name, outcome, required, score, message, details)
    select envio,
           c->>'name',
           c->>'outcome',
           coalesce((c->>'required')::boolean, true),
           coalesce((c->>'score')::double precision, 0),
           c->>'message',
           coalesce(c->'details', '{}'::jsonb)
    from jsonb_array_elements(checks) as c;

    insert into public.duplicate_matches
        (submission_id, file_id, matched_file_id, kind, distance, same_student)
    select envio,
           (d->>'file_id')::uuid,
           (d->>'matched_file_id')::uuid,
           d->>'kind',
           (d->>'distance')::int,
           coalesce((d->>'same_student')::boolean, false)
    from jsonb_array_elements(duplicatas) as d;
end;
$function$;

revoke execute on function public.registrar_analise_do_envio(uuid, jsonb, jsonb)
    from public;
grant execute on function public.registrar_analise_do_envio(uuid, jsonb, jsonb)
    to authenticated;

-- ------------------------------------------------------------ auditoria
--
-- `audit_logs` tem política só de administrador, e `add_audit` roda em todo
-- envio e em todo cancelamento. Já mordeu antes, em
-- `concluir_troca_de_senha`: o insert recusado abortava a transação e levava
-- junto o trabalho legítimo. Aqui derrubava o envio inteiro, e derrubava
-- também o cancelamento pelo aluno — que, medido, está quebrado hoje pelo
-- mesmo motivo.
--
-- O que a função deriva, em vez de aceitar: o ator é sempre `auth.uid()`, a
-- entidade é sempre o envio, e a ação sai de uma lista fechada. O aluno não
-- consegue escrever auditoria arbitrária, que é o risco de abrir esta tabela.
create or replace function public.registrar_auditoria_do_envio(
    envio  uuid,
    acao   text,
    motivo text default null,
    depois jsonb default null
)
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    dono uuid;
begin
    if acao not in ('submission_processed', 'submission_cancelled') then
        raise exception 'Acao fora da lista permitida' using errcode = '42501';
    end if;

    select student_id into dono from public.submissions where id = envio;
    if dono is null then
        raise exception 'Envio nao encontrado' using errcode = '42704';
    end if;
    if dono <> auth.uid() and not public.is_admin() then
        raise exception 'Envio de outro aluno' using errcode = '42501';
    end if;

    insert into public.audit_logs
        (actor_id, action, entity_type, entity_id, reason, after_json)
    values (auth.uid(), acao, 'submission', envio::text, motivo, depois);
end;
$function$;

revoke execute on function public.registrar_auditoria_do_envio(uuid, text, text, jsonb)
    from public;
grant execute on function public.registrar_auditoria_do_envio(uuid, text, text, jsonb)
    to authenticated;
