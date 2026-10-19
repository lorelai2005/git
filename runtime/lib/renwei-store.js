'use strict';

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

function createStore() {
  const mysql = loadMysql();
  const pool = mysql.createPool(getConfig());

  async function findClient(clientCodeOrName) {
    const [rows] = await pool.execute(
      'SELECT id, client_code, client_name, status FROM clients WHERE client_code = ? OR client_name = ? LIMIT 1',
      [clientCodeOrName, clientCodeOrName],
    );
    return rows[0] || null;
  }

  async function searchClientFacts({ clientId, query, limit = 30 }) {
    const like = `%${query || ''}%`;
    const [rows] = await pool.execute(
      `SELECT id, fact_group, fact_key, fact_value, confirmation_status, permission_level,
              sensitivity_level, source_document_id, source_locator, valid_from, valid_to
         FROM client_facts
        WHERE client_id = ?
          AND confirmation_status IN ('confirmed', 'pending', 'restricted')
          AND (fact_key LIKE ? OR fact_value LIKE ?)
        ORDER BY confirmation_status = 'confirmed' DESC, updated_at DESC
        LIMIT ?`,
      [clientId, like, like, Math.min(Math.max(Number(limit) || 30, 1), 100)],
    );
    return rows;
  }

  async function listClientFacts({ clientId, includePending = true, limit = 100 }) {
    const statuses = includePending ? ['confirmed', 'pending', 'restricted'] : ['confirmed'];
    const placeholders = statuses.map(() => '?').join(',');
    const [rows] = await pool.execute(
      `SELECT id, fact_group, fact_key, fact_value, confirmation_status, permission_level,
              sensitivity_level, source_document_id, source_locator, valid_from, valid_to
         FROM client_facts
        WHERE client_id = ? AND confirmation_status IN (${placeholders})
        ORDER BY fact_group, fact_key, updated_at DESC
        LIMIT ?`,
      [clientId, ...statuses, Math.min(Math.max(Number(limit) || 100, 1), 500)],
    );
    return rows;
  }

  async function searchLogicPatterns({ query, platform, limit = 20 }) {
    const like = `%${query || ''}%`;
    const params = [like, like, like, like, like, like, like];
    let sql = `SELECT id, logic_code, source_url, source_account_id, source_account_name, platform,
                      title, hook, structure, speech_style, rhythm, selling_points, target_audience,
                      evidence_status, source_locator, logic_signature
                 FROM logic_patterns
                WHERE evidence_status IN ('verified', 'inference', 'pending')
                  AND (title LIKE ? OR hook LIKE ? OR structure LIKE ? OR speech_style LIKE ?
                       OR rhythm LIKE ? OR selling_points LIKE ? OR target_audience LIKE ?)`;
    if (platform) { sql += ' AND platform = ?'; params.push(platform); }
    sql += ' ORDER BY evidence_status = \'verified\' DESC, updated_at DESC LIMIT ?';
    params.push(Math.min(Math.max(Number(limit) || 20, 1), 100));
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
      `SELECT id, account_id, account_name, logic_pattern_id, content_id, content_title,
              logic_signature, hook_signature, structure_signature, speech_signature,
              selling_point_signature, usage_status, source_locator, used_at
         FROM account_logic_usage
        WHERE client_id = ? AND account_id = ? AND usage_status IN ('used', 'candidate')
          AND (${clauses.join(' OR ')})
        ORDER BY used_at DESC LIMIT ?`,
      [...params, Math.min(Math.max(Number(limit) || 20, 1), 100)],
    );
    return rows;
  }

  async function logRetrieval({ requestId, clientId, accountId, operation, query, resultIds, warnings }) {
    await pool.execute(
      `INSERT INTO agent_retrieval_logs
        (request_id, client_id, account_id, operation, query_text, result_count, result_ids, warnings)
       VALUES (?, ?, ?, ?, ?, ?, CAST(? AS JSON), CAST(? AS JSON))`,
      [requestId, clientId || null, accountId || null, operation, query || null,
       resultIds.length, JSON.stringify(resultIds), JSON.stringify(warnings || [])],
    );
  }

  return { pool, findClient, searchClientFacts, listClientFacts, searchLogicPatterns, findAccountDuplicates, logRetrieval };
}

module.exports = { createStore };
