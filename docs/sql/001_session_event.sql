-- ============================================================================
-- 001 · 会话事件日志表（event sourcing 的账本）
--
-- 作用：把"会话状态"从一张可变的 chat_message 表，改成一条只追加的事件流。
--       chat_message 继续存在（影子写入），事件日志成为可回放的事实来源。
--
-- 执行方式（任选其一）：
--   psql -h localhost -U myuser -d mydb -f docs/sql/001_session_event.sql
--   docker exec -i <db容器名> psql -U myuser -d mydb < docs/sql/001_session_event.sql
--   DBeaver / pgAdmin / Navicat 里整段粘贴执行
--   python -m alembic upgrade head        （等价，见 alembic/versions/d8c31e5a7f42_*.py）
--
-- 幂等：可以重复执行。已存在则跳过。
-- 注意：本文件与 app/models/session_event.py 必须保持一致（列、类型、约束名）。
--       改模型就要同步改这里，或者干脆只保留一条建表路径。
-- ============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. 事件表
--
-- 设计说明：
--   seq            会话内单调递增，回放只认它，不认 created_at（同毫秒也能排稳）
--   type           字符串而非 DB 枚举：新增事件类型不需要 ALTER TYPE
--   data           JSONB：不同 type 有不同结构，演进时不需要加列
--   ignorable      写入时固化：老代码遇到不认识的类型，可跳过还是必须报错
--   schema_version data 的结构版本，为将来的事件迁移留依据
--
-- 刻意不设 DEFAULT：默认值只留在应用侧（一列两处默认就是漂移的开始）。
-- 手工插入请显式给全字段，示例：
--   INSERT INTO session_event (created_at, session_id, seq, turn_id, type, data, ignorable, schema_version)
--   VALUES (now() AT TIME ZONE 'utc', '<session-id>', 1, '<turn-id>',
--           'message/user', '{"content":"hi"}'::jsonb, false, 1);
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS session_event (
    created_at     TIMESTAMP WITHOUT TIME ZONE NOT NULL,
    id             SERIAL NOT NULL,
    session_id     VARCHAR NOT NULL,
    seq            INTEGER NOT NULL,
    turn_id        VARCHAR,
    type           VARCHAR NOT NULL,
    data           JSONB NOT NULL,
    ignorable      BOOLEAN NOT NULL,
    schema_version INTEGER NOT NULL,
    PRIMARY KEY (id),

    CONSTRAINT fk_session_event_session
        FOREIGN KEY (session_id) REFERENCES session (id),

    -- 同一会话内 seq 唯一：既是排序保证，也是并发写入的最后一道防线
    CONSTRAINT uq_session_event_session_seq UNIQUE (session_id, seq)
);

-- ---------------------------------------------------------------------------
-- 2. 索引
--
-- 不加单独的 (session_id) 索引：UNIQUE(session_id, seq) 的最左前缀已经覆盖它。
-- ---------------------------------------------------------------------------

-- 按类型回放 / 统计（例如只取 message/*）
CREATE INDEX IF NOT EXISTS ix_session_event_session_type
    ON session_event (session_id, type);

-- 按轮次聚合（一轮的所有事件）
CREATE INDEX IF NOT EXISTS ix_session_event_turn_id
    ON session_event (turn_id);

COMMIT;

-- ---------------------------------------------------------------------------
-- 3. 验收
-- ---------------------------------------------------------------------------
-- \d session_event
-- SELECT type, count(*) FROM session_event GROUP BY type ORDER BY 2 DESC;
--
-- 回填历史数据 + 校验投影：
--   python scripts/replay_session.py --backfill
--   python scripts/replay_session.py --verify --all
--
-- ---------------------------------------------------------------------------
-- 4. 回滚
-- ---------------------------------------------------------------------------
-- DROP TABLE IF EXISTS session_event;
