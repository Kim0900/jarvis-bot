-- Task #171: narrow retry cleanup for image-source partial rows.
--
-- Render uses the anon Supabase role. raw_calls deliberately has no anon DELETE
-- RLS policy, so direct DELETE silently affects zero rows. This SECURITY DEFINER
-- RPC allows only source-scoped cleanup for OCR-generated rows and only when the
-- existing internal RPC secret is present.

create or replace function public.cleanup_call_image_source_rows(
  p_source_id text
)
returns integer
language plpgsql
security definer
set search_path = public, pg_temp
as $cleanup$
declare
  v_deleted integer := 0;
begin
  if not public.fn_internal_rpc_secret_ok() then
    raise exception 'internal authorization required'
      using errcode = '42501';
  end if;

  if p_source_id is null or btrim(p_source_id) = '' then
    raise exception 'source_id required'
      using errcode = '22023';
  end if;

  delete from public.raw_calls
   where source_id = p_source_id
     and data_source in ('drive_ocr_tesseract', 'drive_ocr_layout_v1');

  get diagnostics v_deleted = row_count;
  return v_deleted;
end;
$cleanup$;

revoke all on function public.cleanup_call_image_source_rows(text)
  from public, authenticated;
grant execute on function public.cleanup_call_image_source_rows(text)
  to anon, service_role;

comment on function public.cleanup_call_image_source_rows(text) is
'Task171 retry cleanup. Internal-secret-only, source-scoped DELETE of OCR-generated raw_calls rows. Prevents anon RLS silent no-op during retry.';
