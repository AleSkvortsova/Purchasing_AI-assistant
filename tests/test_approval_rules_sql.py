import json
import re
from decimal import Decimal
from pathlib import Path

from scripts.validate_approval_rules import load_rule_seed

SQL_005_PATH = Path("scripts/sql/005_approval_rule_engine.sql")
SQL_006_PATH = Path("scripts/sql/006_fix_approval_rule_ranges.sql")
RULES_PATH = Path("data/rules/approval_rules.json")

EXPECTED_BASE_CODES = {
    "BUDGETED_0_100000",
    "BUDGETED_100000_01_500000",
    "BUDGETED_500000_01_PLUS",
    "UNBUDGETED_0_100000",
    "UNBUDGETED_100000_01_PLUS",
}
OLD_TO_NEW_CODES = {
    "BUDGETED_100001_500000": "BUDGETED_100000_01_500000",
    "BUDGETED_500001_PLUS": "BUDGETED_500000_01_PLUS",
    "UNBUDGETED_100001_PLUS": "UNBUDGETED_100000_01_PLUS",
}


def _read_sql(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _assert_no_destructive_statements(sql: str) -> None:
    assert not re.search(r"\bdrop\s+table\b", sql, re.IGNORECASE)
    assert not re.search(r"\btruncate\b", sql, re.IGNORECASE)
    assert not re.search(r"\bdelete\s+from\b", sql, re.IGNORECASE)


def _simulate_code_migration(
    codes: set[str],
) -> tuple[set[str], set[str]]:
    """Model the code-branching semantics of migration 006."""
    migrated = set(codes)
    inactive: set[str] = set()
    for old_code, new_code in OLD_TO_NEW_CODES.items():
        if old_code in migrated and new_code not in migrated:
            migrated.remove(old_code)
            migrated.add(new_code)
        elif old_code in migrated:
            inactive.add(old_code)
    return migrated, inactive


def test_approval_rule_migration_005_is_safe_and_seeded() -> None:
    sql = _read_sql(SQL_005_PATH)

    assert "create table if not exists public.approval_base_rules" in sql
    assert "create table if not exists public.approval_additional_rules" in sql
    assert "on conflict (rule_code) do update" in sql
    assert "grant select on table public.approval_base_rules" in sql
    assert "grant select on table public.approval_additional_rules" in sql
    assert "from public, anon, authenticated" in sql
    _assert_no_destructive_statements(sql)


def test_005_and_json_use_the_canonical_base_rule_codes() -> None:
    sql = _read_sql(SQL_005_PATH)
    payload = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    json_codes = {
        rule["rule_code"] for rule in payload["base_rules"]
    }

    assert json_codes == EXPECTED_BASE_CODES
    for code in EXPECTED_BASE_CODES:
        assert f"'{code}'" in sql
    for old_code in OLD_TO_NEW_CODES:
        assert f"'{old_code}'" not in sql


def test_006_replaces_old_codes_without_unique_conflicts() -> None:
    sql = _read_sql(SQL_006_PATH)

    assert sql.strip().startswith("begin;")
    assert sql.strip().endswith("commit;")
    for old_code, new_code in OLD_TO_NEW_CODES.items():
        assert f"rule_code = '{old_code}'" in sql
        assert f"rule_code = '{new_code}'" in sql
        assert (
            f"set rule_code = '{new_code}'"
            in sql
        )
        assert re.search(
            rf"rule_code = '{re.escape(old_code)}'"
            rf"\s*\)\s+and not exists \("
            rf".*?rule_code = '{re.escape(new_code)}'",
            sql,
            re.DOTALL,
        )

    _assert_no_destructive_statements(sql)


def test_006_sets_one_kopeck_range_boundaries() -> None:
    sql = _read_sql(SQL_006_PATH)

    assert "min_amount = 100000.01" in sql
    assert "max_amount = 500000.00" in sql
    assert "min_amount = 500000.01" in sql
    assert re.search(
        r"min_amount = 100000\.01,\s+max_amount = null,"
        r".*?UNBUDGETED_100000_01_PLUS",
        sql,
        re.DOTALL,
    )
    assert re.search(
        r"min_amount = 500000\.01,\s+max_amount = null,"
        r".*?SINGLE_SUPPLIER_OVER_500000",
        sql,
        re.DOTALL,
    )


def test_006_is_idempotent_for_old_new_and_mixed_states() -> None:
    old_codes = set(OLD_TO_NEW_CODES)
    new_codes = set(OLD_TO_NEW_CODES.values())

    migrated_old, inactive_old = _simulate_code_migration(old_codes)
    rerun_old, rerun_inactive_old = _simulate_code_migration(migrated_old)
    assert migrated_old == new_codes
    assert inactive_old == set()
    assert rerun_old == migrated_old
    assert rerun_inactive_old == set()

    migrated_new, inactive_new = _simulate_code_migration(new_codes)
    rerun_new, rerun_inactive_new = _simulate_code_migration(migrated_new)
    assert migrated_new == new_codes
    assert inactive_new == set()
    assert rerun_new == migrated_new
    assert rerun_inactive_new == set()

    mixed_codes = old_codes | new_codes
    migrated_mixed, inactive_mixed = _simulate_code_migration(
        mixed_codes
    )
    assert migrated_mixed == mixed_codes
    assert inactive_mixed == old_codes
    assert len(migrated_mixed) == len(mixed_codes)


def test_006_refreshes_additional_rule_without_duplication() -> None:
    sql = _read_sql(SQL_006_PATH)

    assert "update public.approval_additional_rules" in sql
    assert "where rule_code = 'SINGLE_SUPPLIER_OVER_500000'" in sql
    assert not re.search(
        r"insert\s+into\s+public\.approval_additional_rules",
        sql,
        re.IGNORECASE,
    )


def test_base_rule_ranges_are_contiguous_to_one_kopeck() -> None:
    _, base_rules, _ = load_rule_seed()

    for budget_status in ("budgeted", "unbudgeted"):
        rules = sorted(
            (
                rule
                for rule in base_rules
                if rule.budget_status == budget_status
            ),
            key=lambda rule: rule.min_amount,
        )
        assert rules[0].min_amount == Decimal("0.00")
        for previous, current in zip(
            rules,
            rules[1:],
            strict=False,
        ):
            assert previous.max_amount is not None
            assert (
                current.min_amount
                == previous.max_amount + Decimal("0.01")
            )
        assert rules[-1].max_amount is None
