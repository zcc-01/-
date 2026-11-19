"""品类与商品演示数据（幂等写入）。

为什么单独一个 seed 模块：
`app/db/seed.py` 只在**空库**时执行，而「新增水果、蔬菜等品类」是一次数据升级——
评审者本地已经有 `data/app.db`，重跑 seed 不会补数据。所以这里做成
「缺什么补什么」：分类树按 code 判断、商品按名字判断、批次按 batch_no 判断，
重复执行多少次结果都一样，也不会覆盖用户后来改过的价格与库存。

数据口径说明：
- 商品价格取 2026 年 11 月产地直供的常见零售区间（元/箱、元/袋），
  写成「分」存储，展示时再除以 100；
- 产地、品种、上市季按公开资料整理，用于演示检索与详情页，不作为采购依据；
- 每个商品都挂一个已质检的溯源批次与溯源码，保证「扫码看履历」在任意商品上都可用。
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.models import Category
from app.common.models import Farm, User
from app.trace.chain import HashChain, canonical_detail
from app.trace.models import Batch, BatchStatus, CodeType, Stage, TraceCode, TraceEvent
from app.trade.models import Product, ProductStatus

# ------------------------------------------------------------------ 默认分类树
# icon 用 emoji、image 用本地 SVG：不依赖外网，clone 下来就有图
DEFAULT_CATEGORIES: list[dict[str, Any]] = [
    {
        "code": "fruit",
        "name": "水果",
        "icon": "🍊",
        "image": "/static/img/categories/fruit.svg",
        "description": "当季鲜果，产地直发，全程冷链",
        "children": [
            ("fruit-citrus", "柑橘类", "柑橘 / 橙 / 柚"),
            ("fruit-pome", "苹果梨类", "苹果 / 梨 / 山楂"),
            ("fruit-berry", "葡萄草莓", "葡萄 / 草莓 / 蓝莓"),
            ("fruit-tropical", "热带水果", "芒果 / 香蕉 / 火龙果"),
            ("fruit-kiwi", "猕猴桃", "徐香 / 翠香 / 红阳"),
        ],
    },
    {
        "code": "vegetable",
        "name": "蔬菜",
        "icon": "🥬",
        "image": "/static/img/categories/vegetable.svg",
        "description": "基地直采，当日采摘次日达",
        "children": [
            ("vegetable-leafy", "叶菜类", "上海青 / 菠菜 / 生菜"),
            ("vegetable-solanum", "茄果类", "番茄 / 辣椒 / 茄子"),
            ("vegetable-root", "根茎类", "山药 / 红薯 / 萝卜"),
            ("vegetable-mushroom", "食用菌", "香菇 / 平菇 / 木耳"),
        ],
    },
    {
        "code": "grain",
        "name": "粮油",
        "icon": "🌾",
        "image": "/static/img/categories/grain.svg",
        "description": "原产地稻米与杂粮，当季新粮",
        "children": [
            ("grain-rice", "大米", "稻花香 / 珍珠米 / 丝苗米"),
            ("grain-coarse", "杂粮", "小米 / 玉米 / 豆类"),
        ],
    },
    {
        "code": "tea",
        "name": "茶叶",
        "icon": "🍵",
        "image": "/static/img/categories/tea.svg",
        "description": "核心产区，明前采摘，手工制作",
        "children": [
            ("tea-green", "绿茶", "龙井 / 云雾 / 毛尖"),
            ("tea-black", "红茶", "功夫红茶 / 小种"),
        ],
    },
    {
        "code": "nuts",
        "name": "坚果炒货",
        "icon": "🌰",
        "image": "/static/img/categories/nuts.svg",
        "description": "低温烘焙，不添加香精",
        "children": [
            ("nuts-walnut", "核桃", "山核桃 / 纸皮核桃"),
        ],
    },
    {
        "code": "dried",
        "name": "干货菌菇",
        "icon": "🍄",
        "image": "/static/img/categories/dried.svg",
        "description": "自然晾晒，无硫熏",
        "children": [
            ("dried-bamboo", "笋干", "天目笋干 / 毛笋干"),
            ("dried-fungi", "菌菇", "香菇 / 木耳 / 银耳"),
        ],
    },
    {
        "code": "poultry",
        "name": "禽蛋水产",
        "icon": "🥚",
        "image": "/static/img/categories/poultry.svg",
        "description": "散养禽蛋，当日收集",
        "children": [
            ("poultry-egg", "禽蛋", "土鸡蛋 / 鸭蛋 / 鹅蛋"),
        ],
    },
    {
        "code": "other",
        "name": "其他",
        "icon": "🧺",
        "image": "/static/img/categories/other.svg",
        "description": "暂未细分的农产品",
        "children": [],
    },
]


# ------------------------------------------------------------------ 商品清单
# 每项 = 一个待演示商品 + 它自己的产地、批次、溯源事件
DEMO_PRODUCTS: list[dict[str, Any]] = [
    {
        "name": "赣南脐橙 5kg",
        "slug": "navel-orange",
        "code": "fruit-citrus",
        "price_cents": 6980,
        "stock": 180,
        "unit": "箱",
        "spec": "5kg/箱（约 18~22 个）",
        "grade": "一级果",
        "origin": "江西省赣州市信丰县",
        "shelf_life": "常温 15 天 / 冷藏 30 天",
        "storage": "阴凉通风处，冷藏 4~8℃ 更佳",
        "tags": "当季,产地直发,酸甜多汁,可溯源",
        "recommended": True,
        "sales": 126,
        "description": "信丰安西镇脐橙核心产区，11 月自然成熟采摘，果径 75~85mm，皮薄化渣、酸甜比约 1:8。采后 24 小时内分级装箱，不打蜡不催熟。",
        "farm": ("赣南橙心果园", "江西省", "赣州市", "信丰县安西镇脐橙基地 6 号", "approved"),
        "crop": "赣南脐橙",
        "plot": "G-01",
        "harvest": date(2026, 11, 8),
        "batch_no": "PC-20261108-ORG1",
        "event_note": "施用有机肥 3 吨/亩，人工疏果两次",
        "quality_note": "农残快检 28 项合格，可溶性固形物 13.2%",
    },
    {
        "name": "烟台红富士苹果 5kg",
        "slug": "fuji-apple",
        "code": "fruit-pome",
        "price_cents": 5980,
        "stock": 150,
        "unit": "箱",
        "spec": "5kg/箱（12~16 个）",
        "grade": "一级果",
        "origin": "山东省烟台市福山区",
        "shelf_life": "冷藏 90 天",
        "storage": "0~4℃ 冷藏，避免与异味食品同放",
        "tags": "脆甜,当季,套袋果,产地直发",
        "recommended": True,
        "sales": 203,
        "description": "福山产区丘陵沙壤土，昼夜温差大，糖度 15° 以上。全程套袋，果面光洁无锈斑，单果重 280~350g。",
        "farm": ("烟台福山果业合作社", "山东省", "烟台市", "福山区张格庄镇苹果基地", "approved"),
        "crop": "红富士苹果",
        "plot": "YT-04",
        "harvest": date(2026, 10, 26),
        "batch_no": "PC-20261026-ORG2",
        "event_note": "套袋 12 万只，行间生草改良土壤",
        "quality_note": "糖度 15.6°，硬度 8.2kg/cm²，无农残检出",
    },
    {
        "name": "云南阳光玫瑰葡萄 2kg",
        "slug": "shine-muscat",
        "code": "fruit-berry",
        "price_cents": 8800,
        "stock": 90,
        "unit": "盒",
        "spec": "2kg/盒（2 串）",
        "grade": "精品果",
        "origin": "云南省昆明市呈贡区",
        "shelf_life": "冷藏 10 天",
        "storage": "0~2℃ 冷藏，食用前再清洗",
        "tags": "高糖,无籽,冷链直发",
        "recommended": True,
        "sales": 74,
        "description": "高原设施栽培，昼夜温差 12℃ 以上，果粒 12g 左右，糖度 18° 以上，带玫瑰香气。单串控产 600g，穗形整齐。",
        "farm": ("云南高原果蔬园", "云南省", "昆明市", "呈贡区吴家营街道设施基地", "approved"),
        "crop": "阳光玫瑰葡萄",
        "plot": "YN-02",
        "harvest": date(2026, 10, 18),
        "batch_no": "PC-20261018-ORG3",
        "event_note": "控产疏穗，每枝留一穗，水肥一体化",
        "quality_note": "糖度 18.4°，果粒横径 24mm，农残合格",
    },
    {
        "name": "丹东红颜草莓 1kg",
        "slug": "strawberry",
        "code": "fruit-berry",
        "price_cents": 7800,
        "stock": 60,
        "unit": "盒",
        "spec": "1kg/盒（约 40 颗）",
        "grade": "特级果",
        "origin": "辽宁省丹东市东港市",
        "shelf_life": "冷藏 3 天",
        "storage": "0~4℃ 冷藏，不建议清洗后存放",
        "tags": "当日采摘,顺丰冷链,果香浓郁",
        "recommended": False,
        "sales": 58,
        "description": "东港日光温室头茬果，单果 25g 以上，果心红润、香气足。清晨采摘预冷后发运，破损包赔。",
        "farm": ("丹东莓香家庭农场", "辽宁省", "丹东市", "东港市椅圈镇草莓园区", "approved"),
        "crop": "红颜草莓",
        "plot": "DD-01",
        "harvest": date(2026, 11, 15),
        "batch_no": "PC-20261115-ORG4",
        "event_note": "蜜蜂授粉，不使用膨大剂",
        "quality_note": "单果均重 27g，糖度 11.8°，农残未检出",
    },
    {
        "name": "海南贵妃芒 5kg",
        "slug": "mango",
        "code": "fruit-tropical",
        "price_cents": 8980,
        "stock": 70,
        "unit": "箱",
        "spec": "5kg/箱（约 12~15 个）",
        "grade": "一级果",
        "origin": "海南省三亚市崖州区",
        "shelf_life": "常温 5 天 / 冷藏 10 天",
        "storage": "常温催熟，微软后冷藏",
        "tags": "树上熟,纤维少,香气足",
        "recommended": False,
        "sales": 41,
        "description": "三亚崖州芒果基地，树上自然转色至七成熟采摘，果皮红润、核薄肉厚，可食率 75% 以上。",
        "farm": ("三亚热带果园", "海南省", "三亚市", "崖州区南滨农场芒果基地", "approved"),
        "crop": "贵妃芒",
        "plot": "SY-07",
        "harvest": date(2026, 11, 2),
        "batch_no": "PC-20261102-ORG5",
        "event_note": "果实套袋防虫，物理诱虫板 60 张",
        "quality_note": "可食率 76%，糖度 17.1°，农残合格",
    },
    {
        "name": "陕西徐香猕猴桃 2.5kg",
        "slug": "kiwi",
        "code": "fruit-kiwi",
        "price_cents": 5680,
        "stock": 110,
        "unit": "箱",
        "spec": "2.5kg/箱（约 30 个）",
        "grade": "一级果",
        "origin": "陕西省宝鸡市眉县",
        "shelf_life": "冷藏 30 天",
        "storage": "与苹果同袋可加速后熟",
        "tags": "后熟发货,酸甜适口,维C高",
        "recommended": False,
        "sales": 66,
        "description": "秦岭北麓眉县产区，10 月下旬采收，硬果发货，常温 3~5 天自然软熟。单果 80~100g，果肉翠绿。",
        "farm": ("眉县猕猴桃合作社", "陕西省", "宝鸡市", "眉县金渠镇猕猴桃示范园", "approved"),
        "crop": "徐香猕猴桃",
        "plot": "MX-03",
        "harvest": date(2026, 10, 22),
        "batch_no": "PC-20261022-ORG6",
        "event_note": "人工授粉，果园生草，减少化肥用量 40%",
        "quality_note": "可溶性固形物 7.2%（硬果），单果均重 92g",
    },
    {
        "name": "寿光沙瓤番茄 2.5kg",
        "slug": "tomato",
        "code": "vegetable-solanum",
        "price_cents": 3980,
        "stock": 140,
        "unit": "箱",
        "spec": "2.5kg/箱",
        "grade": "一级",
        "origin": "山东省潍坊市寿光市",
        "shelf_life": "常温 7 天",
        "storage": "阴凉处存放，避免阳光直射",
        "tags": "沙瓤,自然成熟,生食",
        "recommended": True,
        "sales": 152,
        "description": "寿光日光温室越冬茬，熊蜂授粉自然坐果，果实沙瓤多汁，番茄味浓，适合生食与炒制。",
        "farm": ("寿光绿源蔬菜基地", "山东省", "潍坊市", "寿光市稻田镇蔬菜园区 12 号棚", "approved"),
        "crop": "番茄",
        "plot": "SG-12",
        "harvest": date(2026, 11, 16),
        "batch_no": "PC-20261116-ORG7",
        "event_note": "熊蜂授粉，滴灌水肥一体化，不打催红剂",
        "quality_note": "农残快检 32 项合格，糖酸比 6.8",
    },
    {
        "name": "有机上海青 1kg",
        "slug": "bok-choy",
        "code": "vegetable-leafy",
        "price_cents": 1680,
        "stock": 200,
        "unit": "份",
        "spec": "1kg/份（约 6~8 棵）",
        "grade": "有机",
        "origin": "浙江省杭州市临安区",
        "shelf_life": "冷藏 4 天",
        "storage": "保鲜袋包好冷藏，尽快食用",
        "tags": "有机认证,当日采摘,不打农药",
        "recommended": False,
        "sales": 188,
        "description": "临安高山基地有机种植，施用腐熟有机肥，清晨采摘带露水直发，菜梗脆嫩、叶片完整。",
        "farm": ("青禾家庭农场", "浙江省", "杭州市", "临安区清凉峰镇新峰村 3 组", "approved"),
        "crop": "上海青",
        "plot": "QH-V2",
        "harvest": date(2026, 11, 18),
        "batch_no": "PC-20261118-ORG8",
        "event_note": "有机肥种植，防虫网覆盖，人工除草",
        "quality_note": "有机认证 OPC-2025-0312，农残未检出",
    },
    {
        "name": "温县铁棍山药 2.5kg",
        "slug": "chinese-yam",
        "code": "vegetable-root",
        "price_cents": 4980,
        "stock": 95,
        "unit": "箱",
        "spec": "2.5kg/箱（约 8~10 根）",
        "grade": "一级",
        "origin": "河南省焦作市温县",
        "shelf_life": "阴凉处 30 天",
        "storage": "干燥通风，切口用保鲜膜包好冷藏",
        "tags": "垆土种植,粉糯,地理标志",
        "recommended": False,
        "sales": 77,
        "description": "温县垆土地块种植，条形顺直、毛须少，蒸煮后粉糯回甘。地理标志保护产品，一年一茬。",
        "farm": ("温县怀药种植合作社", "河南省", "焦作市", "温县番田镇铁棍山药基地", "approved"),
        "crop": "铁棍山药",
        "plot": "WX-05",
        "harvest": date(2026, 11, 6),
        "batch_no": "PC-20261106-ORG9",
        "event_note": "垆土深耕 1.2m，施用腐熟羊粪",
        "quality_note": "重金属检测合格，淀粉含量 18.6%",
    },
    {
        "name": "五常稻花香大米 5kg",
        "slug": "wuchang-rice",
        "code": "grain-rice",
        "price_cents": 12800,
        "stock": 130,
        "unit": "袋",
        "spec": "5kg/袋",
        "grade": "一级",
        "origin": "黑龙江省哈尔滨市五常市",
        "shelf_life": "12 个月",
        "storage": "阴凉干燥，开封后密封防潮",
        "tags": "当季新米,稻花香2号,地理标志",
        "recommended": True,
        "sales": 231,
        "description": "五常民乐乡核心产区稻花香 2 号，一年一季，9 月末收割自然晾晒，米粒饱满、饭香浓郁，冷饭不回生。",
        "farm": ("五常稻香合作社", "黑龙江省", "哈尔滨市", "五常市民乐乡稻花香基地", "approved"),
        "crop": "稻花香水稻",
        "plot": "WC-08",
        "harvest": date(2026, 9, 28),
        "batch_no": "PC-20260928-ORA1",
        "event_note": "人工插秧，河水灌溉，鸭稻共作 200 亩",
        "quality_note": "出米率 68%，直链淀粉 16.8%，食味值 86",
    },
    {
        "name": "沁州黄小米 2.5kg",
        "slug": "millet",
        "code": "grain-coarse",
        "price_cents": 4580,
        "stock": 120,
        "unit": "袋",
        "spec": "2.5kg/袋",
        "grade": "一级",
        "origin": "山西省长治市沁县",
        "shelf_life": "12 个月",
        "storage": "阴凉干燥，夏季建议冷藏",
        "tags": "地理标志,熬粥浓稠,当年新米",
        "recommended": False,
        "sales": 63,
        "description": "沁县檀山岭一带传统品种，颗粒金黄饱满，熬粥米油厚、香味足。当年新谷脱壳，不抛光不打蜡。",
        "farm": ("沁州黄谷子合作社", "山西省", "长治市", "沁县檀山岭谷子种植基地", "approved"),
        "crop": "沁州黄谷子",
        "plot": "QZ-02",
        "harvest": date(2026, 10, 5),
        "batch_no": "PC-20261005-ORA2",
        "event_note": "传统旱作，施用农家肥，人工除草三次",
        "quality_note": "蛋白质 11.2%，脂肪 3.6%，色泽金黄",
    },
    {
        "name": "临安散养土鸡蛋 30 枚",
        "slug": "free-range-eggs",
        "code": "poultry-egg",
        "price_cents": 4580,
        "stock": 160,
        "unit": "盒",
        "spec": "30 枚/盒（约 1.5kg）",
        "grade": "新鲜蛋",
        "origin": "浙江省杭州市临安区",
        "shelf_life": "冷藏 30 天",
        "storage": "0~10℃ 冷藏，大头朝上存放",
        "tags": "散养,当日收集,蛋黄橙红",
        "recommended": False,
        "sales": 143,
        "description": "林下散养蛋鸡，日粮以玉米、豆粕与青绿饲料为主，每日收集当日蛋，蛋清浓稠、蛋黄橙红。",
        "farm": ("青禾家庭农场", "浙江省", "杭州市", "临安区清凉峰镇新峰村 3 组", "approved"),
        "crop": "散养蛋鸡",
        "plot": "QH-E1",
        "harvest": date(2026, 11, 18),
        "batch_no": "PC-20261118-ORA3",
        "event_note": "林下散养 1200 羽，日粮不含抗生素",
        "quality_note": "沙门氏菌未检出，哈夫单位 82",
    },
    {
        "name": "古田干香菇 250g",
        "slug": "shiitake",
        "code": "dried-fungi",
        "price_cents": 3980,
        "stock": 150,
        "unit": "袋",
        "spec": "250g/袋（约 30 朵）",
        "grade": "一级",
        "origin": "福建省宁德市古田县",
        "shelf_life": "18 个月",
        "storage": "密封防潮，避免阳光直射",
        "tags": "椴木种植,自然晾晒,无硫熏",
        "recommended": False,
        "sales": 98,
        "description": "古田椴木香菇，菇盖厚实、菌褶细密，日晒干燥无硫熏，泡发后香气浓郁，适合炖汤与红烧。",
        "farm": ("古田菌菇合作社", "福建省", "宁德市", "古田县大桥镇椴木香菇基地", "approved"),
        "crop": "椴木香菇",
        "plot": "GT-06",
        "harvest": date(2026, 10, 12),
        "batch_no": "PC-20261012-ORA4",
        "event_note": "椴木打孔接种，山泉水喷雾保湿",
        "quality_note": "水分 11.3%，二氧化硫未检出",
    },
]


# ------------------------------------------------------------------ 内部工具
def _chain(db: Session, batch: Batch, events: list[tuple[str, str, str, datetime]], operator_id: str) -> None:
    """把环节记录串进哈希链并同步链尾锚点（与 app/db/seed.py 同一套算法）。"""
    prev_hash = HashChain.genesis_prev_hash(batch.batch_id)
    for seq_no, (stage, detail_text, location, occurred) in enumerate(events, start=1):
        detail = canonical_detail({"note": detail_text})
        curr_hash = HashChain.compute_hash(
            prev_hash=prev_hash,
            batch_id=batch.batch_id,
            seq_no=seq_no,
            stage=stage,
            operator_id=operator_id,
            occurred_at=occurred,
            detail=detail,
        )
        db.add(
            TraceEvent(
                batch_id=batch.batch_id,
                seq_no=seq_no,
                stage=stage,
                operator_id=operator_id,
                occurred_at=occurred,
                location=location,
                detail=detail,
                prev_hash=prev_hash,
                curr_hash=curr_hash,
            )
        )
        prev_hash = curr_hash
    batch.event_count = len(events)
    batch.last_event_hash = prev_hash


def _ensure_farm(db: Session, spec: tuple[str, str, str, str, str], owner_id: str) -> tuple[Farm, bool]:
    name, province, city, address, cert = spec
    farm = db.scalar(select(Farm).where(Farm.farm_name == name))
    if farm is not None:
        return farm, False
    farm = Farm(
        farm_name=name,
        owner_id=owner_id,
        province=province,
        city=city,
        address=address,
        area_mu=80,
        cert_status=cert,
    )
    db.add(farm)
    db.flush()
    return farm, True


def _ensure_batch(db: Session, farm: Farm, item: dict[str, Any], operator_id: str) -> tuple[Batch, bool]:
    batch = db.scalar(select(Batch).where(Batch.batch_no == item["batch_no"]))
    if batch is not None:
        return batch, False

    harvest: date = item["harvest"]
    batch = Batch(
        batch_no=item["batch_no"],
        farm_id=farm.farm_id,
        crop_name=item["crop"],
        plot_no=item["plot"],
        plant_date=harvest - timedelta(days=150),
        harvest_date=harvest,
        expected_yield=item.get("expected_yield", 1200),
        status=BatchStatus.QUALIFIED,
    )
    db.add(batch)
    db.flush()

    _chain(
        db,
        batch,
        [
            (
                Stage.PRODUCTION,
                item["event_note"],
                f"{farm.farm_name} {item['plot']} 地块",
                datetime.combine(harvest - timedelta(days=120), time(8, 30)),
            ),
            (
                Stage.QUALITY,
                item["quality_note"],
                f"{farm.city}农产品质量检测中心",
                datetime.combine(harvest, time(15, 20)),
            ),
            (
                Stage.SALES,
                "产地直供上架，一物一码可溯源",
                f"{farm.farm_name}直供店",
                datetime.combine(harvest + timedelta(days=2), time(10, 0)),
            ),
        ],
        operator_id,
    )
    db.add(
        TraceCode(
            code_value=f"{batch.batch_no}-BOX0001",
            code_type=CodeType.BOX,
            batch_id=batch.batch_id,
            item_no=1,
        )
    )
    db.flush()
    return batch, True


# ------------------------------------------------------------------ 对外入口
def seed_categories(db: Session) -> int:
    """写入默认分类树（按 code 判断，已存在则跳过），返回新增数量。"""
    created = 0
    for sort_no, node in enumerate(DEFAULT_CATEGORIES, start=1):
        parent = db.scalar(select(Category).where(Category.code == node["code"]))
        if parent is None:
            parent = Category(
                code=node["code"],
                name=node["name"],
                level=1,
                icon=node["icon"],
                image=node["image"],
                sort_no=sort_no * 10,
                enabled=1,
                description=node["description"],
            )
            db.add(parent)
            db.flush()
            created += 1
        for child_sort, (code, name, desc) in enumerate(node["children"], start=1):
            if db.scalar(select(Category).where(Category.code == code)) is not None:
                continue
            db.add(
                Category(
                    code=code,
                    name=name,
                    parent_id=parent.category_id,
                    level=2,
                    icon=node["icon"],
                    image=parent.image,
                    sort_no=child_sort * 10,
                    enabled=1,
                    description=desc,
                )
            )
            created += 1
    db.flush()
    return created


# 历史商品（app/db/seed.py 写入的 5 件）按名称补分类与配图，避免老库升级后全是「未归类」
_LEGACY_BACKFILL: list[tuple[str, str, str]] = [
    ("临安山核桃", "nuts-walnut", "walnut"),
    ("径山云雾茶", "tea-green", "green-tea"),
    ("径山红茶", "tea-black", "black-tea"),
    ("天目笋干", "dried-bamboo", "bamboo-shoot"),
]


def backfill_products(db: Session) -> int:
    """给没有分类/配图/规格的老商品补默认值（幂等，不覆盖已有值）。"""
    touched = 0
    categories = {c.code: c for c in db.scalars(select(Category)).all()}
    by_id = {c.category_id: c for c in categories.values()}
    for product in db.scalars(select(Product)).all():
        changed = False
        if product.category_id is None:
            for keyword, code, slug in _LEGACY_BACKFILL:
                if keyword in product.name and code in categories:
                    product.category_id = categories[code].category_id
                    product.category = categories[code].name
                    if not product.image_url:
                        product.image_url = f"/static/img/products/{slug}.svg"
                    changed = True
                    break
            else:
                fallback = categories.get("other")
                if fallback is not None and product.category in ("", "其他"):
                    product.category_id = fallback.category_id
                    product.category = fallback.name
                    changed = True
        if not product.image_url:
            # 有分类就借分类图，实在没有才用通用图，避免「核桃配橙子」的错配
            category = by_id.get(product.category_id or "")
            product.image_url = (
                category.image if category and category.image else "/static/img/products/other.svg"
            )
            changed = True
        if changed:
            touched += 1
    db.flush()
    return touched


def ensure_catalog(db: Session) -> dict[str, int]:
    """补齐分类树、演示商品与所属农场/批次/溯源码。返回本次各项新增数量。"""
    stats = {"categories": seed_categories(db), "products": 0, "farms": 0, "batches": 0}

    owner = db.scalar(select(User).where(User.username == "farmer01"))
    if owner is None:  # 连演示账号都没有的空库，交给 app/db/seed.py
        db.commit()
        return stats

    categories = {c.code: c for c in db.scalars(select(Category)).all()}
    for item in DEMO_PRODUCTS:
        if db.scalar(select(Product).where(Product.name == item["name"])) is not None:
            continue
        category = categories.get(item["code"])
        if category is None:
            continue

        farm, farm_created = _ensure_farm(db, item["farm"], owner.user_id)
        stats["farms"] += 1 if farm_created else 0

        batch, batch_created = _ensure_batch(db, farm, item, owner.user_id)
        stats["batches"] += 1 if batch_created else 0

        db.add(
            Product(
                batch_id=batch.batch_id,
                farm_id=farm.farm_id,
                name=item["name"],
                category=category.name,
                category_id=category.category_id,
                price_cents=item["price_cents"],
                stock=item["stock"],
                unit=item["unit"],
                status=ProductStatus.ON_SALE,
                certified=1 if farm.cert_status == "approved" else 0,
                description=item["description"],
                sales_count=item["sales"],
                spec=item["spec"],
                grade=item["grade"],
                origin=item["origin"],
                shelf_life=item["shelf_life"],
                storage=item["storage"],
                image_url=f"/static/img/products/{item['slug']}.svg",
                tags=item["tags"],
                is_recommended=1 if item["recommended"] else 0,
            )
        )
        stats["products"] += 1

    backfill_products(db)
    db.commit()
    return stats
