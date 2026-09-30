-- Task #164 / Task #27 shared canonical raw_calls selector v1.
-- READ-ONLY canonicalization: no source row is deleted or overwritten.
-- Calendar-day semantics follow MAGI event #3966.
--
-- Automatic collapse is limited to STRONG identity:
--   A) same platform + same fare + exact start AND end, or
--   B) one/both rows have incomplete timing, same platform + same fare +
--      same-position time + normalized origin/destination equality.
-- Cross-boundary time-only matches are WEAK candidates and are NOT auto-collapsed.
-- Access contract:
-- - Render currently uses the anon Supabase role.
-- - This view MUST NOT silently downgrade Drive evidence when the internal RPC
--   secret is absent, because call_image_ingestions SELECT is secret-gated.
-- - Therefore the view itself is fail-closed: without the internal secret it
--   returns zero rows.
-- - Production consumers must use get_canonical_raw_calls_v1() through the
--   dedicated internal-secret helper.

create or replace view public.canonical_raw_calls_v1
with (security_invoker = true)
as
with eligible as (
  select
    r.*,
    case
      when coalesce(r."콜유형",'') ilike '%카카오%' then 'KAKAO'
      when coalesce(r."콜유형",'') ilike '%우버%' then 'UBER'
      when coalesce(r."콜유형",'') ilike '%배회%' then 'ROAM'
      when regexp_replace(coalesce(r."콜유형",''),'[[:space:]]+','','g') = ''
           and r.data_source in ('drive_ocr_layout_v1','drive_ocr_tesseract','app_ocr_individual')
        then 'KAKAO'
      else 'UNKNOWN'
    end as canonical_platform,
    lower(regexp_replace(coalesce(r."출발지",''),'[[:space:]·.]+','','g')) as _origin_norm,
    lower(regexp_replace(coalesce(r."도착지",''),'[[:space:]·.]+','','g')) as _dest_norm,
    case
      when r.data_source like 'manual_correction%'
           and r.verify_status='verified' then 600
      when r.data_source='drive_ocr_layout_v1'
           and r.source_id is not null
           and exists (
             select 1
             from public.call_image_ingestions i
             where i.source_id=r.source_id
               and i.status='COMPLETED'
           ) then 550
      when r.data_source='drive_ocr_tesseract'
           and r.source_id is not null
           and exists (
             select 1
             from public.call_image_ingestions i
             where i.source_id=r.source_id
               and i.status='COMPLETED'
           ) then 500
      when r.data_source='app_ocr_individual' then 450
      when r.data_source='argos_reconstructed'
           and r.verify_status='verified' then 400
      when r.verify_status='verified' then 350
      when r.source_id is not null then 300
      when r.status='confirmed' then 200
      else 100
    end as canonical_evidence_rank
  from public.raw_calls r
  where public.fn_internal_rpc_secret_ok()
    and coalesce(r.raw_row_type,'trip') <> 'daily_total'
    and coalesce(r.data_source,'') <> 'app_ocr_summary'
),
annotated as (
  select
    e.*,
    (
      select count(*)
      from eligible d
      where d.id <> e.id
        and d."날짜"=e."날짜"
        and d.canonical_platform=e.canonical_platform
        and d."요금" is not distinct from e."요금"
        and (
          (
            e."배차시각" is not null and e."하차시각" is not null
            and d."배차시각" is not null and d."하차시각" is not null
            and e."배차시각"=d."배차시각"
            and e."하차시각"=d."하차시각"
          )
          or
          (
            (e."배차시각" is null or e."하차시각" is null
             or d."배차시각" is null or d."하차시각" is null)
            and e._origin_norm<>'' and e._dest_norm<>''
            and e._origin_norm=d._origin_norm
            and e._dest_norm=d._dest_norm
            and (
              (e."배차시각" is not null and d."배차시각" is not null
               and e."배차시각"=d."배차시각")
              or
              (e."하차시각" is not null and d."하차시각" is not null
               and e."하차시각"=d."하차시각")
            )
          )
        )
    ) as canonical_strong_peer_count,
    (
      select count(*)
      from eligible w
      where w.id <> e.id
        and w."날짜"=e."날짜"
        and w.canonical_platform=e.canonical_platform
        and w."요금" is not distinct from e."요금"
        and not (
          (
            e."배차시각" is not null and e."하차시각" is not null
            and w."배차시각" is not null and w."하차시각" is not null
            and e."배차시각"=w."배차시각"
            and e."하차시각"=w."하차시각"
          )
          or
          (
            (e."배차시각" is null or e."하차시각" is null
             or w."배차시각" is null or w."하차시각" is null)
            and e._origin_norm<>'' and e._dest_norm<>''
            and e._origin_norm=w._origin_norm
            and e._dest_norm=w._dest_norm
            and (
              (e."배차시각" is not null and w."배차시각" is not null
               and e."배차시각"=w."배차시각")
              or
              (e."하차시각" is not null and w."하차시각" is not null
               and e."하차시각"=w."하차시각")
            )
          )
        )
        and (
          (e."배차시각" is not null and e."배차시각" in (w."배차시각",w."하차시각"))
          or
          (e."하차시각" is not null and e."하차시각" in (w."배차시각",w."하차시각"))
          or
          (w."배차시각" is not null and w."배차시각" in (e."배차시각",e."하차시각"))
          or
          (w."하차시각" is not null and w."하차시각" in (e."배차시각",e."하차시각"))
        )
    ) as canonical_weak_candidate_count
  from eligible e
),
selected as (
  select a.*
  from annotated a
  where not exists (
    select 1
    from eligible h
    where h.id <> a.id
      and h."날짜"=a."날짜"
      and h.canonical_platform=a.canonical_platform
      and h."요금" is not distinct from a."요금"
      and (
        (
          a."배차시각" is not null and a."하차시각" is not null
          and h."배차시각" is not null and h."하차시각" is not null
          and a."배차시각"=h."배차시각"
          and a."하차시각"=h."하차시각"
        )
        or
        (
          (a."배차시각" is null or a."하차시각" is null
           or h."배차시각" is null or h."하차시각" is null)
          and a._origin_norm<>'' and a._dest_norm<>''
          and a._origin_norm=h._origin_norm
          and a._dest_norm=h._dest_norm
          and (
            (a."배차시각" is not null and h."배차시각" is not null
             and a."배차시각"=h."배차시각")
            or
            (a."하차시각" is not null and h."하차시각" is not null
             and a."하차시각"=h."하차시각")
          )
        )
      )
      and (h.canonical_evidence_rank,h.id) > (a.canonical_evidence_rank,a.id)
  )
)
select
  id,"날짜","요일","배차시각","출발지","도착지","요금","콜유형","비고",
  created_at,"하차시각","결제수단","운행시간_분","주행거리_km","영업거리_km",
  "공차거리_km","구속시간_h","km당매출",status,rule_flag,"건수",date_axis,
  "카카오매출","카드매출","현금매출","기타매출","메모",data_source,
  verify_status,platform_date,business_date,business_session_id,raw_row_type,source_id,
  canonical_platform,
  canonical_evidence_rank,
  canonical_strong_peer_count,
  canonical_weak_candidate_count,
  case
    when canonical_weak_candidate_count > 0 then 'CANONICAL_WITH_WEAK_DUPLICATE_CANDIDATE'
    when canonical_strong_peer_count > 0 then 'CANONICAL_SELECTED_FROM_STRONG_DUPLICATE_GROUP'
    else 'CANONICAL_UNIQUE'
  end as canonical_state
