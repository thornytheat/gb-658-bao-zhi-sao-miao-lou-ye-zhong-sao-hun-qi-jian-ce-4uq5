"""M4 清单导入。

导入事务一次性完成：批次快照、扫描原件、新业务键的基线采纳、应有页数、
合规证据与扫描件四账。批次快照冻结在导入时刻，后续裁决只改当前采纳页，
归档批次始终展示原扫描与当时的缺页、混期证据。
"""
import json
from hashlib import sha256

from app.manifest import input_digest
from app.modules.m1_intake.integrity import complete_tail, valid_sha256
from app.modules.m1_intake.parser import parse_manifest
from app.modules.m2_issue.catalog import business_key
from app.modules.m2_issue.rules import normalize_edition, normalize_issue, normalize_page
from app.modules.m5_mixed.evidence import header_match
from . import repository as repo
from .decision import choose_scan

ADOPT_ACTIONS = {"ADOPT", "REVIEW_APPROVE"}

CORE_COLUMNS = ("issue", "edition", "page", "file_name", "sha256", "phash", "clarity", "header_issue")
HARD_QUARANTINE = {"COLUMN_COUNT", "EMPTY_VALUE", "BAD_SHA256", "BAD_FORMAT", "MISSING_COLUMN", "EMPTY_FILE", "DUPLICATE_KEY", "TRUNCATED_INPUT"}


def scan_id(key: str, file_name: str, digest: str) -> str:
    """扫描原件标识与批次无关：同一业务键的同一文件同一摘要只留一份原件。"""
    return "SCN-" + sha256(f"{key}|{file_name}|{digest}".encode("utf-8")).hexdigest()[:20]


def evidence_id(batch_id: str, line_no: int, code: str, raw: str) -> str:
    return "EV-" + sha256(f"{batch_id}|{line_no}|{code}|{raw}".encode("utf-8")).hexdigest()[:20]


def scope_key(issue: str, edition: str) -> str:
    return f"{issue}|{edition}"


def _int_or_none(value) -> int | None:
    token = str(value or "").strip()
    if not token.isdigit() or int(token) < 1:
        return None
    return int(token)


def _prepare(text: str, batch_id: str, created_at: str):
    rows, bad = parse_manifest(text)
    evidence: list[dict] = []
    quarantined = 0

    def quarantine(line_no, code, raw, key=None, detail=None):
        nonlocal quarantined
        if code in HARD_QUARANTINE:
            quarantined += 1
        evidence.append({
            "evidence_id": evidence_id(batch_id, line_no, code, str(raw)),
            "batch_id": batch_id,
            "business_key": key,
            "line_no": line_no,
            "reason_code": code,
            "raw_value": str(raw),
            "detail": detail,
            "created_at": created_at,
        })

    for item in bad:
        quarantine(item["line_no"], item["reason_code"], item.get("raw", ""))

    scans, seen, scopes = [], set(), {}
    for row in rows:
        raw = ",".join(str(row.get(name, "")) for name in CORE_COLUMNS)
        line_no = row["line_no"]
        try:
            issue = normalize_issue(row["issue"])
            edition = normalize_edition(row["edition"])
            page = normalize_page(row["page"])
        except ValueError:
            quarantine(line_no, "BAD_FORMAT", raw, detail="期号/版次/页码无法规范化")
            continue
        if not valid_sha256(row["sha256"]):
            quarantine(line_no, "BAD_SHA256", raw, detail="sha256 不是 64 位十六进制")
            continue
        clarity = _int_or_none(row["clarity"])
        if clarity is None:
            quarantine(line_no, "BAD_FORMAT", raw, detail="清晰度不是正整数")
            continue
        file_name = (row["file_name"] or "").strip()
        if not file_name:
            quarantine(line_no, "EMPTY_VALUE", raw)
            continue

        key = business_key({"issue": issue, "edition": edition, "page": page})
        token = (key, file_name, row["sha256"].lower())
        if token in seen:
            quarantine(line_no, "DUPLICATE_KEY", raw, key=key, detail="清单内相同业务键重复扫描")
            continue
        seen.add(token)

        header_raw = (row.get("header_issue") or "").strip()
        header_issue = None
        if header_raw:
            try:
                header_issue = normalize_issue(header_raw)
            except ValueError:
                quarantine(line_no, "BAD_FORMAT", raw, key=key, detail="页眉期号无法规范化")
                continue
        if not header_raw:
            header_verified = 0
            quarantine(line_no, "HEADER_MISSING", raw, key=key, detail="页眉缺失，候选留待复核")
        elif header_match(issue, header_issue) is True:
            header_verified = 1
        else:
            header_verified = -1
            quarantine(line_no, "HEADER_MISMATCH", raw, key=key, detail=f"清单期号 {issue} 与页眉 {header_issue} 不符，候选留待复核")

        scans.append({
            "scan_id": scan_id(key, file_name, row["sha256"].lower()),
            "batch_id": batch_id,
            "business_key": key,
            "issue": issue,
            "edition": edition,
            "page": page,
            "line_no": line_no,
            "file_name": file_name,
            "sha256": row["sha256"].lower(),
            "phash": (row.get("phash") or "").strip() or None,
            "clarity": clarity,
            "header_issue": header_issue,
            "has_header": 1 if header_raw else 0,
            "header_verified": header_verified,
            "created_at": created_at,
        })

        scope = scope_key(issue, edition)
        provided = _int_or_none(row.get("page_count")) or _int_or_none(row.get("total_pages"))
        scopes[scope] = max(scopes.get(scope, 0), int(page), provided or 0)
    return scans, evidence, scopes, quarantined


