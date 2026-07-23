from fastapi import APIRouter

from app.api.approval_rules import router as approval_rules_router
from app.api.database import router as database_router
from app.api.rag import router as rag_router
from app.api.requests import router as requests_router

api_router = APIRouter()
api_router.include_router(approval_rules_router)
api_router.include_router(database_router)
api_router.include_router(requests_router)
api_router.include_router(rag_router)
