-- =============================================================================
-- 仁维 Crabot Skill：抖音账号数据复盘 —— PostgreSQL 12+ 初始化脚本
-- 目标库：193 服务器 PostgreSQL 栈（独立 schema `dy`，不与其它 Skill 混表）
--
-- 用法 A（推荐，幂等）：python -m dyreview init-db
-- 用法 B（psql）：psql "$DYREVIEW_DB_DSN" -v ON_ERROR_STOP=1 -f config/schema.sql
--
-- 说明：
--   * 全表带 dy. 前缀，配置里 database.schema 默认 'dy'；
--     若改用其它 schema，init-db 会把脚本中的 `dy.` 自动替换。
--   * 所有时间为 timestamptz，统一按 UTC 存储，展示层按 skill.timezone 转换。
--   * token 字段存的是「信封字符串」：enc:<fernet密文> 或 raw:<明文>，
--     由 dyreview.secrets_box 负责封包/拆包；raw: 仅允许测试环境。
-- =============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS dy;

CREATE TABLE IF NOT EXISTS dy.schema_migration (
    version    integer     PRIMARY KEY,
    name       text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);

-- 公共：updated_at 自动维护
CREATE OR REPLACE FUNCTION dy.fn_touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;


-- -----------------------------------------------------------------------------
-- 1. 账号档案：一个客户抖音账号一行
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dy.account (
    id                bigserial   PRIMARY KEY,
    open_id           text        NOT NULL,                 -- 抖音 open_id（应用维度唯一）
    client_key        text        NOT NULL DEFAULT 'default',-- 多套开放平台应用时区分
    account_name      text        NOT NULL,                 -- 运营内部称呼，飞书里 @ 用这个名字
    douyin_id         text,                                 -- 抖音号（用户在 App 里能看到）
    nickname          text,                                 -- 抖音昵称
    avatar_url        text,
    org_id            text,                                 -- 客户/机构编号
    status            text        NOT NULL DEFAULT 'active', -- active|paused|reauth_required|revoked
    data_scope        text,                                 -- 授权时拿到的 scope 快照
    owner_feishu_ids  text[],                               -- 报告默认推送接收人 open_id
    feishu_chat_id    text,                                 -- 该账号复盘报告默认推送会话
    remark            text,
    last_collect_at   timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT account_open_id_uk UNIQUE (client_key, open_id),
    CONSTRAINT account_status_ck  CHECK (status IN ('active','paused','reauth_required','revoked'))
);
CREATE INDEX IF NOT EXISTS account_douyin_id_idx ON dy.account (douyin_id);
CREATE INDEX IF NOT EXISTS account_status_idx    ON dy.account (status) WHERE status = 'active';
DROP TRIGGER IF EXISTS trg_account_touch ON dy.account;
CREATE TRIGGER trg_account_touch BEFORE UPDATE ON dy.account
    FOR EACH ROW EXECUTE FUNCTION dy.fn_touch_updated_at();

COMMENT ON TABLE  dy.account IS '客户抖音账号档案（复盘的主体）';
COMMENT ON COLUMN dy.account.account_name IS '运营内部称呼；飞书指令“帮我复盘 环宇家居 账号数据”用它匹配';
COMMENT ON COLUMN dy.account.status IS 'reauth_required=refresh_token 也失效，必须让客户重新授权';


-- -----------------------------------------------------------------------------
-- 2. OAuth Token 缓存（一个账号一行，refresh_token 会轮换，必须回写）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dy.oauth_token (
    account_id         bigint      PRIMARY KEY REFERENCES dy.account(id) ON DELETE CASCADE,
    access_token       text        NOT NULL,                -- enc:... / raw:...
    refresh_token      text,                                -- 轮换后的新值会覆盖旧值
    access_expires_at  timestamptz NOT NULL,
    refresh_expires_at timestamptz,
    access_issued_at   timestamptz NOT NULL DEFAULT now(),
    access_ttl_seconds integer,                             -- 服务端下发的 expires_in，用于算预刷新窗口
    scope              text,
    refreshed_at       timestamptz NOT NULL DEFAULT now(),
    refresh_count      bigint      NOT NULL DEFAULT 0,
    version            bigint      NOT NULL DEFAULT 0,      -- 乐观锁：防止并发刷新写坏轮换后的 refresh_token
    last_refresh_error text,
    updated_at         timestamptz NOT NULL DEFAULT now()
);
DROP TRIGGER IF EXISTS trg_oauth_token_touch ON dy.oauth_token;
CREATE TRIGGER trg_oauth_token_touch BEFORE UPDATE ON dy.oauth_token
    FOR EACH ROW EXECUTE FUNCTION dy.fn_touch_updated_at();