def import_manifest(text: str, raw: bytes | None = None) -> dict:
    payload = raw if raw is not None else text.encode("utf-8")
    digest = input_digest(payload)
    batch_id = f"BATCH-{digest[:12]}"
    created_at = repo.now_iso()
    db = repo.connect()
    try:
        existing = repo.get_batch(db, batch_id)
        if existing:
            return {"created": False, **json.loads(existing["snapshot"])}

        tail_line_no, tail_raw = 0, None
        if text and not complete_tail(text):
            body, _, tail_raw = text.rpartition("\n")
            tail_line_no = text.count("\n") + 1
            text = body

        scans, evidence, scopes, quarantined = _prepare(text, batch_id, created_at)
        if tail_raw is not None:
            evidence.append({
                "evidence_id": evidence_id(batch_id, tail_line_no, "TRUNCATED_INPUT", tail_raw),
                "batch_id": batch_id,
                "business_key": None,
                "line_no": tail_line_no,
                "reason_code": "TRUNCATED_INPUT",
                "raw_value": tail_raw,
                "detail": "清单尾部不完整，未进入扫描件",
                "created_at": created_at,
            })
            quarantined += 1
        with repo.write(db):
            fresh = repo.get_batch(db, batch_id) is None
            if not fresh:
                return {"created": False, **json.loads(repo.get_batch(db, batch_id)["snapshot"])}

            new_groups: dict[str, list[dict]] = {}
            for scan in scans:
                if repo.insert_scan(db, scan) and repo.get_group(db, scan["business_key"]) is None:
                    # 已有业务键的扫描是新重扫候选；只有首次出现的业务键建立采纳组。
                    new_groups.setdefault(scan["business_key"], []).append(scan)

            for key, group_scans in new_groups.items():
                first = sorted(group_scans, key=lambda item: (item["line_no"], item["file_name"]))[0]
                repo.insert_group(db, {
                    "business_key": key,
                    "issue": first["issue"],
                    "edition": first["edition"],
                    "page": first["page"],
                    "revision": 0,
                    "adopted_scan_id": None,
                    "original_scan_id": first["scan_id"],
                    "origin_batch_id": batch_id,
                    "updated_at": created_at,
                })
                repo.mark_original(db, first["scan_id"])
                verified = [item for item in group_scans if item["header_verified"] == 1]
                if not verified:
                    continue
                chosen = choose_scan(verified)
                repo.update_adoption(db, key, expected_revision=0, scan_id=chosen["scan_id"], new_revision=1, updated_at=created_at)
                repo.insert_decision(db, {
                    "decision_id": "DEC-" + sha256(f"{batch_id}|{key}|1".encode("utf-8")).hexdigest()[:20],
                    "request_id": None,
                    "business_key": key,
                    "action": "ADOPT",
                    "revision": 1,
                    "previous_revision": 0,
                    "scan_id": chosen["scan_id"],
                    "reason_code": "BASELINE" if len(verified) == 1 else "CLEARER_SCAN",
                    "reason_text": "导入基线：期版页与页眉一致，按清晰度择清晰扫描",
                    "actor": "system",
                    "decided_at": created_at,
                    "publish_state": "PUBLISHED",
                    "published_at": created_at,
                    "publish_attempts": 1,
                    "batch_id": batch_id,
                })

            for scope, expected in scopes.items():
                issue, edition = scope.split("|", 1)
                current = repo.get_scope(db, scope)
                if current is None:
                    repo.insert_scope(db, {
                        "scope_key": scope,
                        "issue": issue,
                        "edition": edition,
                        "expected_count": expected,
                        "origin_batch_id": batch_id,
                        "updated_at": created_at,
                    })
                elif expected > current["expected_count"]:
                    repo.expand_scope(db, scope, expected, created_at)

            for item in evidence:
                repo.insert_evidence(db, item)

            state = live_state(db)
            snapshot = {
                "batch_id": batch_id,
                "manifest_sha256": digest,
                "created_at": created_at,
                "input_count": len(scans) + quarantined,
                "accepted": len(scans),
                "quarantined": quarantined,
                "scans": scans,
                "evidence": evidence,
                "groups": state["groups"],
                "gaps": state["gaps"],
                "accounts": state["accounts"],
            }
            repo.insert_batch(db, {
                "batch_id": batch_id,
                "manifest_sha256": digest,
                "created_at": created_at,
                "input_count": snapshot["input_count"],
                "accepted": snapshot["accepted"],
                "quarantined": quarantined,
                "snapshot": json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
            })
        return {"created": True, **snapshot}
    finally:
        db.close()


