from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query, Response

from app.core.dependencies import CurrentUserDep, get_conversations_repo
from app.repositories.conversations import ConversationRepository
from app.schemas.conversations import (ConversationCreate, ConversationOut, ConversationPage, ConversationUpdate,
                                       MessagePage)

router = APIRouter(prefix="/conversations", tags=["conversations"])
ConvsDep = Annotated[ConversationRepository, Depends(get_conversations_repo)]
_ERR = {401: {"description": "Missing/invalid token"}, 404: {"description": "Not found (or not yours)"}}


def _uuid(value: str) -> str:
    import uuid
    from app.core.exceptions import NotFound
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise NotFound("Conversation not found") from None


@router.post("", response_model=ConversationOut, status_code=201, responses=_ERR)
def create_conversation(body: ConversationCreate, user: CurrentUserDep, convs: ConvsDep):
    """`single_document` requires `document_id` (must be yours); `all_documents` forbids it."""
    return convs.create(user.id, body.mode, str(body.document_id) if body.document_id else None, body.title)


@router.get("", response_model=ConversationPage)
def list_conversations(user: CurrentUserDep, convs: ConvsDep, limit: Annotated[int, Query(ge=1, le=100)] = 20,
                       cursor: Optional[str] = None):
    return convs.list(limit, cursor)


@router.get("/{conversation_id}", response_model=ConversationOut, responses=_ERR)
def get_conversation(conversation_id: str, user: CurrentUserDep, convs: ConvsDep):
    return convs.get(_uuid(conversation_id))


@router.patch("/{conversation_id}", response_model=ConversationOut, responses=_ERR)
def rename_conversation(conversation_id: str, body: ConversationUpdate, user: CurrentUserDep, convs: ConvsDep):
    return convs.update_title(_uuid(conversation_id), body.title)


@router.delete("/{conversation_id}", status_code=204, responses=_ERR)
def delete_conversation(conversation_id: str, user: CurrentUserDep, convs: ConvsDep):
    convs.delete(_uuid(conversation_id))
    return Response(status_code=204)


@router.get("/{conversation_id}/messages", response_model=MessagePage, responses=_ERR)
def list_messages(conversation_id: str, user: CurrentUserDep, convs: ConvsDep,
                  limit: Annotated[int, Query(ge=1, le=100)] = 20, cursor: Optional[str] = None):
    """Newest first (keyset pagination); each assistant message carries its `sources`."""
    return convs.list_messages(_uuid(conversation_id), limit, cursor)
