"""哈希链组件单元测试：可重现性 + 四类篡改的定位能力。

这是本域最核心的测试：报告 5.2.2 声称「任一环节被改动可定位到断链位置」，
必须用可执行的用例证明，而不是只写在文档里。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, text

from app.trace.chain import HashChain, canonical_detail, normalize_time
from app.trace.models import TraceEvent
from app.trace.schemas import EventCreate
from app.trace.service import TraceService
from tests.conftest import create_batch


def _append(db, batch, seed, stage="processing", detail=None, occurred=None):
    payload = EventCreate(stage=stage, occurred_at=occurred, location="车间", detail=detail or {"note": "x"})
    event, _ = TraceService.append_event(db, batch.batch_id, payload, seed.farmer)
    return event


def test_genesis_hash_is_reproducible_and_batch_specific():
    h1 = HashChain.genesis_prev_hash("BAT001")
    h2 = HashChain.genesis_prev_hash("BAT001")
    h3 = HashChain.genesis_prev_hash("BAT002")
    assert h1 == h2, "同一批次的创世基准必须可重现"
    assert h1 != h3, "不同批次不能共用同一创世基准"
    assert len(h1) == 64


def test_compute_hash_is_stable_against_dict_ordering():
    args = dict(
        prev_hash="a" * 64,
        batch_id="BAT001",
        seq_no=1,
        stage="production",
        operator_id="USR001",
        occurred_at=datetime(2026, 3, 20, 8, 30),
    )
    left = HashChain.compute_hash(detail=canonical_detail({"b": 1, "a": 2}), **args)
    right = HashChain.compute_hash(detail=canonical_detail({"a": 2, "b": 1}), **args)
    assert left == right
    assert len(left) == 64


def test_normalize_time_truncates_microseconds():
    value = normalize_time(datetime(2026, 3, 20, 8, 30, 12, 987654))
    assert value.microsecond == 0


def test_chain_ok_after_appends(db, seed):
    batch = create_batch(db, seed)
    _append(db, batch, seed)
    _append(db, batch, seed, stage="quality")

    result = HashChain.verify(db, batch.batch_id)
    assert result.ok is True
    assert result.total == 3
    assert result.expected_total == 3


def test_content_tamper_is_located(db, seed):
    batch = create_batch(db, seed)
    _append(db, batch, seed)
    target = db.scalar(
        select(TraceEvent).where(TraceEvent.batch_id == batch.batch_id, TraceEvent.seq_no == 2)
    )

    db.execute(
        text("UPDATE trace_event SET detail = :detail WHERE event_id = :event_id"),
        {"detail": '{"note":"被偷偷改掉的明细"}', "event_id": target.event_id},
    )
    db.commit()
    db.expire_all()

    result = HashChain.verify(db, batch.batch_id)
    assert result.ok is False
    assert result.reason == "content_tampered"
    assert result.broken_at == 2
    assert result.broken_event_id == target.event_id


def test_prev_hash_tamper_is_located(db, seed):
    batch = create_batch(db, seed)
    _append(db, batch, seed)

    db.execute(
        text("UPDATE trace_event SET prev_hash = :prev WHERE batch_id = :bid AND seq_no = 2"),
        {"prev": "0" * 64, "bid": batch.batch_id},
    )
    db.commit()
    db.expire_all()

    result = HashChain.verify(db, batch.batch_id)
    assert result.ok is False
    assert result.reason == "prev_hash_mismatch"
    assert result.broken_at == 2


def test_deleted_middle_record_is_located(db, seed):
    batch = create_batch(db, seed)
    _append(db, batch, seed)
    _append(db, batch, seed, stage="quality")

    db.execute(
        text("DELETE FROM trace_event WHERE batch_id = :bid AND seq_no = 2"),
        {"bid": batch.batch_id},
    )
    db.commit()
    db.expire_all()

    result = HashChain.verify(db, batch.batch_id)
    assert result.ok is False
    assert result.reason in {"seq_gap", "count_mismatch"}
    assert result.broken_at == 2


def test_deleted_tail_record_is_detected_by_anchor(db, seed):
    batch = create_batch(db, seed)
    _append(db, batch, seed)

    db.execute(
        text("DELETE FROM trace_event WHERE batch_id = :bid AND seq_no = 2"),
        {"bid": batch.batch_id},
    )
    # 同步篡改批次锚点，模拟「删了尾巴还改了计数」的高级篡改
    db.execute(
        text("UPDATE batch SET event_count = 1 WHERE batch_id = :bid"),
        {"bid": batch.batch_id},
    )
    db.commit()
    db.expire_all()

    result = HashChain.verify(db, batch.batch_id)
    assert result.ok is False
    assert result.reason in {"tail_truncated", "count_mismatch"}


def test_verify_does_not_raise_when_broken(db, seed):
    """校验返回结果而不是抛异常：页面要先展示已有记录再告警（UC-04）。"""
    batch = create_batch(db, seed)
    db.execute(
        text("UPDATE batch SET event_count = 9 WHERE batch_id = :bid"), {"bid": batch.batch_id}
    )
    db.commit()
    db.expire_all()
    result = HashChain.verify(db, batch.batch_id)
    assert result.ok is False
    assert isinstance(result.message, str) and result.message


def test_operator_name_masked(db, seed):
    from app.trace.service import mask_name

    assert mask_name("张建国") == "张**"
    assert mask_name("A") == "A"
    assert mask_name("") == "匿名"
