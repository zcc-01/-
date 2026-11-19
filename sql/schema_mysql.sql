-- =============================================================================
-- 农产品溯源与产地直供电商平台 · 溯源链域与公共基础表结构（MySQL 8.0）
-- 开发环境用 SQLite 自动建表；本文件用于生产库手工建表 / 课程评审时展示 DDL。
-- 约定（报告 4.5）：主键为 32 位字符串雪花号；所有表带 created_at/updated_at；
--                  溯源记录表只允许 INSERT，不授予 UPDATE / DELETE。
-- =============================================================================

CREATE DATABASE IF NOT EXISTS agritrace
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE agritrace;

-- ---------------------------------------------------------------- 用户与产地
CREATE TABLE IF NOT EXISTS sys_user (
  user_id       VARCHAR(32)  NOT NULL COMMENT '用户标识',
  username      VARCHAR(32)  NOT NULL COMMENT '登录名',
  password_hash VARCHAR(128) NOT NULL COMMENT 'bcrypt 口令摘要',
  phone         VARCHAR(20)  DEFAULT NULL COMMENT '手机号（展示时脱敏）',
  display_name  VARCHAR(32)  NOT NULL DEFAULT '' COMMENT '显示名',
  role          VARCHAR(16)  NOT NULL COMMENT 'farmer/consumer/operator/regulator',
  status        VARCHAR(16)  NOT NULL DEFAULT 'active',
  created_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (user_id),
  UNIQUE KEY uq_user_username (username),
  KEY idx_user_role (role)
) ENGINE=InnoDB COMMENT='用户表';

CREATE TABLE IF NOT EXISTS farm (
  farm_id     VARCHAR(32)  NOT NULL COMMENT '产地标识',
  farm_name   VARCHAR(64)  NOT NULL COMMENT '产地名称',
  owner_id    VARCHAR(32)  NOT NULL COMMENT '所属农户',
  province    VARCHAR(32)  NOT NULL DEFAULT '',
  city        VARCHAR(32)  NOT NULL DEFAULT '',
  address     VARCHAR(128) NOT NULL DEFAULT '',
  area_mu     DECIMAL(10,2) DEFAULT NULL COMMENT '面积（亩）',
  cert_status VARCHAR(16)  NOT NULL DEFAULT 'pending' COMMENT 'pending/approved/rejected',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (farm_id),
  KEY idx_farm_owner (owner_id),
  CONSTRAINT fk_farm_owner FOREIGN KEY (owner_id) REFERENCES sys_user (user_id)
) ENGINE=InnoDB COMMENT='产地（农场）表';

-- ------------------------------------------------------------------ 溯源链域
CREATE TABLE IF NOT EXISTS batch (
  batch_id       VARCHAR(32)  NOT NULL COMMENT '批次标识',
  batch_no       VARCHAR(32)  NOT NULL COMMENT '对外批次号 PC-YYYYMMDD-XXXX',
  farm_id        VARCHAR(32)  NOT NULL COMMENT '产地',
  crop_name      VARCHAR(64)  NOT NULL COMMENT '品种',
  plot_no        VARCHAR(32)  NOT NULL COMMENT '地块编号',
  plant_date     DATE         NOT NULL COMMENT '播种日期',
  harvest_date   DATE         NOT NULL COMMENT '采收日期',
  expected_yield DECIMAL(10,2) DEFAULT NULL COMMENT '预计产量（kg）',
  status         VARCHAR(16)  NOT NULL DEFAULT 'draft' COMMENT 'draft/growing/harvested/qualified/closed',
  event_count    INT          NOT NULL DEFAULT 0 COMMENT '链尾序号锚点',
  last_event_hash CHAR(64)    DEFAULT NULL COMMENT '链尾摘要锚点',
  created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (batch_id),
  UNIQUE KEY uq_batch_no (batch_no),
  UNIQUE KEY uq_batch_identity (farm_id, plot_no, crop_name, harvest_date),
  KEY idx_batch_farm (farm_id),
  KEY idx_batch_status (status),
  CONSTRAINT fk_batch_farm FOREIGN KEY (farm_id) REFERENCES farm (farm_id)
) ENGINE=InnoDB COMMENT='生产批次表';

