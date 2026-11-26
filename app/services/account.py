"""Account deletion: vectors, files, then the auth user (whose `on delete cascade` removes every row).

Every step is safe to repeat and the auth user is deleted LAST, so a failure leaves an account that can simply
retry. Blocking code (call from a `def` route).
"""
import logging
from typing import Callable

from app.core.exceptions import AppError

logger = logging.getLogger("uvicorn.error")
SECURITY = logging.getLogger("uvicorn.error.security")


def delete_auth_user(supabase_url: str, service_role_key: str, user_id: str) -> None:
    """GoTrue admin API. A user that is already gone counts as success."""
    import httpx
    r = httpx.request("DELETE", f"{supabase_url.rstrip('/')}/auth/v1/admin/users/{user_id}",
                      headers={"Authorization": f"Bearer {service_role_key}", "apikey": service_role_key}, timeout=20)
    if r.status_code != 404:
        r.raise_for_status()


class AccountService:
    def __init__(self, repo, store, storage, bucket: str, delete_user: Callable[[str], None]):
        self.repo, self.store, self.storage, self.bucket, self._delete_user = repo, store, storage, bucket, delete_user

    def delete(self, user_id: str) -> None:
        try:
            self.repo.mark_all_deleting(user_id)   # hides documents; a running ingestion aborts at commit
            self.store.delete_user(user_id)
            self._remove_files(user_id)
            self._delete_user(user_id)             # LAST
        except AppError as e:
            logger.error("account deletion failed: %s", e.code)
            raise self._incomplete() from None
        except Exception:
            logger.exception("account deletion failed")
            raise self._incomplete() from None
        logger.info("account deleted user=%s", user_id)

    @staticmethod
    def _incomplete():
        return AppError("account_deletion_incomplete", "The account could not be fully deleted; please try again", 503)

    def _remove_files(self, user_id: str) -> None:
        prefix, done = f"{user_id}/", set()
        for _ in range(20):  # the listing may be paged by the server
            names = []
            for name in self.repo.list_user_objects(user_id):
                if not name.startswith(prefix) or ".." in name.split("/"):
                    SECURITY.error("account deletion: refusing to remove an object outside the user's folder")
                    continue
                if name not in done:
                    names.append(name)
            if not names:
                return
            for name in names:
                self.storage.remove_object(self.bucket, name)
                done.add(name)
        raise RuntimeError("too many objects to remove in one request")


def process_deletion_requests(repo, service: AccountService) -> int:
    """Deletes the accounts that asked for it from the app. A failure keeps the request for the next pass
    (deleting the auth user cascades to the request row)."""
    done = 0
    for user_id in repo.list_deletion_requests():
        try:
            service.delete(user_id)
            done += 1
        except AppError as e:
            logger.error("requested account deletion will be retried: %s", e.code)
    return done