from selected;

comment on view public.canonical_raw_calls_v1 is
'Task164/Task27 shared read-only canonical selector v1. Fail-closed without X-MAGI-RPC-Secret. Raw provenance retained. Strong duplicates collapsed by evidence precedence; weak time-only candidates retained and flagged.';

revoke all on public.canonical_raw_calls_v1 from public, anon, authenticated;
grant select on public.canonical_raw_calls_v1 to anon, service_role;

create or replace function public.get_canonical_raw_calls_v1(
  p_start_date date default null,
  p_end_date date default null
)
returns setof public.canonical_raw_calls_v1
language plpgsql
security invoker
set search_path = public, pg_temp
as $
begin
  if not public.fn_internal_rpc_secret_ok() then
    raise exception 'canonical_raw_calls_v1 internal authorization required'
      using errcode = '42501';
  end if;

  return query
  select c.*
  from public.canonical_raw_calls_v1 c
  where (p_start_date is null or c."날짜" >= p_start_date)
    and (p_end_date is null or c."날짜" <= p_end_date)
  order by c."날짜", c."배차시각" nulls last, c.id;
end;
$;

revoke all on function public.get_canonical_raw_calls_v1(date,date)
  from public, authenticated;
grant execute on function public.get_canonical_raw_calls_v1(date,date)
  to anon, service_role;

comment on function public.get_canonical_raw_calls_v1(date,date) is
'Approved Task164/Task27 canonical read path. SECURITY INVOKER + mandatory internal RPC secret. No-secret calls fail with 42501; the underlying view also returns zero rows without the secret.';
