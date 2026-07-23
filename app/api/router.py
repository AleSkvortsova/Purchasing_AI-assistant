from fastapi import APIRouter

from app.api.database import router as database_router
from app.api.requests import router as requests_router

api_router = APIRouter()
api_router.include_router(database_router)
api_router.include_router(requests_router)
