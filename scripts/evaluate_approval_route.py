import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pydantic import ValidationError  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.rules.exceptions import ApprovalRuleError  # noqa: E402
from app.rules.models import ApprovalContext  # noqa: E402
from app.rules.repository import SupabaseApprovalRuleRepository  # noqa: E402
from app.rules.service import ApprovalRuleService  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate approval route")
    parser.add_argument("--amount", required=True)
    parser.add_argument(
        "--budget-status",
        choices=("budgeted", "unbudgeted"),
    )
    parser.add_argument("--urgency", choices=("P1", "P2", "P3", "P4"))
    parser.add_argument("--single-supplier", action="store_true")
    parser.add_argument("--category-code")
    parser.add_argument("--has-data-access", action="store_true")
    parser.add_argument("--work-on-site", action="store_true")
    parser.add_argument("--date", dest="evaluation_date")
    parser.add_argument("--json", action="store_true")
    return parser


def build_service() -> ApprovalRuleService:
    settings = get_settings()
    if not settings.supabase_configured:
        raise ApprovalRuleError("Supabase is not configured")
    assert settings.supabase_url is not None
    assert settings.supabase_service_role_key is not None
    repository = SupabaseApprovalRuleRepository.from_credentials(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )
    return ApprovalRuleService(repository)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        context = ApprovalContext(
            amount=args.amount,
            budget_status=args.budget_status,
            urgency=args.urgency,
            single_supplier=args.single_supplier,
            category_code=args.category_code,
            has_data_access=args.has_data_access,
            work_on_site=args.work_on_site,
            evaluation_date=args.evaluation_date,
        )
        result = build_service().evaluate(context)
    except (ApprovalRuleError, ValidationError) as exc:
        print(f"Approval route evaluation failed: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(
            json.dumps(
                result.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0

    print(f"status: {result.status}")
    print(f"base rule: {result.base_rule_code or '-'}")
    print(
        "additional rules: "
        + (", ".join(result.applied_additional_rule_codes) or "-")
    )
    print("route: " + (", ".join(result.final_approvers) or "-"))
    print("missing fields: " + (", ".join(result.missing_fields) or "-"))
    print("warnings: " + ("; ".join(result.warnings) or "-"))
    print("sources:")
    for source in result.source_references:
        print(
            f"  {source.document_id} > {source.section} "
            f"({source.rule_code})"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
