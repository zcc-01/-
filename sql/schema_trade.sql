-- =============================================================================
-- 农产品溯源与产地直供电商平台 · 交易域表结构（MySQL 8.0）
-- 负责人：李××    用例：UC-06 商品上架 / UC-07 检索筛选 / UC-08 下单与支付
--                        UC-09 发货与收货 / UC-10 评价晒单
--
-- 与报告表 4-3「数据库表清单」对齐，交易域共 5 张表：
--   product / orders / order_item / payment / review
-- 开发环境由 SQLAlchemy 的 create_all 自动建表（SQLite），本文件用于生产库与评审展示 DDL。
--
-- 三条与业务规则强绑定的设计，改表前请先看注释：
--   1. product.stock 的扣减一律通过条件更新完成，本文件末尾给出标准语句；
--   2. payment.order_id 唯一索引 —— BR-04「同一订单仅允许一条成功流水」的落地点；
--   3. review(order_id, product_id) 唯一 —— 一单一商品只能评价一次。
-- =============================================================================

CREATE DATABASE IF NOT EXISTS agritrace
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE agritrace;

-- ------------------------------------------------------------------ 商品（UC-06）
CREATE TABLE IF NOT EXISTS product (
  product_id   VARCHAR(32)   NOT NULL COMMENT '商品标识（雪花字符串）',
  product_no   VARCHAR(32)   NOT NULL COMMENT '对外商品编号 SP-YYYYMMDD-XXXX',
  farm_id      VARCHAR(32)   NOT NULL COMMENT '供货产地',
  batch_id     VARCHAR(32)   DEFAULT NULL COMMENT '关联生产批次，可空（非溯源品类）',
  title        VARCHAR(128)  NOT NULL COMMENT '商品标题',
  crop_name    VARCHAR(64)   NOT NULL COMMENT '品种',
  spec         VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '规格，如 500g/袋',
  unit         VARCHAR(16)   NOT NULL DEFAULT '份' COMMENT '计价单位',
  price        DECIMAL(10,2) NOT NULL COMMENT '单价（元）',
  stock        INT           NOT NULL DEFAULT 0 COMMENT '可售库存',
  sales        INT           NOT NULL DEFAULT 0 COMMENT '累计销量（支付成功后累加）',
  status       VARCHAR(16)   NOT NULL DEFAULT 'off_shelf' COMMENT 'on_shelf/off_shelf',
  cert_badge   VARCHAR(32)   NOT NULL DEFAULT '' COMMENT '产地认证标识，由认证审核回填',
  description  TEXT          NOT NULL COMMENT '商品描述',
  created_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (product_id),
  UNIQUE KEY uq_product_no (product_no),
  UNIQUE KEY uq_product_identity (farm_id, batch_id, title, spec),
  KEY idx_product_farm (farm_id),
  KEY idx_product_status (status),
  KEY idx_product_price (price),
  CONSTRAINT fk_product_farm  FOREIGN KEY (farm_id)  REFERENCES farm (farm_id),
  CONSTRAINT fk_product_batch FOREIGN KEY (batch_id) REFERENCES batch (batch_id)
) ENGINE=InnoDB COMMENT='商品表（一行 = 一个可售 SKU）';

-- -------------------------------------------------------------- 订单主表（UC-08）
CREATE TABLE IF NOT EXISTS orders (
  order_id         VARCHAR(32)   NOT NULL COMMENT '订单标识，对外单号为 order_no',
  order_no         VARCHAR(32)   NOT NULL COMMENT '对外订单号 ORD-YYYYMMDD-XXXX',
  buyer_id         VARCHAR(32)   NOT NULL COMMENT '买家',
  farm_id          VARCHAR(32)   NOT NULL COMMENT '供货产地（一单一产地，跨产地自动拆单）',
  total_amount     DECIMAL(10,2) NOT NULL DEFAULT 0 COMMENT '商品金额合计',
  freight          DECIMAL(10,2) NOT NULL DEFAULT 0 COMMENT '运费',
  pay_amount       DECIMAL(10,2) NOT NULL DEFAULT 0 COMMENT '应付 = 商品金额 + 运费，落库防重算偏差',
  status           VARCHAR(16)   NOT NULL DEFAULT 'pending' COMMENT 'pending/paid/shipped/received/cancelled',
  address_snapshot VARCHAR(256)  NOT NULL COMMENT '下单时地址快照，地址后续修改不影响历史订单',
  remark           VARCHAR(128)  NOT NULL DEFAULT '' COMMENT '买家备注',
  express_no       VARCHAR(64)   DEFAULT NULL COMMENT '物流单号（发货时写入）',
  paid_at          DATETIME      DEFAULT NULL,
  shipped_at       DATETIME      DEFAULT NULL,
  received_at      DATETIME      DEFAULT NULL,
  cancelled_at     DATETIME      DEFAULT NULL,
  cancel_reason    VARCHAR(128)  DEFAULT NULL,
  created_at       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at       DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (order_id),
  UNIQUE KEY uq_order_no (order_no),
  KEY idx_order_buyer (buyer_id, created_at),
  KEY idx_order_farm (farm_id, status),
  KEY idx_order_status (status, created_at),
  CONSTRAINT fk_order_buyer FOREIGN KEY (buyer_id) REFERENCES sys_user (user_id),
  CONSTRAINT fk_order_farm  FOREIGN KEY (farm_id)  REFERENCES farm (farm_id)
) ENGINE=InnoDB COMMENT='订单主表';

