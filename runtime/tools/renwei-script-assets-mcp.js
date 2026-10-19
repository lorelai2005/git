'use strict';

const crypto = require('node:crypto');
const { createStore } = require('../lib/renwei-store');

function dependency(name) {
  try { return require(name); }
  catch (error) {
    if (error.code !== 'MODULE_NOT_FOUND') throw error;
    return require(require.resolve(name, { paths: ['/root/.crabot/crabot-mcp-tools'] }));
  }
}

async function main() {
  const { McpServer } = dependency('@modelcontextprotocol/sdk/server/mcp.js');
  const { StdioServerTransport } = dependency('@modelcontextprotocol/sdk/server/stdio.js');
  const { z } = dependency('zod/v4');
  const store = createStore();
  const server = new McpServer({ name: 'renwei-script-assets', version: '1.0.0' });
  const requestId = () => crypto.randomUUID();
  const response = (data, isError = false) => ({ content: [{ type: 'text', text: JSON.stringify(data) }], isError });

  server.tool('retrieve_client_materials', '检索指定客户的结构化素材。默认返回已确认、待确认和受限事实，并保留来源、权限和状态；不得把待确认事实表述为已证实事实。', {
    client: z.string().min(1).max(160),
    query: z.string().max(500).optional(),
    confirmed_only: z.boolean().optional(),
    limit: z.number().int().min(1).max(100).optional(),
  }, async (input) => {
    try {
      const client = await store.findClient(input.client);
      if (!client) return response({ ok: false, error: 'CLIENT_NOT_FOUND' }, true);
      const rows = input.query
        ? await store.searchClientFacts({ clientId: client.id, query: input.query, limit: input.limit })
        : await store.listClientFacts({ clientId: client.id, includePending: !input.confirmed_only, limit: input.limit });
      const warnings = rows.filter(row => row.confirmation_status !== 'confirmed').map(row => `${row.fact_key}:${row.confirmation_status}`);
      await store.logRetrieval({ requestId: requestId(), clientId: client.id, operation: 'retrieve_client_materials', query: input.query, resultIds: rows.map(row => row.id), warnings });
      return response({ ok: true, client, count: rows.length, materials: rows, warnings, next_step: '生成脚本前必须检查 confirmation_status、permission_level、source_locator。' });
    } catch (error) { return response({ ok: false, error: error.message }, true); }
  });

  server.tool('retrieve_logic_patterns', '检索可复用的爆款逻辑记录。返回来源账号、证据状态和来源定位，不把逻辑推断当成已验证事实。', {
    query: z.string().min(1).max(500),
    platform: z.string().max(40).optional(),
    limit: z.number().int().min(1).max(100).optional(),
  }, async (input) => {
    try {
      const rows = await store.searchLogicPatterns(input);
      const warnings = rows.filter(row => row.evidence_status !== 'verified').map(row => `${row.logic_code}:${row.evidence_status}`);
      await store.logRetrieval({ requestId: requestId(), operation: 'retrieve_logic_patterns', query: input.query, resultIds: rows.map(row => row.id), warnings });
      return response({ ok: true, count: rows.length, logic_patterns: rows, warnings });
    } catch (error) { return response({ ok: false, error: error.message }, true); }
  });

  server.tool('check_account_logic_duplicates', '检查同一客户账号是否已经使用过相同或相近的逻辑签名。只返回历史匹配记录，不自动阻止脚本生成。', {
    client: z.string().min(1).max(160),
    account_id: z.string().min(1).max(160),
    logic_signature: z.string().length(64).optional(),
    hook_signature: z.string().length(64).optional(),
    structure_signature: z.string().length(64).optional(),
    speech_signature: z.string().length(64).optional(),
    selling_point_signature: z.string().length(64).optional(),
    limit: z.number().int().min(1).max(100).optional(),
  }, async (input) => {
    try {
      const client = await store.findClient(input.client);
      if (!client) return response({ ok: false, error: 'CLIENT_NOT_FOUND' }, true);
      const matches = await store.findAccountDuplicates({ clientId: client.id, accountId: input.account_id, signatures: input, limit: input.limit });
      await store.logRetrieval({ requestId: requestId(), clientId: client.id, accountId: input.account_id, operation: 'check_account_logic_duplicates', resultIds: matches.map(row => row.id), warnings: matches.length ? ['ACCOUNT_LOGIC_DUPLICATE_CANDIDATES'] : [] });
      return response({ ok: true, duplicate: matches.length > 0, count: matches.length, matches, recommendation: matches.length ? '提示重复并要求 Agent 重新选择切口或明确复用理由。' : '未发现同账号历史签名匹配。' });
    } catch (error) { return response({ ok: false, error: error.message }, true); }
  });

  await server.connect(new StdioServerTransport());
}

if (require.main === module) main().catch(error => { console.error('renwei-script-assets MCP failed:', error.message); process.exitCode = 1; });
