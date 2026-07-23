begin;

-- Each block supports:
-- 1. an old 005 row that must be renamed;
-- 2. an already-corrected 005 row that only needs an idempotent refresh;
-- 3. a defensive mixed state without violating unique(rule_code).

do $budgeted_middle$
begin
    if exists (
        select 1 from public.approval_base_rules
        where rule_code = 'BUDGETED_100001_500000'
    ) and not exists (
        select 1 from public.approval_base_rules
        where rule_code = 'BUDGETED_100000_01_500000'
    ) then
        update public.approval_base_rules
        set rule_code = 'BUDGETED_100000_01_500000'
        where rule_code = 'BUDGETED_100001_500000';
    elsif exists (
        select 1 from public.approval_base_rules
        where rule_code = 'BUDGETED_100001_500000'
    ) then
        update public.approval_base_rules
        set
            is_active = false,
            description = 'Устаревшее правило; заменено BUDGETED_100000_01_500000.'
        where rule_code = 'BUDGETED_100001_500000';
    end if;

    update public.approval_base_rules
    set
        min_amount = 100000.01,
        max_amount = 500000.00,
        description = 'Бюджетная закупка свыше 100 000 и до 500 000 рублей включительно.'
    where rule_code = 'BUDGETED_100000_01_500000';
end;
$budgeted_middle$;

do $budgeted_upper$
begin
    if exists (
        select 1 from public.approval_base_rules
        where rule_code = 'BUDGETED_500001_PLUS'
    ) and not exists (
        select 1 from public.approval_base_rules
        where rule_code = 'BUDGETED_500000_01_PLUS'
    ) then
        update public.approval_base_rules
        set rule_code = 'BUDGETED_500000_01_PLUS'
        where rule_code = 'BUDGETED_500001_PLUS';
    elsif exists (
        select 1 from public.approval_base_rules
        where rule_code = 'BUDGETED_500001_PLUS'
    ) then
        update public.approval_base_rules
        set
            is_active = false,
            description = 'Устаревшее правило; заменено BUDGETED_500000_01_PLUS.'
        where rule_code = 'BUDGETED_500001_PLUS';
    end if;

    update public.approval_base_rules
    set
        min_amount = 500000.01,
        max_amount = null,
        description = 'Бюджетная закупка свыше 500 000 рублей.'
    where rule_code = 'BUDGETED_500000_01_PLUS';
end;
$budgeted_upper$;

do $unbudgeted_upper$
begin
    if exists (
        select 1 from public.approval_base_rules
        where rule_code = 'UNBUDGETED_100001_PLUS'
    ) and not exists (
        select 1 from public.approval_base_rules
        where rule_code = 'UNBUDGETED_100000_01_PLUS'
    ) then
        update public.approval_base_rules
        set rule_code = 'UNBUDGETED_100000_01_PLUS'
        where rule_code = 'UNBUDGETED_100001_PLUS';
    elsif exists (
        select 1 from public.approval_base_rules
        where rule_code = 'UNBUDGETED_100001_PLUS'
    ) then
        update public.approval_base_rules
        set
            is_active = false,
            description = 'Устаревшее правило; заменено UNBUDGETED_100000_01_PLUS.'
        where rule_code = 'UNBUDGETED_100001_PLUS';
    end if;

    update public.approval_base_rules
    set
        min_amount = 100000.01,
        max_amount = null,
        description = 'Внебюджетная закупка свыше 100 000 рублей.'
    where rule_code = 'UNBUDGETED_100000_01_PLUS';
end;
$unbudgeted_upper$;

-- No insert is used here: the existing additional rule is refreshed in place,
-- so repeated runs cannot create a duplicate.
update public.approval_additional_rules
set
    min_amount = 500000.01,
    max_amount = null,
    description = 'Единственный поставщик при сумме свыше 500 000 рублей.'
where rule_code = 'SINGLE_SUPPLIER_OVER_500000';

commit;