CREATE TABLE IF NOT EXISTS trace_event (
  event_id    VARCHAR(32)  NOT NULL COMMENT '记录标识',
  batch_id    VARCHAR(32)  NOT NULL COMMENT '所属批次',
  seq_no      INT          NOT NULL COMMENT '批次内序号，从 1 递增',
  stage       VARCHAR(16)  NOT NULL COMMENT 'production/processing/quality/logistics/sales',
  operator_id VARCHAR(32)  NOT NULL COMMENT '操作人',
  occurred_at DATETIME     NOT NULL COMMENT '环节发生时间（UTC，秒精度）',
  location    VARCHAR(128) DEFAULT NULL COMMENT '发生地点',
  detail      TEXT         NOT NULL COMMENT '环节明细，规范化 JSON',
  prev_hash   CHAR(64)     NOT NULL COMMENT '前一条摘要，首条为创世摘要',
  curr_hash   CHAR(64)     NOT NULL COMMENT '本条 SHA-256 摘要',
  created_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (event_id),
  UNIQUE KEY uq_event_seq (batch_id, seq_no),
  KEY idx_event_batch (batch_id),
  CONSTRAINT fk_event_batch FOREIGN KEY (batch_id) REFERENCES batch (batch_id)
) ENGINE=InnoDB COMMENT='溯源环节记录表（只追加）';

CREATE TABLE IF NOT EXISTS trace_code (
  code_id        VARCHAR(32) NOT NULL COMMENT '码标识',
  code_value     VARCHAR(64) NOT NULL COMMENT '码值（批次号+类型+四位序号派生）',
  code_type      VARCHAR(8)  NOT NULL COMMENT 'box/item',
  batch_id       VARCHAR(32) NOT NULL COMMENT '所属批次',
  item_no        INT         NOT NULL DEFAULT 0 COMMENT '批次内序号',
  status         VARCHAR(16) NOT NULL DEFAULT 'active' COMMENT 'active/revoked',
  revoked_at     DATETIME    DEFAULT NULL,
  revoked_reason VARCHAR(128) DEFAULT NULL,
  created_at     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (code_id),
  UNIQUE KEY uq_code_value (code_value),
  KEY idx_code_batch (batch_id, code_type),
  CONSTRAINT fk_code_batch FOREIGN KEY (batch_id) REFERENCES batch (batch_id)
) ENGINE=InnoDB COMMENT='溯源码表';

CREATE TABLE IF NOT EXISTS scan_log (
  log_id     VARCHAR(32) NOT NULL COMMENT '日志标识',
  code_value VARCHAR(64) NOT NULL COMMENT '被扫码值',
  batch_id   VARCHAR(32) DEFAULT NULL,
  user_id    VARCHAR(32) DEFAULT NULL,
  scanned_at DATETIME    NOT NULL COMMENT '扫码时间',
  province   VARCHAR(32) DEFAULT NULL,
  city       VARCHAR(32) DEFAULT NULL,
  device     VARCHAR(64) DEFAULT NULL COMMENT 'mobile/desktop',
  ip         VARCHAR(64) DEFAULT NULL,
  created_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (log_id),
  KEY idx_scan_code (code_value),
  KEY idx_scan_batch (batch_id, scanned_at)
) ENGINE=InnoDB COMMENT='扫码日志表';

-- ------------------------------------------------------------- 权限：只追加
-- 生产环境用独立账号，禁止对溯源记录做 UPDATE / DELETE（报告 3.6 数据防篡改）。
-- 说明：trace_code.status 的人工作废是唯一例外，需单独授权。
CREATE USER IF NOT EXISTS 'trace_app'@'%' IDENTIFIED BY 'change-me-in-env';
GRANT SELECT, INSERT ON agritrace.trace_event  TO 'trace_app'@'%';
GRANT SELECT, INSERT ON agritrace.scan_log     TO 'trace_app'@'%';
GRANT SELECT, INSERT ON agritrace.batch        TO 'trace_app'@'%';
GRANT SELECT, INSERT ON agritrace.trace_code   TO 'trace_app'@'%';
GRANT SELECT, INSERT, UPDATE ON agritrace.farm      TO 'trace_app'@'%';
GRANT SELECT, INSERT, UPDATE ON agritrace.sys_user  TO 'trace_app'@'%';
-- 作废溯源码时临时授权（或改为写入独立作废表，见 docs/数据库设计.md 的演进说明）
GRANT UPDATE (status, revoked_at, revoked_reason, updated_at) ON agritrace.trace_code TO 'trace_app'@'%';
FLUSH PRIVILEGES;

