-- Read-only audit for legacy procurement_type = 'work'.
-- This statement does not modify requests or lifecycle snapshots.
select
    count(*) filter (
        where data #>> '{intake,draft,procurement_type}' = 'work'
    ) as canonical_intake_draft_work_count,
    count(*) filter (
        where data ->> 'procurement_type' = 'work'
    ) as legacy_procurement_type_work_count,
    count(*) filter (
        where request_type = 'work'
           or data ->> 'request_type' = 'work'
    ) as request_type_work_count,
    count(*) filter (
        where (
            data #>> '{intake,draft,procurement_type}' = 'work'
            or data ->> 'procurement_type' = 'work'
        )
          and request_type = 'service'
    ) as work_rows_projected_as_service_count
from public.requests;
