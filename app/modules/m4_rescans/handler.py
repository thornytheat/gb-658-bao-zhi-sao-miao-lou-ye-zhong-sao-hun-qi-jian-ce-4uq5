from fastapi import APIRouter, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, PlainTextResponse
from app.errors import DomainError
from app.reasons import REASONS
from app.web import page
from .service import demo, overview
from .intake import import_manifest
from .adjudication import decide, resume
from . import repository as repo

router = APIRouter()


@router.get("/rescans", response_class=HTMLResponse)
def view(request: Request):
    state = overview()
    model = _view_model(state)
    return page(request, "m4.html", "重扫采信", "/api/rescans/decide", model["rows"],
                group_rows=model["groups"], candidates=model["candidates"], gaps=state["gaps"],
                accounts=state["accounts"], pending=state["pending"], batches=state["batches"],
                decision_rows=model["decisions"])


@router.post("/api/rescans/decide")
def adjudicate(payload: dict):
    return decide(payload)


@router.post("/api/rescans/import")
async def import_scan_manifest(request: Request, file: UploadFile | None = File(default=None), text: str | None = Form(default=None)):
    if file is not None:
        raw = await file.read()
    elif text is not None:
        raw = text.encode("utf-8")
    else:
        body = await request.body()
        if not body:
            raise DomainError("BAD_REQUEST", "请上传清单文件或提供清单文本", status=400)
        raw = body
    try:
        source = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DomainError("BAD_FORMAT", "清单不是 UTF-8 文本", status=422) from exc
    return {"status": "ok", **import_manifest(source, raw=raw)}


@router.get("/api/rescans/state")
def state():
    return overview()


@router.post("/api/rescans/decisions/{decision_id}/publish")
def publish_decision(decision_id: str):
    return resume(decision_id)


@router.get("/api/rescans/batches/{batch_id}")
def batch_snapshot(batch_id: str):
    import json
    db = repo.connect()
    try:
        batch = repo.get_batch(db, batch_id)
        if batch is None:
            raise DomainError("UNKNOWN_REFERENCE", "归档批次不存在", status=404)
        snapshot = json.loads(batch["snapshot"])
        scans = repo.list_scans(db)
        scan_by_id = {scan["scan_id"]: scan for scan in scans}
        archived_adopted = {
            group["business_key"]: (group["adopted"] or {}).get("scan_id")
            for group in snapshot["groups"]
        }
        current_adopted = {
            group["business_key"]: (group["adopted"] or {}).get("scan_id")
            for group in overview()["groups"]
        }
        rows = []
        for scan in snapshot["scans"]:
            key = scan["business_key"]
            archived_scan = archived_adopted.get(key)
            now_scan = current_adopted.get(key)
            current = scan_by_id.get(now_scan) or {}
            rows.append({
                "line_no": scan["line_no"],
                "business_key": key,
                "file_name": scan["file_name"],
                "clarity": scan["clarity"],
                "header_issue": scan["header_issue"] or "缺失",
                "archived_adopted": scan["scan_id"] == archived_scan,
                "current_adopted": scan["scan_id"] == now_scan,
                "changed": archived_scan != now_scan,
                "current_adopted_file": current.get("file_name"),
            })
        return {"batch": {key: batch[key] for key in ("batch_id", "created_at", "input_count", "accepted", "quarantined")}, "rows": rows, "evidence": snapshot["evidence"], "gaps": snapshot["gaps"], "accounts": snapshot["accounts"]}
    finally:
        db.close()


@router.get("/api/rescans/evidence.csv", response_class=PlainTextResponse)
def export_evidence():
    import csv
    import io
    db = repo.connect()
    try:
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["batch_id", "line_no", "business_key", "reason_code", "raw_value", "detail"])
        for item in repo.list_evidence(db):
            writer.writerow([item["batch_id"], item["line_no"], item["business_key"] or "", item["reason_code"], item["raw_value"] or "", item["detail"] or ""])
        return PlainTextResponse(output.getvalue(), media_type="text/csv", headers={"content-disposition": "attachment; filename=rescan-evidence.csv"})
    finally:
        db.close()


@router.post("/api/rescans/evaluate")
def evaluate():
    return demo()


def _view_model(state: dict) -> dict:
    groups, revision_by_key = [], {}
    for row in state["groups"]:
        adopted = row["adopted"] or {}
        original = row["original"] or {}
        revision_by_key[row["business_key"]] = row["revision"]
        groups.append({
            "business_key": row["business_key"],
            "issue": row["issue"],
            "edition": row["edition"],
            "page": row["page"],
            "revision": row["revision"],
            "status": row["status"],
            "reason": REASONS.get(row["reason_code"], row["reason_code"] or "—"),
            "adopted_file": adopted.get("file_name", "—"),
            "adopted_scan_id": adopted.get("scan_id"),
            "adopted_clarity": adopted.get("clarity", "—"),
            "original_file": original.get("file_name", "—"),
            "original_clarity": original.get("clarity", "—"),
            "changed": bool(original and adopted and original["scan_id"] != adopted["scan_id"]),
        })
    candidates = []
    for item in state["candidates"]:
        candidates.append({**item, "revision": revision_by_key.get(item["business_key"], 0)})
    decisions = sorted(state["decisions"], key=lambda item: (item["decided_at"], item["revision"]), reverse=True)
    rows = [{"key": group["business_key"], "status": group["status"], "value": group["revision"]} for group in groups]
    return {"groups": groups, "candidates": candidates, "decisions": decisions, "rows": rows}
