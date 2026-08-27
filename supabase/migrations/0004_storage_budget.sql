-- Orçamento do Supabase Storage.
--
-- O plano gratuito inclui 1 GB de espaço e 10 GB de egress por mês. Acima
-- disso a cobrança é real ($0,0213/GB-mês). Não existe teto de gasto que
-- pause o serviço, então o limite é aplicado pela aplicação, antes de cada
-- operação.
--
-- Espaço ocupado sai da soma dos metadados; egress é contado aqui porque o
-- Storage não expõe esse número em tempo real.

create table public.storage_usage (
    period         text primary key,          -- 'AAAA-MM' em UTC
    egress_bytes   bigint not null default 0, -- o que é efetivamente cobrado
    upload_count   bigint not null default 0,
    download_count bigint not null default 0,
    updated_at     timestamptz not null default now(),
    constraint storage_usage_periodo_formato check (period ~ '^\d{4}-\d{2}$'),
    constraint storage_usage_nao_negativo check (
        egress_bytes >= 0 and upload_count >= 0 and download_count >= 0
    )
);

alter table public.storage_usage enable row level security;

create policy uso_storage_admin on public.storage_usage
    for all using (public.is_admin()) with check (public.is_admin());

grant select, insert, update on public.storage_usage to authenticated;

-- Soma dos bytes já armazenados. Toda linha de submission_files corresponde
-- a um objeto no bucket, então esta é a fonte de verdade do espaço ocupado.
create or replace function public.storage_bytes_armazenados()
returns bigint
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    select coalesce(sum(file_size), 0)::bigint from public.submission_files;
$$;

grant execute on function public.storage_bytes_armazenados() to authenticated;
