-- O teto de custo do Storage não valia para quem consome o Storage.
--
-- `storage_usage` é contabilidade global: política `ALL: is_admin()`. Mas
-- quem faz upload é o aluno, e o pipeline de envio corre sob o papel dele.
-- Duas falhas de naturezas diferentes, medidas em produção:
--
--   ESCRITA  recusada, alto: `new row violates row-level security policy`.
--            Derrubava o envio inteiro depois de o arquivo já ter subido.
--   LEITURA  errada, em silêncio: sob o aluno a linha do mês some, e
--            `uso_atual` devolve zeros. `bytes_armazenados` também: soma
--            `submission_files`, que o aluno só enxerga do próprio.
--
-- A segunda é a grave. `garantir_espaco` e `garantir_egress` decidiam contra
-- zero, então **o teto nunca era aplicado ao papel que gasta** — e sem erro
-- nenhum. Hoje isso está encoberto porque o envio falha antes; consertar só a
-- escrita descobriria o buraco em vez de fechá-lo.
--
-- É a mesma armadilha que já mordeu nas verificações: sob RLS, o que você não
-- pode ver aparece como zero linhas, não como erro.
--
-- ----------------------------------------------------------------- upload
--
-- O contador de upload vira GATILHO, e não função que recebe número.
--
-- Uma `somar_uso(uploads int)` aceitaria o que o chamador dissesse, e um
-- aluno poderia zerar ou inflar o teto de todo mundo. O gatilho **deriva**: a
-- unidade de contagem é a linha de `submission_files` que o aluno já grava
-- legitimamente, com política própria. Ninguém informa quantidade.
create or replace function public.contar_upload_de_storage()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
begin
    insert into public.storage_usage (period, upload_count)
    values (to_char(now() at time zone 'utc', 'YYYY-MM'), 1)
    on conflict (period) do update
        set upload_count = storage_usage.upload_count + 1,
            updated_at   = now();
    return null;
end;
$function$;

drop trigger if exists submission_files_conta_upload on public.submission_files;
create trigger submission_files_conta_upload
    after insert on public.submission_files
    for each row
    execute function public.contar_upload_de_storage();

-- ---------------------------------------------------------------- leitura
--
-- Quatro números e nenhum outro. `stored_bytes` sai da soma de
-- `submission_files` sem RLS, que é o total de verdade — sob o aluno aquela
-- soma devolve só os arquivos dele, e é isso que fazia o teto de espaço
-- decidir contra um número menor que o real.
--
-- Devolver isto a todo autenticado não expõe ninguém: são agregados de
-- consumo, sem aluno, sem arquivo e sem chave.
create or replace function public.uso_de_storage(periodo text)
returns table (
    egress_bytes   bigint,
    upload_count   bigint,
    download_count bigint,
    stored_bytes   bigint
)
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    select coalesce(u.egress_bytes, 0),
           coalesce(u.upload_count, 0),
           coalesce(u.download_count, 0),
           (select coalesce(sum(f.file_size), 0)::bigint
              from public.submission_files f)
    from (select periodo as period) as p
    left join public.storage_usage u on u.period = p.period
    where auth.uid() is not null;
$$;

revoke execute on function public.uso_de_storage(text) from public;
grant execute on function public.uso_de_storage(text) to authenticated;

-- O contador de DOWNLOAD continua fechado ao aluno, de propósito: ele soma
-- `egress_bytes`, que é dinheiro, e não tem linha equivalente de onde
-- derivar. Fica para o desenho que vai tratar junto a premiação — os dois são
-- "aluno mexe em contador global". Enquanto isso, ver a própria prova segue
-- quebrado para o aluno, e está registrado como tal.
