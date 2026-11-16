from .similarity import hamming_distance, is_candidate
from .decision import choose_scan
from . import intake
from . import repository as repo


def run(rows: list[dict] | None = None) -> dict:
    rows = rows or [{"file_name": "old.tif", "phash": "0f0f", "clarity": 72, "captured_at": "2026-01-01"}, {"file_name": "new.tif", "phash": "0f0e", "clarity": 91, "captured_at": "2026-02-01"}]
    distance = hamming_distance(rows[0]["phash"], rows[1]["phash"])
    return {"distance": distance, "candidate": is_candidate(rows[0]["phash"], rows[1]["phash"]), "selected": choose_scan(rows)["file_name"], "superseded": len(rows) - 1}


def demo():
    return run()


def overview() -> dict:
    db = repo.connect()
    try:
        state = intake.live_state(db)
        batches = [
            {"batch_id": batch["batch_id"], "created_at": batch["created_at"], "input_count": batch["input_count"], "accepted": batch["accepted"], "quarantined": batch["quarantined"]}
            for batch in repo.list_batches(db)
        ]
    finally:
        db.close()
    return {
        "groups": state["groups"],
        "candidates": state["candidates"],
        "gaps": state["gaps"],
        "accounts": state["accounts"],
        "pending": state["pending"],
        "decisions": state["decisions"],
        "batches": batches,
    }
