import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.extraction.exceptions import (  # noqa: E402
    ApprovalExtractionError,
    ApprovalExtractionProviderError,
)
from app.extraction.provider import (  # noqa: E402
    OpenAIApprovalExtractionProvider,
    RuleBasedApprovalExtractionProvider,
)
from app.extraction.service import (  # noqa: E402
    ApprovalContextExtractionService,
    ApprovalEvaluationOrchestrator,
)
from app.rules.repository import SupabaseApprovalRuleRepository  # noqa: E402
from app.rules.service import ApprovalRuleService  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Extract structured approval context from Russian text."
    )
    parser.add_argument("text")
    parser.add_argument(
        "--provider",
        choices=("openai", "rule-based"),
        default=None,
    )
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--show-evidence", action="store_true")
    parser.add_argument("--model")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show safe provider diagnostics without prompts or credentials.",
    )
    return parser


def build_extraction_service(
    provider_name: str,
    model: str | None = None,
) -> ApprovalContextExtractionService:
    settings = get_settings()
    if provider_name == "rule-based":
        provider = RuleBasedApprovalExtractionProvider()
    else:
        selected_model = (
            model
            if model is not None
            else settings.approval_extraction_model
        )
        provider = OpenAIApprovalExtractionProvider(
            api_key=settings.openai_api_key,
            model=selected_model,
            timeout_seconds=settings.approval_extraction_timeout_seconds,
            max_retries=settings.approval_extraction_max_retries,
        )
    return ApprovalContextExtractionService(
        provider,
        min_confidence=settings.approval_extraction_min_confidence,
    )


def build_approval_service() -> ApprovalRuleService:
    settings = get_settings()
    if not settings.supabase_configured:
        raise ApprovalExtractionError(
            "Supabase approval rule repository is not configured"
        )
    assert settings.supabase_url is not None
    assert settings.supabase_service_role_key is not None
    repository = SupabaseApprovalRuleRepository.from_credentials(
        settings.supabase_url,
        settings.supabase_service_role_key,
    )
    return ApprovalRuleService(repository)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    provider_name = args.provider or settings.approval_extraction_provider
    provider_name = provider_name.replace("_", "-")
    try:
        extraction_service = build_extraction_service(
            provider_name,
            args.model,
        )
        if args.evaluate:
            result = ApprovalEvaluationOrchestrator(
                extraction_service,
                build_approval_service(),
            ).extract_and_evaluate(args.text)
        else:
            result = extraction_service.extract(args.text)
    except ApprovalExtractionError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if args.debug:
            if isinstance(exc, ApprovalExtractionProviderError):
                diagnostics = {
                    "error_type": exc.error_type,
                    "status_code": exc.status_code,
                    "error_code": exc.error_code,
                    "error_param": exc.error_param,
                    "request_id": exc.request_id,
                    "response_status": exc.response_status,
                    "incomplete_reason": exc.incomplete_reason,
                    "validation_errors": exc.validation_errors,
                }
            else:
                diagnostics = {
                    "error_type": type(exc).__name__,
                    "status_code": None,
                    "error_code": None,
                    "error_param": None,
                    "request_id": None,
                    "response_status": None,
                    "incomplete_reason": None,
                    "validation_errors": None,
                }
            for name, value in diagnostics.items():
                rendered = (
                    json.dumps(value, ensure_ascii=False)
                    if isinstance(value, list)
                    else "null"
                    if value is None
                    else str(value)
                )
                print(f"{name}: {rendered}", file=sys.stderr)
        return 1

    payload = result.model_dump(mode="json")
    if not args.show_evidence:
        extraction = (
            payload.get("extraction")
            or payload.get("extraction_result", {}).get("extraction")
        )
        if extraction is not None:
            extraction.pop("evidence_by_field", None)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        extraction_result = getattr(result, "extraction_result", result)
        print(f"Status: {extraction_result.status}")
        print(
            extraction_result.extraction.model_dump_json(
                indent=2,
                exclude={"evidence_by_field"} if not args.show_evidence else None,
            )
        )
        route = getattr(result, "approval_route_result", None)
        if route is not None:
            print(f"Route status: {route.status}")
            print(f"Approvers: {', '.join(route.final_approvers)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
