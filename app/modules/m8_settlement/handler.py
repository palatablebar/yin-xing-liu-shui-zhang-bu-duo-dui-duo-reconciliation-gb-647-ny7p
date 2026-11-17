"""净额结算入口：导入、执行、查询、版本恢复与 CSV 下载。"""
from __future__ import annotations

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import Response

from ...errors import DomainError
from ..m1_ingest.service import resolve_sample
from . import service

router = APIRouter(prefix="/api/settlements", tags=["结算"])


@router.post("/import")
async def import_inputs(
    request: Request,
    side: str = Form("vouchers"),
    sample: str = Form(""),
    file: UploadFile | None = File(None),
):
    if side not in ("vouchers", "receipts"):
        raise DomainError("E_SIDE", "side 只能是 vouchers 或 receipts", 400)
    settings = request.app.state.settings
    if file is not None and file.filename:
        raw = await file.read()
        filename = file.filename
    elif sample:
        filename, raw = resolve_sample(settings, sample)
    else:
        raise DomainError("E_INPUT", "需要上传文件或指定样例", 400)
    return service.import_side(settings, request.app.state.store, side, filename, raw)


@router.post("/apply")
def apply_settlement(request: Request):
    return service.apply(request.app.state.settings, request.app.state.store)


@router.post("/demo")
def demo_settlement(request: Request):
    return service.demo(request.app.state.settings, request.app.state.store)


@router.get("")
def get_settlement(request: Request):
    return service.payload(request.app.state.store)


@router.get("/versions")
def get_versions(request: Request):
    return {"versions": service.payload(request.app.state.store)["versions"]}


@router.post("/restore/{batch_id}")
def restore_version(request: Request, batch_id: str):
    return service.restore(request.app.state.store, batch_id)


@router.get("/export.csv")
def get_settlement_csv(request: Request):
    text = service.csv_text(request.app.state.store)
    return Response(
        content=text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="settlement.csv"'},
    )
