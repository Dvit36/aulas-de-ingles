-- Ver a própria prova estava quebrado para o aluno. Destravar.
--
-- `url_temporaria` assina a URL e debita o egress em `storage_usage`, que é
-- `ALL: is_admin()`. Sob o papel do aluno o débito era recusado e a transação
-- caía: quem enviava não conseguia abrir o que enviou, e concluiria que o
-- sistema tinha perdido o arquivo. Era o terceiro caminho do aluno derrubado
-- pela mesma tabela — enviar e cancelar já foram consertados em `0014`/`0015`.
--
-- DERIVAR, NÃO RECEBER
--
-- O cliente informa **qual arquivo**, nunca quantos bytes. A função lê
-- `file_size` da própria linha de `submission_files` e debita esse número.
-- Uma `somar_egress(bytes)` aceitaria o que o chamador dissesse, e um aluno
-- poderia zerar o contador — escapando do teto — ou inflá-lo, negando o
-- download a todos.
--
-- Aqui não dá para ser gatilho, como o upload: assinar URL não escreve linha
-- nenhuma. Por isso é função. Mas o número continua vindo da linha, que é o
-- que importa.
--
-- O QUE ELA MEDE — e o que não mede
--
-- Mede o que o código já media: o tamanho do arquivo no momento da
-- assinatura. Isso é aproximação nos dois sentidos, e o documento de
-- validação registra por quê: subconta quando o navegador busca a mesma URL
-- mais de uma vez dentro da janela, superconta quando a URL é assinada e
-- ninguém abre. Esta migração não melhora nem piora a precisão; faz o débito
-- voltar a acontecer, e com ele o teto voltar a existir para o download.
--
-- A trava de saída continua em `garantir_egress`, antes de assinar, e lê pela
-- `uso_de_storage()` da `0015` — ou seja, com os números de verdade.
create or replace function public.registrar_download_do_arquivo(arquivo uuid)
returns bigint
language plpgsql
security definer
set search_path = public, pg_temp
as $function$
declare
    dono    uuid;
    tamanho bigint;
begin
    select student_id, coalesce(file_size, 0) into dono, tamanho
    from public.submission_files
    where id = arquivo;

    if dono is null then
        raise exception 'Arquivo nao encontrado' using errcode = '42704';
    end if;

    -- A mesma regra de `resolver_arquivo_autorizado`, repetida no banco: a
    -- aplicação já conferiu antes de chegar aqui, e esta é a segunda camada.
    if dono <> auth.uid() and not public.is_admin() then
        raise exception 'Arquivo de outro aluno' using errcode = '42501';
    end if;

    insert into public.storage_usage (period, download_count, egress_bytes)
    values (to_char(now() at time zone 'utc', 'YYYY-MM'), 1, tamanho)
    on conflict (period) do update
        set download_count = storage_usage.download_count + 1,
            egress_bytes   = storage_usage.egress_bytes + tamanho,
            updated_at     = now();

    return tamanho;
end;
$function$;

revoke execute on function public.registrar_download_do_arquivo(uuid) from public;
grant execute on function public.registrar_download_do_arquivo(uuid) to authenticated;