COMMENT ON TABLE  dy.oauth_token IS 'access_token 缓存表：定时采集前先查这里，未过期直接用，过期则用 refresh_token 自动刷新';
COMMENT ON COLUMN dy.oauth_token.version IS '乐观锁；刷新成功后 version+1，冲突则重读重试，避免把已轮换的 refresh_token 写回旧值';


-- -----------------------------------------------------------------------------
-- 3. Token 事件审计（刷新/失败/需重新授权，全部留痕）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dy.oauth_event (
    id          bigserial   PRIMARY KEY,
    account_id  bigint      NOT NULL REFERENCES dy.account(id) ON DELETE CASCADE,
    event       text        NOT NULL,   -- cache_hit|cache_miss|refreshed|refresh_failed|renewed|reauth_required|revoked
    ok          boolean     NOT NULL DEFAULT true,
    error_code  text,
    description text,
    logid       text,                   -- 抖音返回的 logid，排障用
    detail      jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS oauth_event_account_idx ON dy.oauth_event (account_id, created_at DESC);


-- -----------------------------------------------------------------------------
-- 4. 采集批次（可观测性：每次定时任务/手动拉取一行）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dy.collect_run (
    id             bigserial   PRIMARY KEY,
    account_id     bigint      REFERENCES dy.account(id) ON DELETE CASCADE,
    kind           text        NOT NULL DEFAULT 'incremental', -- full|incremental|token_keepalive|fan_profile
    status         text        NOT NULL DEFAULT 'running',     -- running|success|partial|failed|skipped
    started_at     timestamptz NOT NULL DEFAULT now(),
    finished_at    timestamptz,
    api_calls      integer     NOT NULL DEFAULT 0,
    video_pages    integer     NOT NULL DEFAULT 0,
    videos_seen    integer     NOT NULL DEFAULT 0,
    videos_upserted integer    NOT NULL DEFAULT 0,
    token_refreshed boolean    NOT NULL DEFAULT false,
    error_code     text,
    error_msg      text,
    detail         jsonb
);
CREATE INDEX IF NOT EXISTS collect_run_account_idx ON dy.collect_run (account_id, started_at DESC);


-- -----------------------------------------------------------------------------
-- 5. 视频作品主表
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dy.video (
    account_id     bigint      NOT NULL REFERENCES dy.account(id) ON DELETE CASCADE,
    item_id        text        NOT NULL,
    title          text,
    description    text,
    cover_url      text,
    share_url      text,
    create_time    timestamptz,
    duration_sec   integer,
    media_type     text,
    is_top         boolean     NOT NULL DEFAULT false,
    topics         text[],                    -- 提取的 #话题
    first_seen_at  timestamptz NOT NULL DEFAULT now(),
    last_seen_at   timestamptz NOT NULL DEFAULT now(),
    raw            jsonb,
    PRIMARY KEY (account_id, item_id)
);
CREATE INDEX IF NOT EXISTS video_create_time_idx ON dy.video (account_id, create_time DESC);
CREATE INDEX IF NOT EXISTS video_topics_idx      ON dy.video USING gin (topics);

COMMENT ON TABLE dy.video IS '作品维度主表，只存相对静态的元信息；会变化的指标在 dy.video_metric';


-- -----------------------------------------------------------------------------
-- 6. 作品指标（按天快照，抖音返回的是累计值，涨跌由复盘层算差值）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dy.video_metric (
    account_id            bigint      NOT NULL,
    item_id               text        NOT NULL,
    stat_date             date        NOT NULL,
    play_count            bigint,
    like_count            bigint,
    comment_count         bigint,
    share_count           bigint,
    collect_count         bigint,
    forward_count         bigint,
    finish_rate           numeric(8,5),   -- 完播率 0~1
    full_play_rate        numeric(8,5),
    bounce_rate           numeric(8,5),   -- 2s 跳出率 0~1
    avg_play_duration_ms  integer,
    cover_click_rate      numeric(8,5),
    raw                   jsonb,
    collected_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, item_id, stat_date),
    FOREIGN KEY (account_id, item_id) REFERENCES dy.video (account_id, item_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS video_metric_date_idx ON dy.video_metric (account_id, stat_date DESC);

COMMENT ON TABLE dy.video_metric IS '作品指标日快照；同一 item_id 每天一行，重跑覆盖（幂等）';


-- -----------------------------------------------------------------------------
-- 7. 账号指标（按天快照）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dy.account_metric (
    account_id           bigint      NOT NULL REFERENCES dy.account(id) ON DELETE CASCADE,
    stat_date            date        NOT NULL,
    follower_count       bigint,
    new_follower_count   bigint,
    unfollower_count     bigint,
    total_favorited      bigint,
    video_count          integer,
    profile_visit_count  bigint,
    home_visit_count     bigint,
    raw                  jsonb,
    collected_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, stat_date)
);
CREATE INDEX IF NOT EXISTS account_metric_date_idx ON dy.account_metric (account_id, stat_date DESC);

COMMENT ON TABLE dy.account_metric IS '账号维度日快照：粉丝数、净增、主页访问等后台指标';


-- -----------------------------------------------------------------------------
-- 8. 粉丝画像（维度化，便于任意扩展）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dy.fan_profile (
    account_id  bigint      NOT NULL REFERENCES dy.account(id) ON DELETE CASCADE,
    stat_date   date        NOT NULL,
    dimension   text        NOT NULL,   -- gender|age|region|interest|device
    payload     jsonb       NOT NULL,   -- [{"bucket":"女","pct":0.62}, ...]
    collected_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, stat_date, dimension)
);


