-- Task #164 canonical platform equivalence fixture.
-- Expected results:
-- app blank -> KAKAO
-- drive tesseract null -> KAKAO
-- drive layout whitespace -> KAKAO
-- reconstructed blank -> UNKNOWN
-- explicit Uber remains UBER

with fixture(call_type, data_source, expected) as (
  values
    (''::text, 'app_ocr_individual'::text, 'KAKAO'::text),
    (null::text, 'drive_ocr_tesseract'::text, 'KAKAO'::text),
    ('   '::text, 'drive_ocr_layout_v1'::text, 'KAKAO'::text),
    (''::text, 'argos_reconstructed'::text, 'UNKNOWN'::text),
    ('우버'::text, 'app_ocr_individual'::text, 'UBER'::text)
),
classified as (
  select *,
    case
      when coalesce(call_type,'') ilike '%카카오%' then 'KAKAO'
      when coalesce(call_type,'') ilike '%우버%' then 'UBER'
      when coalesce(call_type,'') ilike '%배회%' then 'ROAM'
      when regexp_replace(coalesce(call_type,''),'[[:space:]]+','','g') = ''
           and data_source in ('drive_ocr_layout_v1','drive_ocr_tesseract','app_ocr_individual')
        then 'KAKAO'
      else 'UNKNOWN'
    end as actual
  from fixture
)
select call_type, data_source, expected, actual, (expected=actual) as pass
from classified
order by data_source;