-- -------------------------------------------------------------- 订单明细（UC-08）
CREATE TABLE IF NOT EXISTS order_item (
  item_id       VARCHAR(32)   NOT NULL COMMENT '明细标识',
  order_id      VARCHAR(32)   NOT NULL COMMENT '所属订单',
  product_id    VARCHAR(32)   NOT NULL COMMENT '商品（不建外键，允许商品下架后历史订单仍可读）',
  batch_id      VARCHAR(32)   DEFAULT NULL COMMENT '下单时的批次快照，评价与溯源串联用',
  product_title VARCHAR(128)  NOT NULL COMMENT '商品标题快照',
  spec          VARCHAR(32)   NOT NULL DEFAULT '',
  unit          VARCHAR(16)   NOT NULL DEFAULT '份',
  unit_price    DECIMAL(10,2) NOT NULL COMMENT '成交单价快照',
  quantity      INT           NOT NULL COMMENT '数量',
  subtotal      DECIMAL(10,2) NOT NULL COMMENT '小计 = 单价 × 数量',
  created_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (item_id),
  KEY idx_item_order (order_id),
  KEY idx_item_product (product_id),
  CONSTRAINT fk_item_order FOREIGN KEY (order_id) REFERENCES orders (order_id)
) ENGINE=InnoDB COMMENT='订单明细表（快照式，商品改价不影响历史订单）';

-- -------------------------------------------------------------- 支付流水（UC-08）
CREATE TABLE IF NOT EXISTS payment (
  payment_id    VARCHAR(32)   NOT NULL COMMENT '流水标识',
  payment_no    VARCHAR(32)   NOT NULL COMMENT '对外支付单号 PAY-YYYYMMDD-XXXX',
  order_id      VARCHAR(32)   NOT NULL COMMENT '所属订单（唯一：一单一行）',
  channel       VARCHAR(16)   NOT NULL DEFAULT 'mockpay' COMMENT 'mockpay/mock_alipay/mock_wechat',
  amount        DECIMAL(10,2) NOT NULL COMMENT '支付金额',
  status        VARCHAR(16)   NOT NULL DEFAULT 'initiated' COMMENT 'initiated/success/failed',
  attempt_count INT           NOT NULL DEFAULT 0 COMMENT '尝试次数，上限 3 次（UC-08 异常流程 7a）',
  trade_no      VARCHAR(64)   DEFAULT NULL COMMENT '网关交易号',
  fail_reason   VARCHAR(128)  DEFAULT NULL COMMENT '最近一次失败原因',
  paid_at       DATETIME      DEFAULT NULL,
  created_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME      NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (payment_id),
  UNIQUE KEY uq_payment_no (payment_no),
  UNIQUE KEY uq_payment_order (order_id) COMMENT 'BR-04：同一订单仅允许一条成功流水',
  KEY idx_payment_status (status),
  CONSTRAINT fk_payment_order FOREIGN KEY (order_id) REFERENCES orders (order_id)
) ENGINE=InnoDB COMMENT='支付流水表';

-- ---------------------------------------------------------------- 评价（UC-10）
CREATE TABLE IF NOT EXISTS review (
  review_id  VARCHAR(32)  NOT NULL COMMENT '评价标识',
  order_id   VARCHAR(32)  NOT NULL COMMENT '所属订单',
  product_id VARCHAR(32)  NOT NULL COMMENT '被评商品',
  batch_id   VARCHAR(32)  DEFAULT NULL COMMENT '关联批次，支撑「按批次看口碑」',
  user_id    VARCHAR(32)  NOT NULL COMMENT '评价人（展示时脱敏）',
  rating     TINYINT      NOT NULL COMMENT '评分 1~5',
  content    VARCHAR(500) NOT NULL DEFAULT '' COMMENT '评价内容',
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (review_id),
  UNIQUE KEY uq_review_order_product (order_id, product_id),
  KEY idx_review_product (product_id, created_at),
  KEY idx_review_batch (batch_id),
  CONSTRAINT fk_review_order FOREIGN KEY (order_id) REFERENCES orders (order_id)
) ENGINE=InnoDB COMMENT='评价表（收货后一条订单一个商品仅一条）';

-- ------------------------------------------------------- 库存扣减与权限（运维约定）
-- 标准扣减语句：以受影响行数判断成败，affected_rows = 0 即「已下架或库存不足」，事务整体回滚。
--   UPDATE product SET stock = stock - :n
--    WHERE product_id = :pid AND status = 'on_shelf' AND stock >= :n;
-- 取消/超时释放库存（BR-06：库存只允许经状态流转释放，禁止人工改数）：
--   UPDATE product SET stock = stock + :n WHERE product_id = :pid;
-- 销量只在支付成功后累加，避免未支付订单污染销量口径：
--   UPDATE product SET sales = sales + :n WHERE product_id = :pid;

-- 应用账号只授予必需权限：溯源表仍按溯源域的约定只给 SELECT/INSERT。
CREATE USER IF NOT EXISTS 'trade_app'@'%' IDENTIFIED BY 'change-me-in-env';
GRANT SELECT, INSERT, UPDATE ON agritrace.product    TO 'trade_app'@'%';
GRANT SELECT, INSERT, UPDATE ON agritrace.orders     TO 'trade_app'@'%';
GRANT SELECT, INSERT         ON agritrace.order_item TO 'trade_app'@'%';
GRANT SELECT, INSERT, UPDATE ON agritrace.payment    TO 'trade_app'@'%';
GRANT SELECT, INSERT         ON agritrace.review     TO 'trade_app'@'%';
GRANT SELECT                 ON agritrace.farm       TO 'trade_app'@'%';
GRANT SELECT                 ON agritrace.batch      TO 'trade_app'@'%';
GRANT SELECT                 ON agritrace.sys_user   TO 'trade_app'@'%';
FLUSH PRIVILEGES;