-- -----------------------------------------------------------------------------
-- 9. 复盘报告（一次复盘一行，贯穿 facts -> LLM -> 渲染 -> 推送）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dy.review (
    id             bigserial   PRIMARY KEY,
    account_id     bigint      NOT NULL REFERENCES dy.account(id) ON DELETE CASCADE,
    period_start   date        NOT NULL,
    period_end     date        NOT NULL,
    status         text        NOT NULL DEFAULT 'pending',
                   -- pending|facts_ready|rendered|pushed|failed|cancelled
    source         text        NOT NULL DEFAULT 'feishu',    -- feishu|cli|cron
    request_text   text,                                      -- 用户原始指令，便于回溯
    facts          jsonb,                                     -- 事实包（唯一数据源，LLM 不得越界）
    diagnosis      jsonb,                                     -- 规则引擎诊断结论
    prompt_md      text,                                      -- 交给大模型的 prompt（handoff 用）
    report_md      text,
    llm_model      text,
    llm_mode       text,                                      -- handoff|builtin
    input_tokens   integer,
    output_tokens  integer,
    latency_ms     integer,
    validation     jsonb,                                     -- 数字校验结果
    feishu_chat_id text,
    feishu_msg_ids text[],
    error          text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS review_account_idx ON dy.review (account_id, created_at DESC);
CREATE INDEX IF NOT EXISTS review_status_idx  ON dy.review (status);
DROP TRIGGER IF EXISTS trg_review_touch ON dy.review;
CREATE TRIGGER trg_review_touch BEFORE UPDATE ON dy.review
    FOR EACH ROW EXECUTE FUNCTION dy.fn_touch_updated_at();

COMMENT ON TABLE dy.review IS '复盘工单：facts 是唯一允许的事实来源，report_md 由大模型基于 facts 生成';


-- -----------------------------------------------------------------------------
-- 10. 大模型用量（公共约束：每次调用记录输入/输出 token）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dy.llm_usage (
    id            bigserial   PRIMARY KEY,
    skill         text        NOT NULL DEFAULT 'douyin-account-review',
    account_id    bigint      REFERENCES dy.account(id) ON DELETE SET NULL,
    review_id     bigint      REFERENCES dy.review(id) ON DELETE SET NULL,
    trace_id      text,
    model         text,
    mode          text,                    -- handoff|builtin
    input_tokens  integer     NOT NULL DEFAULT 0,
    output_tokens integer     NOT NULL DEFAULT 0,
    latency_ms    integer,
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS llm_usage_time_idx ON dy.llm_usage (created_at DESC);


-- -----------------------------------------------------------------------------
-- 11. 通用 KV（飞书 tenant_access_token 缓存、采集水位等）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dy.kv (
    key        text        PRIMARY KEY,
    value      jsonb       NOT NULL,
    expires_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);


-- -----------------------------------------------------------------------------
-- 12. 任务锁（防递归/防并发的持久化去重；短事务，不替代 advisory lock）
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dy.task_lock (
    lock_key    text        PRIMARY KEY,
    owner       text,
    acquired_at timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL
);


-- -----------------------------------------------------------------------------
-- 视图
-- -----------------------------------------------------------------------------

-- 每个作品的最新指标 + 相对上一快照的增量
CREATE OR REPLACE VIEW dy.v_video_latest AS
SELECT DISTINCT ON (m.account_id, m.item_id)
       m.account_id, m.item_id, v.title, v.create_time, v.duration_sec, v.topics,
       m.stat_date, m.play_count, m.like_count, m.comment_count, m.share_count,
       m.collect_count, m.finish_rate, m.bounce_rate, m.avg_play_duration_ms,
       m.cover_click_rate,
       CASE WHEN m.play_count > 0
            THEN round(((COALESCE(m.like_count,0) + COALESCE(m.comment_count,0)
                       + COALESCE(m.share_count,0) + COALESCE(m.collect_count,0))::numeric
                       / m.play_count), 5)
       END AS interaction_rate
FROM dy.video_metric m
JOIN dy.video v ON v.account_id = m.account_id AND v.item_id = m.item_id
ORDER BY m.account_id, m.item_id, m.stat_date DESC;

-- 账号日指标及 7 日前后对比
CREATE OR REPLACE VIEW dy.v_account_metric_delta AS
SELECT account_id, stat_date, follower_count, new_follower_count, unfollower_count,
       total_favorited, video_count, profile_visit_count,
       lag(follower_count, 7)  OVER w AS follower_count_7d_ago,
       lag(total_favorited, 7) OVER w AS total_favorited_7d_ago
FROM dy.account_metric
WINDOW w AS (PARTITION BY account_id ORDER BY stat_date);

-- Token 健康度（定时巡检 / doctor 用）
CREATE OR REPLACE VIEW dy.v_token_health AS
SELECT a.id            AS account_id,
       a.account_name,
       a.douyin_id,
       a.status        AS account_status,
       t.access_expires_at,
       t.refresh_expires_at,
       t.refresh_count,
       t.version,
       t.last_refresh_error,
       extract(epoch FROM (t.access_expires_at  - now())) / 3600 AS access_hours_left,
       extract(epoch FROM (t.refresh_expires_at - now())) / 3600 AS refresh_hours_left
FROM dy.account a
LEFT JOIN dy.oauth_token t ON t.account_id = a.id;

-- 数据新鲜度（复盘前必查：数据断更就不能出报告）
CREATE OR REPLACE VIEW dy.v_data_freshness AS
SELECT a.id AS account_id,
       a.account_name,
       a.last_collect_at,
       (SELECT max(r.started_at) FROM dy.collect_run r
         WHERE r.account_id = a.id AND r.status IN ('success','partial')) AS last_success_run_at,
       (SELECT max(m.stat_date) FROM dy.account_metric m WHERE m.account_id = a.id)  AS latest_account_stat_date,
       (SELECT max(m.stat_date) FROM dy.video_metric   m WHERE m.account_id = a.id)  AS latest_video_stat_date,
       (SELECT count(*) FROM dy.video v WHERE v.account_id = a.id)                   AS video_total
FROM dy.account a;

INSERT INTO dy.schema_migration (version, name)
VALUES (1, 'init: account/token/metric/video/review/usage')
ON CONFLICT (version) DO NOTHING;

COMMIT;
