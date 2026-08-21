/**
 * Semantic conventions for AI agent observability.
 *
 * Where OpenTelemetry's GenAI conventions exist we use them verbatim
 * (`gen_ai.request.model`, `gen_ai.usage.input_tokens`, ...). Everything the
 * spec doesn't cover yet — tool retries, citation coverage, n8n execution
 * ids — is namespaced so it can't collide later when upstream catches up.
 */

export const ATTR = {
  // ---- agent -------------------------------------------------------------
  AGENT_NAME: 'gen_ai.agent.name',
  AGENT_VERSION: 'gen_ai.agent.version',
  AGENT_OPERATION: 'gen_ai.operation.name',
  AGENT_WORKFLOW_ID: 'gen_ai.workflow.id',
  AGENT_STEP_NAME: 'gen_ai.workflow.step.name',
  AGENT_STEP_INDEX: 'gen_ai.workflow.step.index',

  // ---- tools -------------------------------------------------------------
  TOOL_NAME: 'gen_ai.tool.name',
  TOOL_DESCRIPTION: 'gen_ai.tool.description',
  TOOL_PARAMETERS: 'gen_ai.tool.parameters',
  TOOL_CALL_ID: 'gen_ai.tool.call_id',
  TOOL_STATUS: 'gen_ai.tool.status', // success | error | retry
  TOOL_RETRY_COUNT: 'gen_ai.tool.retry_count',
  TOOL_DURATION_MS: 'gen_ai.tool.duration_ms',
  TOOL_COST_USD: 'gen_ai.tool.cost_usd',

  // ---- LLM ---------------------------------------------------------------
  LLM_MODEL: 'gen_ai.request.model',
  LLM_TEMPERATURE: 'gen_ai.request.temperature',
  LLM_MAX_TOKENS: 'gen_ai.request.max_tokens',
  LLM_RESPONSE_ID: 'gen_ai.response.id',
  LLM_RESPONSE_MODEL: 'gen_ai.response.model',
  LLM_FINISH_REASON: 'gen_ai.response.finish_reason',
  LLM_INPUT_TOKENS: 'gen_ai.usage.input_tokens',
  LLM_OUTPUT_TOKENS: 'gen_ai.usage.output_tokens',
  LLM_TOTAL_TOKENS: 'gen_ai.usage.total_tokens',
  LLM_COST_USD: 'gen_ai.response.cost_usd',

  // ---- vector stores -----------------------------------------------------
  VECTOR_OPERATION: 'db.operation.name',
  VECTOR_COLLECTION: 'db.collection.name',
  VECTOR_TOP_K: 'db.vector.top_k',
  VECTOR_SCORE_THRESHOLD: 'db.vector.score_threshold',
  VECTOR_RESULTS_COUNT: 'db.vector.results_count',

  // ---- answer quality ----------------------------------------------------
  HAS_CITATIONS: 'gen_ai.response.has_citations',
  CITATION_COVERAGE: 'gen_ai.response.citation_coverage',
  CITATIONS: 'gen_ai.response.citations',
  SOURCES: 'gen_ai.response.sources',
  IS_REFUSAL: 'gen_ai.response.is_refusal',
  REFUSAL_REASON: 'gen_ai.response.refusal_reason',

  // ---- n8n ---------------------------------------------------------------
  N8N_WORKFLOW_ID: 'n8n.workflow.id',
  N8N_EXECUTION_ID: 'n8n.execution.id',
  N8N_NODE_NAME: 'n8n.node.name',
  N8N_NODE_TYPE: 'n8n.node.type',
} as const;

export type ToolStatus = 'success' | 'error' | 'retry';

/** Latency buckets tuned for agent workloads (seconds). */
export const LATENCY_BUCKETS = {
  agent: [0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300],
  tool: [0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60],
  llm: [0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300],
  vector: [0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5],
} as const;

/**
 * Per-million-token pricing. Keep this in one place — it's the only thing
 * standing between you and a surprise invoice. Update when providers change.
 */
export const MODEL_PRICING: Record<string, { input: number; output: number }> = {
  'gpt-4o': { input: 2.5, output: 10 },
  'gpt-4o-mini': { input: 0.15, output: 0.6 },
  'gpt-4.1': { input: 2, output: 8 },
  'gpt-4.1-mini': { input: 0.4, output: 1.6 },
  'o3-mini': { input: 1.1, output: 4.4 },
  'claude-sonnet-4': { input: 3, output: 15 },
  'claude-haiku-3.5': { input: 0.8, output: 4 },
  'gemini-2.5-flash': { input: 0.3, output: 2.5 },
  'gemini-2.5-pro': { input: 1.25, output: 10 },
  'llama-3.3-70b': { input: 0.6, output: 0.7 },
  'text-embedding-3-small': { input: 0.02, output: 0 },
  'text-embedding-3-large': { input: 0.13, output: 0 },
};

export function priceCall(
  model: string,
  inputTokens: number,
  outputTokens: number,
): number {
  const key = Object.keys(MODEL_PRICING).find((m) => model.toLowerCase().includes(m));
  if (!key) return 0;
  const p = MODEL_PRICING[key];
  return (inputTokens / 1_000_000) * p.input + (outputTokens / 1_000_000) * p.output;
}
