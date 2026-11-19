"""并发追加测试：证明「序号不冲突、链不断裂」。

对应报告 5.3.1 的案例：`SELECT MAX(seq_no)+1` 在并发下会产生重复序号，
这里用多线程同时对同一批次追加，验证原子序号 + 唯一索引兜底确实有效。

用文件型 SQLite（非内存）才能真正模拟多连接写竞争。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.models_registry  # noqa: F401
from app.common.models import Farm, User
from app.core.security import hash_password
from app.db.base import Base
from app.trace.chain import HashChain
from app.trace.models import Batch, BatchStatus, TraceEvent
from app.trace.schemas import EventCreate
from app.trace.service import TraceService

THREADS = 6
PER_THREAD = 2


@pytest.fixture()
def file_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'concurrency.db').as_posix()}",
        connect_args={"check_same_thread": False, "timeout": 30},
        future=True,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)

    db = Session()
    farmer = User(
        username="farmer01",
        password_hash=hash_password("Passw0rd!"),
        display_name="张建国",
        role="farmer",
    )
    db.add(farmer)
    db.flush()
    farm = Farm(farm_name="并发测试农场", owner_id=farmer.user_id, province="浙江省", city="杭州市")
    db.add(farm)
    db.flush()
    batch = Batch(
        batch_no="PC-CONCURRENT-0001",
        farm_id=farm.farm_id,
        crop_name="测试作物",
        plot_no="B-01",
        plant_date=datetime(2026, 3, 1).date(),
        harvest_date=datetime(2026, 9, 1).date(),
        status=BatchStatus.QUALIFIED,
    )
    db.add(batch)
    db.commit()
    batch_id, farmer_id = batch.batch_id, farmer.user_id
    db.close()
    yield Session, batch_id, farmer_id
    engine.dispose()


def test_concurrent_appends_keep_chain_intact(file_db):
    Session, batch_id, farmer_id = file_db
    errors: list[Exception] = []
    barrier = threading.Barrier(THREADS)

    def worker(index: int) -> None:
        session = Session()
        try:
            user = session.get(User, farmer_id)
            batch = session.get(Batch, batch_id)
            barrier.wait(timeout=10)
            for n in range(PER_THREAD):
                TraceService.append_event(
                    session,
                    batch_id,
                    EventCreate(
                        stage="processing",
                        occurred_at=datetime(2026, 9, 5, 8, 0) + timedelta(minutes=index * 10 + n),
                        location=f"车间-{index}",
                        detail={"worker": index, "n": n},
                    ),
                    user,
                )
                session.refresh(batch)
        except Exception as exc:  # pragma: no cover - 失败时用于定位
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not errors, f"并发写入出现异常：{errors[:3]}"

    session = Session()
    try:
        events = session.scalars(
            select(TraceEvent).where(TraceEvent.batch_id == batch_id).order_by(TraceEvent.seq_no)
        ).all()
        seqs = [event.seq_no for event in events]
        assert seqs == list(range(1, THREADS * PER_THREAD + 1)), f"序号不连续：{seqs}"

        result = HashChain.verify(session, batch_id)
        assert result.ok is True, result.message
        assert result.total == THREADS * PER_THREAD
    finally:
        session.close()
