from typing import Annotated

from fastapi import APIRouter, Depends, Response

from app.core.dependencies import CurrentUserDep, get_account_repo, get_account_service
from app.repositories.account import AccountRepository
from app.schemas.me import AccountDeleteRequest, MeResponse
from app.services.account import AccountService

router = APIRouter(prefix="/me", tags=["account"])


@router.get("", response_model=MeResponse, summary="Who am I, and how much of my space is used")
def get_me(user: CurrentUserDep, repo: Annotated[AccountRepository, Depends(get_account_repo)]):
    usage, limits = repo.usage(user.id), repo.limits()  # the id comes only from the verified token
    profile = repo.profile(user.id)  # display data; the token is the fallback
    hidden = bool(profile.get("avatar_hidden"))
    return {"id": user.id, "generated_key": profile.get("generated_key"),
            "email": profile.get("email") or user.email,
            "name": profile.get("display_name") or user.name, "full_name": profile.get("full_name") or user.name,
            "avatar_url": None if hidden else profile.get("avatar_url") or user.avatar_url, "avatar_hidden": hidden,
            "country": profile.get("country"), "gender": profile.get("gender"),
            "providers": list(user.providers),
            "usage": {"documents": repo.document_count(user.id), "storage_bytes": usage["bytes"],
                      "storage_limit_bytes": limits["max_storage_bytes"], "objects": usage["objects"],
                      "object_limit": limits["max_objects"]}}


@router.delete("", status_code=204, summary="Delete my account and all my data (irreversible)",
               responses={503: {"description": "`account_deletion_incomplete`: nothing is lost, retry"}})
def delete_me(body: AccountDeleteRequest, user: CurrentUserDep,
              account: Annotated[AccountService, Depends(get_account_service)]):
    account.delete(user.id)
    return Response(status_code=204)