def adoption_state(group: dict, adopted: dict | None, original: dict | None, decisions: list[dict]) -> dict:
    published = [item for item in decisions if item["publish_state"] == "PUBLISHED"]
    latest = published[-1] if published else None
    return {
        "business_key": group["business_key"],
        "issue": group["issue"],
        "edition": group["edition"],
        "page": group["page"],
        "revision": group["revision"],
        "adopted": adopted,
        "original": original,
        "status": "已采信" if adopted else "未采信",
        "reason_code": latest["reason_code"] if latest else None,
        "reason_text": latest["reason_text"] if latest else None,
        "updated_at": group["updated_at"],
    }


def _latest_decision_actions(decisions: list[dict]) -> dict[str, str]:
    """每个扫描件最近一次生效决定的动作：ADOPT/REVIEW_APPROVE 或 REJECT。"""
    latest: dict[str, str] = {}
    for item in sorted(decisions, key=lambda row: row["revision"]):
        if item["publish_state"] in {"PUBLISHED", "SUPERSEDED"}:
            latest[item["scan_id"]] = item["action"]
    return latest


def classify_scan(scan: dict, group: dict, ever_adopted: set[str] | None = None, ever_rejected: set[str] | None = None) -> str:
    ever_adopted = ever_adopted or set()
    ever_rejected = ever_rejected or set()
    if group.get("adopted_scan_id") == scan["scan_id"]:
        return "ADOPTED"
    if scan["header_verified"] == -1:
        return "MIXED"
    if scan["header_verified"] == 0:
        return "REVIEW_REQUIRED"
    if scan["scan_id"] in ever_rejected:
        return "REJECTED"
    # 曾被采信、后被替换或被撤回的扫描属于被替代件；
    # 尚无决定记录的是新重扫候选。
    if scan["scan_id"] in ever_adopted:
        return "SUPERSEDED"
    if group.get("revision", 0) >= 1 and scan["batch_id"] == group.get("origin_batch_id"):
        return "SUPERSEDED"
    return "CANDIDATE"


