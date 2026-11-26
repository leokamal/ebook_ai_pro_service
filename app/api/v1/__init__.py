from fastapi import APIRouter

from app.api.v1 import admin, chat, conversations, documents, health, me, search
from app.schemas.errors import ErrorResponse

_ERR = {"model": ErrorResponse}
router = APIRouter(responses={
    401: {**_ERR, "description": "Missing, invalid or expired access token"},
    403: {**_ERR, "description": "`provider_not_allowed` (or a bad admin token)"},
    422: {**_ERR, "description": "Validation error"},
    429: {**_ERR, "description": "Rate limit or quota exceeded (see Retry-After)"},
    500: {**_ERR, "description": "Unexpected server error"},
})
router.include_router(health.router)
router.include_router(me.router)
router.include_router(documents.router)
router.include_router(conversations.router)
router.include_router(chat.router)
router.include_router(search.router)
router.include_router(admin.router)
