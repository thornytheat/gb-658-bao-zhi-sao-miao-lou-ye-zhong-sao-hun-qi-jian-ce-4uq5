import json
import threading

import pytest

import app.store as store
from app.errors import DomainError
from app.modules.m4_rescans import publish, service
from app.modules.m4_rescans import repository as repo
from app.modules.m4_rescans.adjudication import decide, recover_all, resume
from app.modules.m4_rescans.intake import import_manifest

HEADER = "issue,edition,page,file_name,sha256,phash,clarity,header_issue\n"
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def row(issue="2026-11-01", edition="01", page="001", name="a.tif", digest=DIGEST_A, clarity="90", header=None, phash="0f0f"):
    header = issue if header is None else header
    return f"{issue},{edition},{page},{name},{digest},{phash},{clarity},{header}\n"


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATABASE_PATH", tmp_path / "m4.sqlite3")
    repo._SCHEMA_READY.clear()
    publish.FAULT = None
    yield tmp_path / "m4.sqlite3"
    publish.FAULT = None
    repo._SCHEMA_READY.clear()


def state():
    return service.overview()


def scan_of(key, name):
    return next(item for item in state()["candidates"] if item["business_key"] == key and item["scan"]["file_name"] == name)["scan"]


def any_scan_of(key, name):
    """含当前采纳页在内的全部扫描件。"""
    database = repo.connect()
    try:
        scans = repo.scans_for_key(database, key)
    finally:
        database.close()
    return next(item for item in scans if item["file_name"] == name)


def snapshot(batch_id):
    database = repo.connect()
    try:
        return json.loads(repo.get_batch(database, batch_id)["snapshot"])
    finally:
        database.close()


def test_import_rebuilds_original_and_current_view(db):
    result = import_manifest(HEADER + row(name="old.tif", clarity="70") + row(name="new.tif", digest=DIGEST_B, clarity="95"))
    assert result["created"] is True
    current = state()
    group = current["groups"][0]
    assert group["original"]["file_name"] == "old.tif"
    assert group["adopted"]["file_name"] == "new.tif"
    assert group["reason_code"] == "CLEARER_SCAN"


def test_import_is_idempotent_on_same_bytes(db):
    text = HEADER + row()
    first = import_manifest(text)
    second = import_manifest(text)
    assert first["batch_id"] == second["batch_id"] and second["created"] is False
    database = repo.connect()
    try:
        assert len(repo.list_batches(database)) == 1 and len(repo.list_scans(database)) == 1
    finally:
        database.close()


def test_missing_header_candidate_waits_for_review(db):
    import_manifest(HEADER + row(header=""))
    current = state()
    assert current["groups"][0]["adopted"] is None
    candidate = current["candidates"][0]
    assert candidate["status"] == "REVIEW_REQUIRED" and candidate["requires_review"] is True
    with pytest.raises(DomainError) as error:
        decide({"business_key": "2026-11-01|01|001", "scan_id": candidate["scan"]["scan_id"], "action": "ADOPT", "expected_revision": 0})
    assert error.value.code == "HEADER_MISSING"


def test_mismatched_header_is_mixed_and_needs_review_reason(db):
    import_manifest(HEADER + row(header="2026-11-02"))
    current = state()
    assert current["candidates"][0]["status"] == "MIXED"
    scan_id = current["candidates"][0]["scan"]["scan_id"]
    with pytest.raises(DomainError) as error:
        decide({"business_key": "2026-11-01|01|001", "scan_id": scan_id, "action": "REVIEW_APPROVE", "expected_revision": 0})
    assert error.value.code == "BAD_REQUEST"
    published = decide({"business_key": "2026-11-01|01|001", "scan_id": scan_id, "action": "REVIEW_APPROVE", "expected_revision": 0, "reason_text": "复核页眉与页码一致"})
    assert published["publish_state"] == "PUBLISHED" and published["reason_code"] == "REVIEW_APPROVED"
    assert published["reason_text"] == "复核页眉与页码一致"


