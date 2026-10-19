-- Incremental migration for the existing renwei_script schema.
-- Existing tables: clients, accounts, source_documents, fact_items, logic_patterns.
-- This migration only adds account-level deduplication and retrieval audit tables.
-- Run after database/001_create_renwei_script.sql.

USE renwei_script;

CREATE TABLE IF NOT EXISTS account_logic_usage (
  id CHAR(36) NOT NULL PRIMARY KEY,
  client_id CHAR(36) NOT NULL,
  account_id CHAR(36) NOT NULL,
  logic_pattern_id CHAR(36) NULL,
  content_id VARCHAR(160) NULL,
  content_title VARCHAR(255) NULL,
  logic_signature CHAR(64) NOT NULL,
  hook_signature CHAR(64) NULL,
  structure_signature CHAR(64) NULL,
  speech_signature CHAR(64) NULL,
  selling_point_signature CHAR(64) NULL,
  usage_status ENUM('used','candidate','rejected','archived') NOT NULL DEFAULT 'used',
  source_locator JSON NULL,
  used_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_account_logic_lookup (client_id, account_id, usage_status),
  KEY idx_account_logic_signature (client_id, account_id, logic_signature),
  KEY idx_account_logic_content (content_id),
  CONSTRAINT fk_account_logic_client FOREIGN KEY (client_id) REFERENCES clients(id),
  CONSTRAINT fk_account_logic_account FOREIGN KEY (account_id) REFERENCES accounts(id),
  CONSTRAINT fk_account_logic_pattern FOREIGN KEY (logic_pattern_id) REFERENCES logic_patterns(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS agent_retrieval_logs (
  id CHAR(36) NOT NULL PRIMARY KEY,
  request_id VARCHAR(160) NOT NULL,
  client_id CHAR(36) NULL,
  account_id CHAR(36) NULL,
  operation VARCHAR(80) NOT NULL,
  query_text TEXT NULL,
  result_count INT UNSIGNED NOT NULL DEFAULT 0,
  result_ids JSON NULL,
  warnings JSON NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  KEY idx_agent_logs_request (request_id),
  KEY idx_agent_logs_client (client_id),
  CONSTRAINT fk_agent_logs_client FOREIGN KEY (client_id) REFERENCES clients(id),
  CONSTRAINT fk_agent_logs_account FOREIGN KEY (account_id) REFERENCES accounts(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
