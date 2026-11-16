"""M4 采信裁决与可恢复发布。

决定保存与发布分两个事务：
1. 裁决事务只追加决定行（PENDING），并校验版本；提交后决定即已留档。
2. 发布事务切换当前采纳页（页序缺口与扫描件四账都由它推导），
   并把决定置为 PUBLISHED。
发布中途失败时第 2 个事务整体回滚：决定仍是 PENDING，页面仍是原采纳页；
同一 request_id 重试只重放这次发布，不会新增决定。
"""
import uuid

from app.errors import DomainError
from app.reasons import REASONS
from . import repository as repo
from . import publish
from .intake import live_state

ADOPT_ACTIONS = {"ADOPT", "REVIEW_APPROVE"}
VALID_ACTIONS = ADOPT_ACTIONS | {"REJECT"}
MAX_REASON = 400


def _fail(code: str, status: int = 422, **details):
    raise DomainError(code, REASONS.get(code, code), status=status, details=details)


def _clean_text(value, limit=MAX_REASON) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _validate_payload(payload) -> dict:
    if not isinstance(payload, dict):
        _fail("BAD_REQUEST", status=400)
    request = {
        "business_key": _clean_text(payload.get("business_key"), 80),
        "scan_id": _clean_text(payload.get("scan_id"), 80),
        "action": (_clean_text(payload.get("action"), 20) or "ADOPT").upper(),
        "actor": _clean_text(payload.get("actor"), 60) or "质检员",
        "reason_text": _clean_text(payload.get("reason_text")),
        "request_id": _clean_text(payload.get("request_id"), 80) or None,
        "expected_revision": payload.get("expected_revision"),
    }
    if not request["business_key"] or not request["scan_id"]:
        _fail("BAD_REQUEST", status=400, field="business_key/scan_id")
    if request["action"] not in VALID_ACTIONS:
        _fail("BAD_REQUEST", status=400, field="action")
    revision = request["expected_revision"]
    if isinstance(revision, bool) or not isinstance(revision, int):
        _fail("BAD_REQUEST", status=400, field="expected_revision")
    if request["action"] == "REVIEW_APPROVE" and not request["reason_text"]:
        _fail("BAD_REQUEST", status=400, field="reason_text", detail="复核采信必须写明采信理由")
    return request


def _check_target(db, request: dict) -> tuple[dict, dict]:
    group = repo.get_group(db, request["business_key"])
    if group is None:
        _fail("UNKNOWN_REFERENCE", status=404, business_key=request["business_key"])
    scan = repo.get_scan(db, request["scan_id"])
    if scan is None or scan["business_key"] != group["business_key"]:
        _fail("UNKNOWN_SCAN", status=404, scan_id=request["scan_id"])
    if request["action"] == "ADOPT":
        # 普通采信不能越过页眉证据；页眉缺失或不符只能走复核采信。
        if scan["header_verified"] == 0:
            _fail("HEADER_MISSING", status=409, scan_id=scan["scan_id"], review_required=True)
        if scan["header_verified"] == -1:
            _fail("HEADER_MISMATCH", status=409, scan_id=scan["scan_id"], review_required=True)
    return group, scan


def _fill_reason(db, request: dict, group: dict, scan: dict) -> tuple[str, str]:
    """采信理由：理由码由证据关系决定，理由文本优先用裁决人填写的内容。"""
    if request["action"] == "REJECT":
        return "REJECTED", request["reason_text"] or "复核决定不采信该扫描件"
    if request["action"] == "REVIEW_APPROVE":
        return "REVIEW_APPROVED", request["reason_text"] or "复核确认页眉与页码后采信"
    current = repo.get_scan(db, group["adopted_scan_id"]) if group.get("adopted_scan_id") else None
    if current is None:
        code, default = "BASELINE", "首次采信该页扫描件"
    elif current["sha256"] and current["sha256"] == scan["sha256"]:
        code, default = "EXACT_COPY", "与原扫描内容一致，按别名保留"
    elif int(scan["clarity"]) > int(current["clarity"]):
        code, default = "CLEARER_SCAN", f"清晰度 {scan['clarity']} 高于当前采纳页 {current['clarity']}"
    else:
        code, default = "MANUAL_OVERRIDE", f"人工指定：清晰度 {scan['clarity']}，当前采纳页 {current['clarity']}"
    return code, request["reason_text"] or default


def _publish(db, decision: dict) -> str:
    """在调用方的事务里把 PENDING 决定落成当前采纳页。"""
    group = repo.get_group(db, decision["business_key"])
    resolved_at = repo.now_iso()
    if group is None or group["revision"] >= decision["revision"]:
        # 世界已经前进到这条决定之后，它不再生效。
        repo.mark_decision_superseded(db, decision["decision_id"], resolved_at)
        return "SUPERSEDED"
    if group["revision"] != decision["previous_revision"]:
        _fail("STALE_VERSION", status=409, expected_revision=decision["previous_revision"], current_revision=group["revision"])

    if decision["action"] == "REJECT":
        # 撤回当前采纳页：该页回到缺页，缺口随发布一起出现。
        adopted = None if group["adopted_scan_id"] == decision["scan_id"] else group["adopted_scan_id"]
    else:
        adopted = decision["scan_id"]
    if not repo.update_adoption(db, decision["business_key"], expected_revision=decision["previous_revision"], scan_id=adopted, new_revision=decision["revision"], updated_at=resolved_at):
        _fail("STALE_VERSION", status=409, expected_revision=decision["previous_revision"])
    # 成功发布计数与状态迁移同事务提交；失败的尝试由调用方单独记入。
    repo.mark_decision_published(db, decision["decision_id"], resolved_at)
    # 故障点：落库后、提交前的发布中断。抛出异常让整个发布事务回滚。
    publish.tick()
    return "PUBLISHED"


