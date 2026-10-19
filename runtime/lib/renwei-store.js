'use strict';

const crypto = require('node:crypto');

function getConfig() {
  return {
    host: process.env.RENWEI_DB_HOST || '127.0.0.1',
    port: Number(process.env.RENWEI_DB_PORT || 3306),
    user: process.env.RENWEI_DB_USER || 'renwei',
    password: process.env.RENWEI_DB_PASSWORD || '',
    database: process.env.RENWEI_DB_NAME || 'renwei_script',
    waitForConnections: true,
    connectionLimit: Number(process.env.RENWEI_DB_POOL_SIZE || 5),
    charset: 'utf8mb4',
  };
}

function loadMysql() {
  try { return require('mysql2/promise'); }
  catch (error) {
    if (error.code !== 'MODULE_NOT_FOUND') throw error;
    return require(require.resolve('mysql2/promise', { paths: ['/root/.crabot/crabot-mcp-tools'] }));
  }
}

function uuid() { return crypto.randomUUID(); }
function bounded(value, fallback, max) { return Math.min(Math.max(Number(value) || fallback, 1), max); }

function createStore() {
  const mysql = loadMysql();
  const pool = mysql.createPool(getConfig());

  async function findClient(clientCodeOrName) {
    const [rows] = await pool.execute(
      'SELECT id, client_code, name AS client_name, status FROM clients WHERE client_code = ? OR name = ? LIMIT 1',
      [clientCodeOrName, clientCodeOrName],
    );
    return rows[0] || null;
  }

  async function searchClientFacts({ clientId, query, limit = 30 }) {
    const like = `%${query || ''}%`;
    const [rows] = await pool.execute(
      `SELECT id, category AS fact_group, title AS fact_key, fact_text AS fact_value,
              fact_type AS confirmation_status, permission_status AS permission_level,
              source_document_id, source_locator, evidence_text, usable_for_script
         FROM fact_items
        WHERE client_id = ? AND fact_type IN ('confirmed', 'pending', 'inference')
          AND (title LIKE ? OR fact_text LIKE ? OR evidence_text LIKE ?)
        ORDER BY fact_type = 'confirmed' DESC, updated_at DESC
        LIMIT ?`,
      [clientId, like, like, like, bounded(limit, 30, 100)],
    );
    return rows;
  }

  async function listClientFacts({ clientId, includePending = true, limit = 100 }) {
    const types = includePending ? ['confirmed', 'pending', 'inference'] : ['confirmed'];
    const placeholders = types.map(() => '?').join(',');
    const [rows] = await pool.execute(
      `SELECT id, category AS fact_group, title AS fact_key, fact_text AS fact_value,
              fact_type AS confirmation_status, permission_status AS permission_level,
              source_document_id, source_locator, evidence_text, usable_for_script
         FROM fact_items
        WHERE client_id = ? AND fact_type IN (${placeholders})
        ORDER BY category, title, updated_at DESC
        LIMIT ?`,
      [clientId, ...types, bounded(limit, 100, 500)],
    );
    return rows;
  }

  async function searchLogicPatterns({ query, platform, limit = 20 }) {
    const like = `%${query || ''}%`;
    const params = [like, like, like, like, like, like, like, like];
    let sql = `SELECT id, benchmark_video_id, industry, account_type,
                      pattern_name, applicable_goals, audience_emotion, core_mechanism,
                      structure_summary, opening_pattern, conflict_pattern, proof_pattern,
                      transition_pattern, cta_pattern, reusable_template, limitations,
                      evidence_status, tags
                 FROM logic_patterns
                WHERE evidence_status IN ('confirmed', 'inference', 'pending')
                  AND (pattern_name LIKE ? OR core_mechanism LIKE ? OR structure_summary LIKE ?
                       OR opening_pattern LIKE ? OR conflict_pattern LIKE ? OR proof_pattern LIKE ?
                       OR reusable_template LIKE ? OR limitations LIKE ?)`;
    if (platform) { sql += ' AND JSON_UNQUOTE(JSON_EXTRACT(tags, \'$.platform\')) = ?'; params.push(platform); }
    sql += ' ORDER BY evidence_status = \'confirmed\' DESC, updated_at DESC LIMIT ?';
    params.push(bounded(limit, 20, 100));
    const [rows] = await pool.execute(sql, params);
    return rows;
  }

  async function findAccountDuplicates({ clientId, accountId, signatures = {}, limit = 20 }) {
    const clauses = [];
    const params = [clientId, accountId];
    for (const key of ['logic_signature', 'hook_signature', 'structure_signature', 'speech_signature', 'selling_point_signature']) {
      if (signatures[key]) { clauses.push(`${key} = ?`); params.push(signatures[key]); }
    }
    if (!clauses.length) return [];
    const [rows] = await pool.execute(
      `SELECT id, account_id, logic_pattern_id, content_id, content_title,
              logic_signature, hook_signature, structure_signature, speech_signature,
              selling_point_signature, usage_status, source_locator, used_at
         FROM account_logic_usage
        WHERE client_id = ? AND account_id = ? AND usage_status IN ('used', 'candidate')
          AND (${clauses.join(' OR ')})
        ORDER BY used_at DESC LIMIT ?`,
      [...params, bounded(limit, 20, 100)],
    );
    return rows;
  }

  async function logRetrieval({ requestId, clientId, accountId, operation, query, resultIds, warnings }) {
    await pool.execute(
      `INSERT INTO agent_retrieval_logs
        (id, request_id, client_id, account_id, operation, query_text, result_count, result_ids, warnings)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
      [uuid(), requestId, clientId || null, accountId || null, operation, query || null,
       resultIds.length, JSON.stringify(resultIds), JSON.stringify(warnings || [])],
    );
  }

  return { pool, findClient, searchClientFacts, listClientFacts, searchLogicPatterns, findAccountDuplicates, logRetrieval };
}

module.exports = { createStore };