def test_adoption_switches_gaps_and_accounts_together(db):
    import_manifest(HEADER + row(name="one.tif") + row(page="003", name="three.tif", digest=DIGEST_B, header="2026-11-02"))
    before = state()
    assert before["gaps"][0]["missing"] == ["002", "003"]
    assert before["accounts"]["adopted"] == 1

    scan_id = scan_of("2026-11-01|01|003", "three.tif")["scan_id"]
    decide({"business_key": "2026-11-01|01|003", "scan_id": scan_id, "action": "REVIEW_APPROVE", "expected_revision": 0, "reason_text": "复核页码一致"})
    after = state()
    assert after["gaps"][0]["missing"] == ["002"]  # 缺页与采纳页同一事务切换
    assert after["accounts"]["adopted"] == 2 and after["accounts"]["balanced"] is True


def test_archive_batch_keeps_original_evidence_after_rescan(db):
    import_manifest(HEADER + row(name="one.tif", clarity="70") + row(page="003", name="three.tif", digest=DIGEST_B))
    archived = state()["batches"][0]["batch_id"]
    before = snapshot(archived)
    settle = import_manifest(HEADER + row(name="rescan.tif", digest="c" * 64, clarity="99"))
    assert settle["created"] is True
    candidate = scan_of("2026-11-01|01|001", "rescan.tif")
    published = decide({"business_key": "2026-11-01|01|001", "scan_id": candidate["scan_id"], "action": "ADOPT", "expected_revision": 1})
    assert published["publish_state"] == "PUBLISHED"

    current = state()["groups"][0]
    assert current["adopted"]["file_name"] == "rescan.tif"
    assert current["original"]["file_name"] == "one.tif"
    after = snapshot(archived)
    assert after == before  # 归档快照不因采信而改写
    assert after["groups"][0]["adopted"]["file_name"] == "one.tif"
    assert after["gaps"][0]["missing"] == ["002"]


def test_stale_revision_rejected_after_concurrent_decision(db):
    import_manifest(HEADER + row())
    key = "2026-11-01|01|001"
    scan = any_scan_of(key, "a.tif")
    first = decide({"business_key": key, "scan_id": scan["scan_id"], "action": "REJECT", "expected_revision": 1, "request_id": "r-1"})
    assert first["publish_state"] == "PUBLISHED"
    with pytest.raises(DomainError) as error:
        decide({"business_key": key, "scan_id": scan["scan_id"], "action": "REJECT", "expected_revision": 1, "request_id": "r-2"})
    assert error.value.code == "STALE_VERSION" and error.value.details["current_revision"] == 2


def test_two_concurrent_decisions_only_one_wins(db):
    import_manifest(HEADER + row(header=""))
    key = "2026-11-01|01|001"
    scan = scan_of(key, "a.tif")
    outcomes = []

    def worker(index):
        try:
            result = decide({"business_key": key, "scan_id": scan["scan_id"], "action": "REVIEW_APPROVE", "expected_revision": 0, "reason_text": f"复核 {index}", "actor": f"qc-{index}", "request_id": f"req-{index}"})
            outcomes.append(("ok", result["revision"]))
        except DomainError as error:
            outcomes.append((error.code, error.details.get("current_revision")))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcome[0] for outcome in outcomes) == ["STALE_VERSION", "ok"]
    database = repo.connect()
    try:
        assert len(repo.list_decisions(database, key)) == 1
    finally:
        database.close()


def test_publish_interruption_keeps_original_page_and_retry_adds_no_decision(db):
    import_manifest(HEADER + row(name="one.tif", clarity="70") + row(name="two.tif", digest=DIGEST_B, clarity="95"))
    key = "2026-11-01|01|001"
    scan = any_scan_of(key, "one.tif")

    def interrupt():
        raise publish.PublishInterrupted()

    publish.FAULT = interrupt
    with pytest.raises(DomainError) as error:
        decide({"business_key": key, "scan_id": scan["scan_id"], "action": "ADOPT", "expected_revision": 1, "reason_text": "回退到原件", "request_id": "retry-1"})
    assert error.value.code == "PUBLISH_INTERRUPTED" and error.value.details["retryable"] is True

    current = state()
    assert current["groups"][0]["adopted"]["file_name"] == "two.tif"  # 失败仍显示原采纳页
    assert current["groups"][0]["revision"] == 1
    assert len(current["pending"]) == 1

    publish.FAULT = None
    retried = decide({"business_key": key, "scan_id": scan["scan_id"], "action": "ADOPT", "expected_revision": 1, "reason_text": "回退到原件", "request_id": "retry-1"})
    assert retried["publish_state"] == "PUBLISHED" and retried["action"] == "ADOPT"
    database = repo.connect()
    try:
        decisions = repo.list_decisions(database, key)
        assert len(decisions) == 2  # 基线 1 条 + 本次 1 条，重试不新增
        assert decisions[-1]["publish_attempts"] == 2
    finally:
        database.close()


