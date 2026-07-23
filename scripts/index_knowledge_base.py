import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.config import get_settings  # noqa: E402
from app.rag.embeddings import OpenAIEmbeddingProvider  # noqa: E402
from app.rag.exceptions import RagError  # noqa: E402
from app.rag.indexing_service import (  # noqa: E402
    KnowledgeIndexingService,
    load_processed_knowledge_data,
)
from app.rag.repository import SupabaseKnowledgeRepository  # noqa: E402

DOCUMENTS_PATH = PROJECT_ROOT / "data" / "processed" / "knowledge_documents.json"
CHUNKS_PATH = PROJECT_ROOT / "data" / "processed" / "knowledge_chunks.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Index the local knowledge base")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-reembed", action="store_true")
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--skip-delete", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    batch_size = args.batch_size or settings.embedding_batch_size
    if batch_size <= 0:
        print("--batch-size must be positive", file=sys.stderr)
        return 2

    try:
        documents, chunks = load_processed_knowledge_data(
            DOCUMENTS_PATH,
            CHUNKS_PATH,
        )
        if args.dry_run:
            print(f"Documents found: {len(documents)}")
            print(f"Chunks found: {len(chunks)}")
            print(f"Potential embeddings required: {len(chunks)}")
            print(f"Embedding model: {settings.embedding_model}")
            print(f"Embedding dimensions: {settings.embedding_dimensions}")
            print(f"Supabase configured: {settings.supabase_configured}")
            print(f"OpenAI configured: {settings.openai_configured}")
            return 0

        if not settings.supabase_configured:
            raise RagError("Supabase is not configured")
        assert settings.supabase_url is not None
        assert settings.supabase_service_role_key is not None
        repository = SupabaseKnowledgeRepository.from_credentials(
            settings.supabase_url,
            settings.supabase_service_role_key,
        )
        provider = OpenAIEmbeddingProvider(
            api_key=settings.openai_api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            batch_size=batch_size,
        )
        service = KnowledgeIndexingService(
            repository,
            provider,
            embedding_model=settings.embedding_model,
            documents_path=DOCUMENTS_PATH,
            chunks_path=CHUNKS_PATH,
        )
        report = service.index(
            force_reembed=args.force_reembed,
            skip_delete=args.skip_delete,
        )
        print(
            json.dumps(
                report.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    except RagError as exc:
        import traceback
        print(f"Indexing failed: {exc}", file=sys.stderr)
        traceback.print_exc()
        print("\nFULL TRACEBACK:")
        traceback.print_exc()

        print("\nDIRECT CAUSE:")
        if exc.__cause__ is not None:
            traceback.print_exception(
                type(exc.__cause__),
                exc.__cause__,
                exc.__cause__.__traceback__,
            )
        else:
            print("No direct cause found.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
