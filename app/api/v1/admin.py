"""Operator endpoints (X-Admin-Token, same guard as the legacy admin routes)."""
from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.dependencies import get_maintenance
from app.schemas.documents import MaintenanceResult
from app.services.maintenance import Maintenance
from security import verify_admin_token

router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(verify_admin_token)])
MaintDep = Annotated[Maintenance, Depends(get_maintenance)]


@router.post("/reindex", status_code=202, response_model=MaintenanceResult,
             summary="Recreate the collection for the CURRENT embedding model and re-ingest every document")
def reindex(m: MaintDep):
    return m.reindex_all()


@router.post("/reconcile", status_code=202, response_model=MaintenanceResult,
             summary="Compare Chroma with Postgres; queue reindex jobs for documents with missing vectors")
def reconcile(m: MaintDep):
    return m.reconcile()
