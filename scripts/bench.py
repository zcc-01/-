"""性能基准：验证报告 3.6 的性能指标（5000 条链校验 < 2s，单码查询 P95 < 500ms）。

用法：
    python scripts/bench.py [事件条数]

实现说明：批量构造记录时直接计算哈希并 add_all，避免 5000 次 commit
把"写入耗时"混进"校验耗时"；被测对象只有 HashChain.verify 与 TraceService.scan。
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import app.models_registry  # noqa: F401,E402
from app.common.models import Farm, User  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.trace.chain import HashChain, canonical_detail  # noqa: E402
from app.trace.models import Batch, BatchStatus, CodeType, Stage, TraceCode, TraceEvent  # noqa: E402
from app.trace.service import TraceService  # noqa: E402


def build(n_events: int):
    tmp = Path(tempfile.mkdtemp())
    engine = create_engine(f"sqlite:///{(tmp / 'bench.db').as_posix()}", future=True)
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    db = Session()

    farmer = User(
        username="bench", password_hash=hash_password("Passw0rd!"), display_name="压测农户", role="farmer"
    )
    db.add(farmer)
    db.flush()
    farm = Farm(farm_name="压测农场", owner_id=farmer.user_id, province="浙江省", city="杭州市")
    db.add(farm)
    db.flush()
    batch = Batch(
        batch_no="PC-BENCH-0001",
        farm_id=farm.farm_id,
        crop_name="压测作物",
        plot_no="P-01",
        plant_date=date(2026, 3, 1),
        harvest_date=date(2026, 9, 1),
        status=BatchStatus.QUALIFIED,
    )
    db.add(batch)
    db.flush()

    prev = HashChain.genesis_prev_hash(batch.batch_id)
    base_time = datetime(2026, 3, 1, 8, 0)
    events = []
    for seq in range(1, n_events + 1):
        occurred = base_time + timedelta(minutes=seq)
        detail = canonical_detail({"note": f"第 {seq} 条压测记录", "value": seq})
        stage = Stage.ALL[seq % len(Stage.ALL)]
        curr = HashChain.compute_hash(
            prev_hash=prev,
            batch_id=batch.batch_id,
            seq_no=seq,
            stage=stage,
            operator_id=farmer.user_id,
            occurred_at=occurred,
            detail=detail,
        )
        events.append(
            TraceEvent(
                batch_id=batch.batch_id,
                seq_no=seq,
                stage=stage,
                operator_id=farmer.user_id,
                occurred_at=occurred,
                location="压测地点",
                detail=detail,
                prev_hash=prev,
                curr_hash=curr,
            )
        )
        prev = curr

    write_start = time.perf_counter()
    db.add_all(events)
    batch.event_count = n_events
    batch.last_event_hash = prev
    db.commit()
    write_seconds = time.perf_counter() - write_start

    for item_no in range(1, 11):
        db.add(
            TraceCode(
                code_value=f"{batch.batch_no}-ITM{item_no:04d}",
                code_type=CodeType.ITEM,
                batch_id=batch.batch_id,
                item_no=item_no,
            )
        )
    db.commit()
    return db, batch, write_seconds


def main() -> None:
    n_events = int(sys.argv[1]) if len(sys.argv) > 1 else 5000
    db, batch, write_seconds = build(n_events)
    print(f"构造完成：{n_events} 条记录写入耗时 {write_seconds:.2f}s")

    start = time.perf_counter()
    result = HashChain.verify(db, batch.batch_id)
    verify_seconds = time.perf_counter() - start
    print(f"链校验：ok={result.ok} total={result.total} 耗时 {verify_seconds:.3f}s")

    # 单码查询 P95：直接调 Service.scan（含链校验），重复 20 次取分位
    code = f"{batch.batch_no}-ITM0001"
    samples = []
    for _ in range(20):
        t0 = time.perf_counter()
        TraceService.scan(db, code)
        samples.append((time.perf_counter() - t0) * 1000)
    p95 = statistics.quantiles(samples, n=20)[-1]
    print(f"扫码查询（含整链校验）：均值 {statistics.mean(samples):.1f}ms，P95 {p95:.1f}ms")

    budget_ok = verify_seconds < 2.0 and p95 < 500
    print(f"是否符合报告 3.6 指标（<2s / P95 <500ms）：{'是' if budget_ok else '否'}")

    if not result.ok:
        raise SystemExit("链校验未通过，基准无效")
    db.close()


if __name__ == "__main__":
    main()