def live_state(db) -> dict:
    """当前采纳快照。缺页与扫描件四账都由当前采纳页推导，
    发布事务提交的同一刻一起切换。"""
    groups = repo.list_groups(db)
    scans = repo.list_scans(db)
    evidence = repo.list_evidence(db)
    decisions = repo.list_decisions(db)
    scan_by_id = {scan["scan_id"]: scan for scan in scans}
    scans_by_key: dict[str, list[dict]] = {}
    for scan in scans:
        scans_by_key.setdefault(scan["business_key"], []).append(scan)
    decisions_by_key: dict[str, list[dict]] = {}
    for decision in decisions:
        decisions_by_key.setdefault(decision["business_key"], []).append(decision)
    latest_action = _latest_decision_actions(decisions)
    ever_adopted = {scan_id for scan_id, action in latest_action.items() if action in ADOPT_ACTIONS}
    ever_rejected = {scan_id for scan_id, action in latest_action.items() if action == "REJECT"}

    rows, candidates = [], []
    adopted_pages: dict[str, set[int]] = {}
    for group in groups:
        key = group["business_key"]
        adopted = scan_by_id.get(group["adopted_scan_id"])
        original = scan_by_id.get(group["original_scan_id"])
        rows.append(adoption_state(group, adopted, original, decisions_by_key.get(key, [])))
        if adopted:
            adopted_pages.setdefault(scope_key(group["issue"], group["edition"]), set()).add(int(group["page"]))
        for scan in scans_by_key.get(key, []):
            if adopted and scan["scan_id"] == adopted["scan_id"]:
                continue
            status = classify_scan(scan, group, ever_adopted, ever_rejected)
            candidates.append({
                "business_key": key,
                "page": group["page"],
                "scan": scan,
                "status": status,
                "requires_review": scan["header_verified"] != 1,
            })

    gaps = []
    for scope in repo.list_scopes(db):
        pages = adopted_pages.get(scope["scope_key"], set())
        expected = scope["expected_count"]
        gaps.append({
            "scope_key": scope["scope_key"],
            "issue": scope["issue"],
            "edition": scope["edition"],
            "expected_count": expected,
            "adopted_pages": [f"{n:03d}" for n in sorted(pages)],
            "missing": [f"{n:03d}" for n in range(1, expected + 1) if n not in pages],
            "unexpected": [f"{n:03d}" for n in sorted(pages) if n > expected],
        })

    accounts = _accounts(scans, groups, evidence, decisions)
    pending = [item for item in decisions if item["publish_state"] == "PENDING"]
    return {
        "groups": rows,
        "candidates": candidates,
        "gaps": gaps,
        "accounts": accounts,
        "decisions": decisions,
        "pending": pending,
        "scans": scans,
        "evidence": evidence,
    }


def _accounts(scans: list[dict], groups: list[dict], evidence: list[dict], decisions: list[dict]) -> dict:
    group_by_key = {group["business_key"]: group for group in groups}
    latest_action = _latest_decision_actions(decisions)
    ever_adopted = {scan_id for scan_id, action in latest_action.items() if action in ADOPT_ACTIONS}
    ever_rejected = {scan_id for scan_id, action in latest_action.items() if action == "REJECT"}
    buckets = {"adopted": 0, "superseded": 0, "mixed": 0, "review": 0, "rejected": 0, "candidate": 0}
    for scan in scans:
        status = classify_scan(scan, group_by_key.get(scan["business_key"], {}), ever_adopted, ever_rejected)
        buckets[{"ADOPTED": "adopted", "MIXED": "mixed", "REVIEW_REQUIRED": "review", "REJECTED": "rejected", "SUPERSEDED": "superseded"}.get(status, "candidate")] += 1
    quarantined_rows = {
        (item["line_no"], item["reason_code"], item["raw_value"])
        for item in evidence
        if item["reason_code"] in HARD_QUARANTINE
    }
    quarantined = len(quarantined_rows)
    total = len(scans) + quarantined
    parts = sum(buckets.values()) + quarantined
    return {
        "input": total,
        "scans": len(scans),
        **buckets,
        "quarantined": quarantined,
        "header_evidence": sum(1 for item in evidence if item["reason_code"] in {"HEADER_MISSING", "HEADER_MISMATCH"}),
        "balanced": total == parts,
        "difference": total - parts,
    }
