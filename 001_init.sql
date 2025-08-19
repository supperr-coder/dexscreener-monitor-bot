-- Users & chats
CREATE TABLE IF NOT EXISTS tg_users (
  user_id        BIGINT PRIMARY KEY,
  username       TEXT,
  first_seen_at  TIMESTAMPTZ DEFAULT now(),
  last_seen_at   TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS tg_chats (
  chat_id        BIGINT PRIMARY KEY,
  chat_type      TEXT NOT NULL,
  title          TEXT,
  first_seen_at  TIMESTAMPTZ DEFAULT now(),
  last_seen_at   TIMESTAMPTZ DEFAULT now()
);

-- Tokens
CREATE TABLE IF NOT EXISTS tokens (
  chain_id       TEXT    NOT NULL,
  token_address  TEXT    NOT NULL,
  symbol         TEXT,
  first_seen_at  TIMESTAMPTZ DEFAULT now(),
  PRIMARY KEY (chain_id, token_address)
);

-- Monitors
CREATE TABLE IF NOT EXISTS monitors (
  id             BIGSERIAL PRIMARY KEY,
  chat_id        BIGINT  NOT NULL REFERENCES tg_chats(chat_id) ON DELETE CASCADE,
  chain_id       TEXT    NOT NULL,
  token_address  TEXT    NOT NULL,
  threshold_pct  NUMERIC(10,4) NOT NULL DEFAULT 3.0,
  is_active      BOOLEAN NOT NULL DEFAULT TRUE,
  prev_price_usd NUMERIC(38,12),
  prev_price_at  TIMESTAMPTZ,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT fk_token FOREIGN KEY (chain_id, token_address)
    REFERENCES tokens(chain_id, token_address) ON DELETE RESTRICT
);

-- One active monitor per chat+token
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_monitor
ON monitors (chat_id, chain_id, token_address)
WHERE is_active;

-- Price samples (append-only time series)
CREATE TABLE IF NOT EXISTS prices (
  chain_id       TEXT    NOT NULL,
  token_address  TEXT    NOT NULL,
  ts             TIMESTAMPTZ NOT NULL,
  price_usd      NUMERIC(38,12) NOT NULL,
  source         TEXT DEFAULT 'dexscreener',
  PRIMARY KEY (chain_id, token_address, ts)
);

-- Helpful indexes
CREATE INDEX IF NOT EXISTS idx_prices_lookup
  ON prices (chain_id, token_address, ts DESC);

-- For fast retention deletes
CREATE INDEX IF NOT EXISTS idx_prices_ts
  ON prices (ts);

CREATE INDEX IF NOT EXISTS idx_monitors_active
  ON monitors (is_active, chat_id);

-- Alerts (optional audit)
CREATE TABLE IF NOT EXISTS alerts (
  id             BIGSERIAL PRIMARY KEY,
  monitor_id     BIGINT  NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
  ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
  price_usd      NUMERIC(38,12) NOT NULL,
  pct_change     NUMERIC(10,4) NOT NULL,
  message        TEXT
);
