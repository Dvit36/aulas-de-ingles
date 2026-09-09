-- O motivo do lançamento manual é texto escrito sobre uma pessoa. Fechar.
--
-- `ledger_leitura` deixava qualquer autenticado ler o ledger inteiro:
--
--     for select using (auth.uid() is not null)
--
-- Ela é de quando o ledger só guardava pontos e chaves de origem. Depois da
-- migração 0012 ele guarda `reason` — "estava atrasado nas lições e
-- compensou" —, e essa frase passou a ser legível por todos os colegas. Não
-- pela tela, que filtra por aluno e passa por `require_self_or_admin`, mas
-- pelo banco, com o JWT do próprio aluno. Medido sob claims reais antes desta
-- migração: o aluno lia o lançamento de outro, com o motivo.
--
-- O comentário original dizia que a leitura ampla existia para o leaderboard.
-- Não era verdade, e dá para verificar: `profiles` já é restrito ao próprio
-- perfil por `perfil_proprio_leitura`, e `leaderboard_rows` faz join com ele.
-- Sob o papel do aluno o ranking já devolvia uma linha só — a dele. A leitura
-- ampla do ledger não sustentava nada; só vazava.
--
-- Por isso esta migração faz duas coisas ao mesmo tempo: fecha o ledger e
-- devolve o ranking, que estava quebrado sem ninguém ter visto — o app tem um
-- aluno só, e com um aluno um ranking de uma linha parece certo.
drop policy if exists ledger_leitura on public.ledger_transactions;

-- O dono e o administrador. Mais ninguém.
create policy ledger_leitura_propria on public.ledger_transactions
    for select using (student_id = auth.uid() or public.is_admin());

-- ---------------------------------------------------------------- o ranking
--
-- O leaderboard precisa de duas coisas que o aluno não pode mais ler linha a
-- linha: a soma de cada colega e o nome de cada colega. Esta função devolve
-- exatamente isso e nada mais.
--
-- Função, e não view agregada. Uma view de dono ignora a RLS de baixo pelo
-- mesmo efeito, mas de forma implícita: quem ligar `security_invoker` depois
-- esvazia o ranking sem erro nenhum, e falha silenciosa é o que já custou
-- caro aqui. A permissão de uma função é explícita, e o recorte fica escrito.
--
-- TRÊS COLUNAS, e nenhuma outra: id, nome exibido e soma. Sem `reason`, sem
-- `description`, sem linha individual, sem `username` — o identificador de
-- login não é exibido em tela nenhuma do ranking, e não atravessa por hábito.
-- Coluna nova aqui entra por nome, uma a uma, e nunca por `select *`.
--
-- Não recebe `student_id`: quem chama não escolhe de quem falar, então não há
-- como percorrer a vida de ninguém por parâmetro. O período entra porque a
-- tela filtra por data, e `null` nos dois lados significa "desde sempre".
create or replace function public.ranking(
    inicio timestamptz default null,
    fim    timestamptz default null
)
returns table (
    student_id   uuid,
    display_name text,
    points       bigint
)
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    -- `auth.uid() is not null` por dentro, no lugar exato onde
    -- `ledger_leitura` a tinha: sem sessão, zero linhas, como antes. O papel
    -- `anon` nem chega aqui, porque o grant abaixo é só para `authenticated`.
    select p.id,
           p.display_name,
           coalesce(sum(l.points), 0)::bigint
    from public.profiles p
    left join public.ledger_transactions l
           on l.student_id = p.id
          and (inicio is null or l.occurred_at >= inicio)
          and (fim    is null or l.occurred_at <  fim)
    where auth.uid() is not null
      and p.role = 'student'
      and p.active
    group by p.id, p.display_name;
$$;

revoke execute on function public.ranking(timestamptz, timestamptz) from public;
grant execute on function public.ranking(timestamptz, timestamptz) to authenticated;

comment on function public.ranking(timestamptz, timestamptz) is
    'Soma e nome por aluno para o leaderboard. Existe porque o ledger e os '
    'perfis sao fechados ao dono: o ranking precisa do agregado, nunca das '
    'linhas. Ordem e empate ficam na aplicacao.';