def test_startup_recovery_publishes_pending_decision(db):
    import_manifest(HEADER + row(name="one.tif", clarity="70") + row(name="two.tif", digest=DIGEST_B, clarity="95"))
    key = "2026-11-01|01|001"
    scan = any_scan_of(key, "one.tif")
    publish.FAULT = lambda: (_ for _ in ()).throw(publish.PublishInterrupted())
    with pytest.raises(DomainError):
        decide({"business_key": key, "scan_id": scan["scan_id"], "action": "ADOPT", "expected_revision": 1, "request_id": "recover-1"})
    publish.FAULT = None
    results = recover_all()
    assert results and results[0]["state"] == "PUBLISHED"
    assert state()["groups"][0]["adopted"]["file_name"] == "one.tif"


def test_rejected_candidate_keeps_original_adoption(db):
    import_manifest(HEADER + row(name="one.tif", clarity="70") + row(name="two.tif", digest=DIGEST_B, clarity="95"))
    key = "2026-11-01|01|001"
    scan = any_scan_of(key, "one.tif")
    result = decide({"business_key": key, "scan_id": scan["scan_id"], "action": "REJECT", "expected_revision": 1, "reason_text": "原件有缺角"})
    assert result["publish_state"] == "PUBLISHED" and result["reason_code"] == "REJECTED"
    current = state()
    assert current["groups"][0]["adopted"]["file_name"] == "two.tif"
    assert current["groups"][0]["revision"] == 2


def test_decisions_history_is_append_only(db):
    import_manifest(HEADER + row(name="one.tif", clarity="70") + row(name="two.tif", digest=DIGEST_B, clarity="95"))
    key = "2026-11-01|01|001"
    one = any_scan_of(key, "one.tif")
    two = any_scan_of(key, "two.tif")
    decide({"business_key": key, "scan_id": one["scan_id"], "action": "ADOPT", "expected_revision": 1, "request_id": "h-1"})
    decide({"business_key": key, "scan_id": two["scan_id"], "action": "ADOPT", "expected_revision": 2, "request_id": "h-2"})
    database = repo.connect()
    try:
        history = repo.list_decisions(database, key)
    finally:
        database.close()
    assert [(item["revision"], item["action"]) for item in history] == [(1, "ADOPT"), (2, "ADOPT"), (3, "ADOPT")]


def test_import_quarantines_bad_rows_with_physical_line_numbers(db):
    text = HEADER + row() + "2026-11-01,01,002\n" + row(page="003", digest="x" * 64)
    result = import_manifest(text)
    assert result["quarantined"] == 2
    codes = {(item["line_no"], item["reason_code"]) for item in result["evidence"]}
    assert (3, "COLUMN_COUNT") in codes and (4, "BAD_SHA256") in codes
    assert result["accounts"]["balanced"] is True and result["accounts"]["difference"] == 0


def test_rejecting_current_adoption_reopens_gap(db):
    import_manifest(HEADER + row(name="one.tif"))
    key = "2026-11-01|01|001"
    scan = any_scan_of(key, "one.tif")
    assert state()["gaps"][0]["missing"] == []
    result = decide({"business_key": key, "scan_id": scan["scan_id"], "action": "REJECT", "expected_revision": 1, "reason_text": "扫描件不可用"})
    assert result["publish_state"] == "PUBLISHED" and result["reason_code"] == "REJECTED"
    after = state()
    assert after["groups"][0]["adopted"] is None
    assert after["groups"][0]["original"]["file_name"] == "one.tif"
    assert after["gaps"][0]["missing"] == ["001"]
    assert after["accounts"]["adopted"] == 0


def test_header_evidence_is_counted_once_even_when_reused(db):
    text = HEADER + row(header="")
    import_manifest(text)
    first = state()["accounts"]
    assert first["quarantined"] == 0  # 页眉证据不是隔离行
    assert first["header_evidence"] == 1
    import_manifest(HEADER + row(header="") + row(name="b.tif", digest=DIGEST_B))
    assert state()["accounts"]["balanced"] is True