-- =============================================================================
-- 品类域与商品表（「完善农产品模块」新增，对应报告 UC-06/UC-07 的目录部分）
-- 说明：开发环境由 app/db/migrate.py 自动补列，本段供生产库手工建表与评审展示。
-- 约定：分类只有两级；商品同时保存 category_id（外键）与 category（名称快照），
--       名称快照让列表页/订单快照不必每次 join 分类表。
-- =============================================================================

CREATE TABLE IF NOT EXISTS product_category (
  category_id  VARCHAR(32)  NOT NULL COMMENT '分类标识',
  code         VARCHAR(32)  NOT NULL COMMENT '稳定对外编码，如 fruit-citrus',
  name         VARCHAR(32)  NOT NULL COMMENT '分类名称',
  parent_id    VARCHAR(32)  DEFAULT NULL COMMENT '上级分类，NULL 表示一级分类',
  level        TINYINT      NOT NULL DEFAULT 1 COMMENT '层级：1 大类 / 2 子类',
  icon         VARCHAR(8)   NOT NULL DEFAULT '' COMMENT '展示图标（emoji）',
  image        VARCHAR(200) NOT NULL DEFAULT '' COMMENT '分类配图',
  sort_no      INT          NOT NULL DEFAULT 0 COMMENT '同级排序，小的在前',
  enabled      TINYINT      NOT NULL DEFAULT 1 COMMENT '1 启用 / 0 停用（停用父类级联停用子类）',
  description  VARCHAR(200) NOT NULL DEFAULT '' COMMENT '分类说明',
  created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (category_id),
  UNIQUE KEY uq_category_code (code),
  UNIQUE KEY uq_category_name_per_parent (parent_id, name),
  KEY idx_category_parent (parent_id),
  KEY idx_category_enabled (enabled)
) ENGINE=InnoDB COMMENT='商品分类（两级目录）';

CREATE TABLE IF NOT EXISTS product (
  product_id     VARCHAR(32)  NOT NULL COMMENT '商品标识',
  batch_id       VARCHAR(32)  NOT NULL COMMENT '关联溯源批次（上架即挂链）',
  farm_id        VARCHAR(32)  NOT NULL COMMENT '所属产地',
  name           VARCHAR(64)  NOT NULL COMMENT '商品名称',
  category       VARCHAR(32)  NOT NULL DEFAULT '其他' COMMENT '分类名称快照',
  category_id    VARCHAR(32)  DEFAULT NULL COMMENT '分类外键（product_category）',
  price_cents    INT          NOT NULL COMMENT '单价，单位分',
  stock          INT          NOT NULL DEFAULT 0 COMMENT '库存',
  unit           VARCHAR(16)  NOT NULL DEFAULT '份' COMMENT '计价单位',
  status         VARCHAR(16)  NOT NULL DEFAULT 'on_sale' COMMENT 'on_sale/off_shelf',
  certified      TINYINT      NOT NULL DEFAULT 0 COMMENT '产地认证标识（运营域回写）',
  description    TEXT         NOT NULL COMMENT '商品详情',
  sales_count    INT          NOT NULL DEFAULT 0 COMMENT '销量',
  spec           VARCHAR(32)  NOT NULL DEFAULT '' COMMENT '规格，如 5kg/箱',
  grade          VARCHAR(16)  NOT NULL DEFAULT '' COMMENT '等级，如 一级果',
  origin         VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '产地文本，如 江西省赣州市信丰县',
  shelf_life     VARCHAR(32)  NOT NULL DEFAULT '' COMMENT '保质期',
  storage        VARCHAR(64)  NOT NULL DEFAULT '' COMMENT '储存方式',
  image_url      VARCHAR(200) NOT NULL DEFAULT '' COMMENT '商品配图（本地静态或外链）',
  tags           VARCHAR(128) NOT NULL DEFAULT '' COMMENT '逗号分隔标签，参与关键词检索',
  is_recommended TINYINT      NOT NULL DEFAULT 0 COMMENT '首页推荐位',
  created_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (product_id),
  KEY idx_product_batch (batch_id),
  KEY idx_product_farm (farm_id),
  KEY idx_product_category_id (category_id),
  KEY idx_product_status (status),
  KEY idx_product_name (name),
  KEY idx_product_recommended (is_recommended)
) ENGINE=InnoDB COMMENT='商品（必须挂在已采收批次上）';