def _note_failed_attempt(db, decision_id: str):
    """发布中断后单独记一次尝试；这只记录诊断信息，不产生新决定。"""
    try:
        with repo.write(db):
            repo.note_publish_attempt(db, decision_id)
    except Exception:
        pass


def _decision_payload(db, decision: dict) -> dict:
    group = repo.get_group(db, decision["business_key"])
    state = live_state(db)
    row = next((item for item in state["groups"] if item["business_key"] == decision["business_key"]), None)
    return {
        "decision_id": decision["decision_id"],
        "request_id": decision["request_id"],
        "business_key": decision["business_key"],
        "action": decision["action"],
        "revision": decision["revision"],
        "previous_revision": decision["previous_revision"],
        "scan_id": decision["scan_id"],
        "reason_code": decision["reason_code"],
        "reason_text": decision["reason_text"],
        "actor": decision["actor"],
        "decided_at": decision["decided_at"],
        "publish_state": decision["publish_state"],
        "published_at": decision["published_at"],
        "publish_attempts": decision["publish_attempts"],
        "current_revision": group["revision"] if group else None,
        "current_adopted_scan_id": group["adopted_scan_id"] if group else None,
        "group": row,
        "history": [
            {"revision": item["revision"], "action": item["action"], "scan_id": item["scan_id"], "reason_code": item["reason_code"], "reason_text": item["reason_text"], "actor": item["actor"], "decided_at": item["decided_at"], "publish_state": item["publish_state"]}
            for item in repo.list_decisions(db, decision["business_key"])
        ],
    }


def _publish_saved(db, decision: dict) -> dict:
    try:
        with repo.write(db):
            _publish(db, decision)
    except publish.PublishInterrupted:
        _note_failed_attempt(db, decision["decision_id"])
        _fail(
            "PUBLISH_INTERRUPTED",
            status=503,
            decision_id=decision["decision_id"],
            publish_state="PENDING",
            retryable=True,
            decision=_decision_payload(db, repo.get_decision(db, decision["decision_id"])),
            page=_page_view(db),
        )
    return _decision_payload(db, repo.get_decision(db, decision["decision_id"]))


def _page_view(db) -> dict:
    state = live_state(db)
    return {"groups": state["groups"], "gaps": state["gaps"], "pending": state["pending"]}


def decide(payload) -> dict:
    request = _validate_payload(payload)
    db = repo.connect()
    try:
        if request["request_id"]:
            existing = repo.get_decision_by_request(db, request["request_id"])
            if existing is not None:
                return _resume(db, existing)

        with repo.write(db):
            group, scan = _check_target(db, request)

            pending = repo.pending_decision(db, group["business_key"])
            if pending is not None:
                # 先把他人的中断发布恢复；失败则本次裁决让路，页面保持原采纳页。
                try:
                    _publish(db, pending)
                except publish.PublishInterrupted:
                    _fail("PUBLISH_INTERRUPTED", status=503, decision_id=pending["decision_id"], publish_state="PENDING", blocked_by="PENDING_DECISION", retryable=True)
                group = repo.get_group(db, request["business_key"])

            if group["revision"] != request["expected_revision"]:
                _fail(
                    "STALE_VERSION",
                    status=409,
                    expected_revision=request["expected_revision"],
                    current_revision=group["revision"],
                    current_adopted_scan_id=group["adopted_scan_id"],
                )
            if request["action"] in ADOPT_ACTIONS and group.get("adopted_scan_id") == request["scan_id"]:
                _fail("ALREADY_ADOPTED", status=409, scan_id=request["scan_id"])

            reason_code, reason_text = _fill_reason(db, request, group, scan)
            decision = {
                "decision_id": "DEC-" + uuid.uuid4().hex[:20],
                "request_id": request["request_id"],
                "business_key": group["business_key"],
                "action": request["action"],
                "revision": group["revision"] + 1,
                "previous_revision": group["revision"],
                "scan_id": request["scan_id"],
                "reason_code": reason_code,
                "reason_text": reason_text,
                "actor": request["actor"],
                "decided_at": repo.now_iso(),
                "publish_state": "PENDING",
                "published_at": None,
                "publish_attempts": 0,
                "batch_id": None,
            }
            repo.insert_decision(db, decision)
        # 裁决事务已提交：决定从此留档，重试同一 request_id 只重放发布。
        return _publish_saved(db, decision)
    finally:
        db.close()


def _resume(db, existing: dict) -> dict:
    if existing["publish_state"] != "PENDING":
        return _decision_payload(db, existing)
    return _publish_saved(db, existing)


def resume(decision_id: str) -> dict:
    db = repo.connect()
    try:
        existing = repo.get_decision(db, decision_id)
        if existing is None:
            _fail("UNKNOWN_REFERENCE", status=404, decision_id=decision_id)
        return _resume(db, existing)
    finally:
        db.close()


def recover_all() -> list[dict]:
    """启动时尽力恢复中断的发布；仍失败的保留 PENDING 供页面重试。"""
    db = repo.connect()
    results = []
    try:
        for pending in repo.list_decisions(db):
            if pending["publish_state"] != "PENDING":
                continue
            try:
                with repo.write(db):
                    state = _publish(db, pending)
                results.append({"decision_id": pending["decision_id"], "state": state})
            except publish.PublishInterrupted:
                _note_failed_attempt(db, pending["decision_id"])
                results.append({"decision_id": pending["decision_id"], "state": "PENDING"})
            except DomainError as exc:
                # 单条恢复失败不能阻断启动；决定保留 PENDING 供页面重试。
                results.append({"decision_id": pending["decision_id"], "state": "PENDING", "reason_code": exc.code})
    finally:
        db.close()
    return results
