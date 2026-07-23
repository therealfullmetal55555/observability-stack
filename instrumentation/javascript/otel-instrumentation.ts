/**
 * OpenTelemetry instrumentation for Node/TypeScript AI agents.
 *
 * Built for the shape of code we actually run: an HTTP handler that calls a
 * chain, the chain calls tools, tools hit a vector DB, and somewhere in the
 * middle a model gets billed. Every wrapper below follows the same pattern —
 * open a span, record the metric, close the span, never swallow the error.
 *
 *   import { initTelemetry, traceAgent, traceLlm } from './otel-instrumentation';
 *   initTelemetry('rag-support-bot');
 */

import { NodeSDK } from '@opentelemetry/sdk-node';
import { getNodeAutoInstrumentations } from '@opentelemetry/auto-instrumentations-node';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-grpc';
import { OTLPMetricExporter } from '@opentelemetry/exporter-metric-otlp-grpc';
import { PeriodicExportingMetricReader } from '@opentelemetry/sdk-metrics';
import { Resource } from '@opentelemetry/resources';
import {
  SEMRESATTRS_SERVICE_NAME,
  SEMRESATTRS_SERVICE_VERSION,
  SEMRESATTRS_DEPLOYMENT_ENVIRONMENT,
} from '@opentelemetry/semantic-conventions';
import {
  trace,
  metrics,
  context,
  propagation,
  Span,
  SpanKind,
  SpanStatusCode,
  Attributes,
  Counter,
  Histogram,
  UpDownCounter,
  Meter,
} from '@opentelemetry/api';

import { ATTR, LATENCY_BUCKETS, ToolStatus, priceCall } from './semantic-conventions';

// ---------------------------------------------------------------------------
// Metrics
// ---------------------------------------------------------------------------

export interface AiMetrics {
  agentCalls: Counter;
  agentLatency: Histogram;
  toolCalls: Counter;
  toolLatency: Histogram;
  toolRetries: Counter;
  llmCalls: Counter;
  llmLatency: Histogram;
  llmTokens: Counter;
  llmCost: Counter;
  vectorOps: Counter;
  vectorLatency: Histogram;
  agentUp: UpDownCounter;
  citationCoverage: Histogram;
  refusals: Counter;
  responses: Counter;
}

let _metrics: AiMetrics | null = null;

export function getMeter(name = 'ai-agent-instrumentation'): Meter {
  return metrics.getMeter(name, '1.0.0');
}

export function getAiMetrics(meter: Meter = getMeter()): AiMetrics {
  if (_metrics) return _metrics;
  _metrics = {
    agentCalls: meter.createCounter('gen_ai_agent_calls_total', {
      description: 'Agent invocations, labelled by operation and status',
    }),
    agentLatency: meter.createHistogram('gen_ai_agent_latency_seconds', {
      description: 'End-to-end agent latency',
      unit: 's',
      advice: { explicitBucketBoundaries: [...LATENCY_BUCKETS.agent] },
    }),
    toolCalls: meter.createCounter('gen_ai_tool_calls_total', {
      description: 'Tool invocations, labelled by tool and status',
    }),
    toolLatency: meter.createHistogram('gen_ai_tool_call_duration_seconds', {
      description: 'Tool call latency',
      unit: 's',
      advice: { explicitBucketBoundaries: [...LATENCY_BUCKETS.tool] },
    }),
    toolRetries: meter.createCounter('gen_ai_tool_retries_total', {
      description: 'Retries observed inside a tool call',
    }),
    llmCalls: meter.createCounter('gen_ai_llm_calls_total', {
      description: 'Model calls, labelled by model and status',
    }),
    llmLatency: meter.createHistogram('gen_ai_llm_latency_seconds', {
      description: 'Model call latency',
      unit: 's',
      advice: { explicitBucketBoundaries: [...LATENCY_BUCKETS.llm] },
    }),
    llmTokens: meter.createCounter('gen_ai_llm_tokens_total', {
      description: 'Tokens consumed, split by type=input|output',
    }),
    llmCost: meter.createCounter('gen_ai_llm_cost_usd_total', {
      description: 'Estimated spend in USD',
    }),
    vectorOps: meter.createCounter('vector_db_operations_total', {
      description: 'Vector store operations',
    }),
    vectorLatency: meter.createHistogram('vector_db_query_duration_seconds', {
      description: 'Vector store query latency',
      unit: 's',
      advice: { explicitBucketBoundaries: [...LATENCY_BUCKETS.vector] },
    }),
    agentUp: meter.createUpDownCounter('gen_ai_agent_up', {
      description: '1 while the agent process is alive',
    }),
    citationCoverage: meter.createHistogram('gen_ai_response_citation_coverage', {
      description: 'Fraction of claims backed by a retrieved source (0-1)',
      unit: '1',
      advice: { explicitBucketBoundaries: [0, 0.1, 0.25, 0.5, 0.75, 0.9, 0.95, 1] },
    }),
    refusals: meter.createCounter('gen_ai_responses_refusal_total', {
      description: 'Answers withheld by a guardrail or the model itself',
    }),
    responses: meter.createCounter('gen_ai_responses_total', {
      description: 'Answers produced, labelled has_citations=true|false',
    }),
  };
  return _metrics;
}

// ---------------------------------------------------------------------------
// Span option shapes
// ---------------------------------------------------------------------------

export interface AgentSpanOptions {
  serviceName: string;
  agentName: string;
  operation: string;
  workflowId?: string;
  stepName?: string;
  stepIndex?: number;
  attributes?: Attributes;
}

export interface ToolSpanOptions {
  serviceName: string;
  toolName: string;
  toolDescription?: string;
  parameters?: Record<string, unknown>;
  callId?: string;
  workflowId?: string;
  costUsd?: number;
}

export interface LlmSpanOptions {
  serviceName: string;
  model: string;
  temperature?: number;
  maxTokens?: number;
  workflowId?: string;
}

export interface LlmUsage {
  inputTokens: number;
  outputTokens: number;
  totalTokens?: number;
  costUsd?: number;
  finishReason?: string;
  responseId?: string;
  responseModel?: string;
}

export interface VectorDbSpanOptions {
  serviceName: string;
  operation: 'search' | 'insert' | 'delete' | 'upsert';
  collection: string;
  topK?: number;
  scoreThreshold?: number;
}

// ---------------------------------------------------------------------------
// Span factories
// ---------------------------------------------------------------------------

const tracer = () => trace.getTracer('ai-agent-instrumentation', '1.0.0');

export function startAgentSpan(o: AgentSpanOptions): Span {
  return tracer().startSpan(`agent.${o.operation}`, {
    kind: SpanKind.INTERNAL,
    attributes: {
      [SEMRESATTRS_SERVICE_NAME]: o.serviceName,
      [ATTR.AGENT_NAME]: o.agentName,
      [ATTR.AGENT_OPERATION]: o.operation,
      ...(o.workflowId ? { [ATTR.AGENT_WORKFLOW_ID]: o.workflowId } : {}),
      ...(o.stepName ? { [ATTR.AGENT_STEP_NAME]: o.stepName } : {}),
      ...(o.stepIndex !== undefined ? { [ATTR.AGENT_STEP_INDEX]: o.stepIndex } : {}),
      ...o.attributes,
    },
  });
}

export function startToolSpan(o: ToolSpanOptions): Span {
  return tracer().startSpan(`tool.${o.toolName}`, {
    kind: SpanKind.CLIENT,
    attributes: {
      [SEMRESATTRS_SERVICE_NAME]: o.serviceName,
      [ATTR.TOOL_NAME]: o.toolName,
      [ATTR.TOOL_STATUS]: 'success' as ToolStatus,
      ...(o.toolDescription ? { [ATTR.TOOL_DESCRIPTION]: o.toolDescription } : {}),
      ...(o.parameters ? { [ATTR.TOOL_PARAMETERS]: JSON.stringify(o.parameters) } : {}),
      ...(o.callId ? { [ATTR.TOOL_CALL_ID]: o.callId } : {}),
      ...(o.workflowId ? { [ATTR.AGENT_WORKFLOW_ID]: o.workflowId } : {}),
    },
  });
}

export function startLlmSpan(o: LlmSpanOptions): Span {
  return tracer().startSpan(`llm.${o.model}`, {
    kind: SpanKind.CLIENT,
    attributes: {
      [SEMRESATTRS_SERVICE_NAME]: o.serviceName,
      [ATTR.LLM_MODEL]: o.model,
      ...(o.temperature !== undefined ? { [ATTR.LLM_TEMPERATURE]: o.temperature } : {}),
      ...(o.maxTokens !== undefined ? { [ATTR.LLM_MAX_TOKENS]: o.maxTokens } : {}),
      ...(o.workflowId ? { [ATTR.AGENT_WORKFLOW_ID]: o.workflowId } : {}),
    },
  });
}

export function startVectorDbSpan(o: VectorDbSpanOptions): Span {
  return tracer().startSpan(`vector.${o.operation}`, {
    kind: SpanKind.CLIENT,
    attributes: {
      [SEMRESATTRS_SERVICE_NAME]: o.serviceName,
      [ATTR.VECTOR_OPERATION]: o.operation,
      [ATTR.VECTOR_COLLECTION]: o.collection,
      ...(o.topK ? { [ATTR.VECTOR_TOP_K]: o.topK } : {}),
      ...(o.scoreThreshold ? { [ATTR.VECTOR_SCORE_THRESHOLD]: o.scoreThreshold } : {}),
    },
  });
}

// ---------------------------------------------------------------------------
// Wrappers — the ones you actually import
// ---------------------------------------------------------------------------

function fail(span: Span, error: unknown): void {
  span.setStatus({
    code: SpanStatusCode.ERROR,
    message: error instanceof Error ? error.message : String(error),
  });
  if (error instanceof Error) span.recordException(error);
}

export async function traceAgent<T>(
  o: AgentSpanOptions,
  fn: (span: Span) => Promise<T>,
): Promise<T> {
  const m = getAiMetrics();
  const span = startAgentSpan(o);
  const started = Date.now();
  const labels = { 'service.name': o.serviceName, operation: o.operation };

  return context.with(trace.setSpan(context.active(), span), async () => {
    try {
      const out = await fn(span);
      span.setStatus({ code: SpanStatusCode.OK });
      m.agentCalls.add(1, { ...labels, status: 'success' });
      return out;
    } catch (err) {
      fail(span, err);
      m.agentCalls.add(1, { ...labels, status: 'error' });
      throw err;
    } finally {
      m.agentLatency.record((Date.now() - started) / 1000, labels);
      span.end();
    }
  });
}

export async function traceTool<T>(
  o: ToolSpanOptions,
  fn: (span: Span) => Promise<T>,
): Promise<T> {
  const m = getAiMetrics();
  const span = startToolSpan(o);
  const started = Date.now();
  const labels = { 'service.name': o.serviceName, 'tool.name': o.toolName };

  return context.with(trace.setSpan(context.active(), span), async () => {
    try {
      const out = await fn(span);
      span.setAttribute(ATTR.TOOL_STATUS, 'success');
      span.setStatus({ code: SpanStatusCode.OK });
      m.toolCalls.add(1, { ...labels, status: 'success' });
      return out;
    } catch (err) {
      span.setAttribute(ATTR.TOOL_STATUS, 'error');
      fail(span, err);
      m.toolCalls.add(1, { ...labels, status: 'error' });
      throw err;
    } finally {
      const seconds = (Date.now() - started) / 1000;
      span.setAttribute(ATTR.TOOL_DURATION_MS, seconds * 1000);
      m.toolLatency.record(seconds, labels);
      if (o.costUsd) m.llmCost.add(o.costUsd, labels);
      span.end();
    }
  });
}

/**
 * Wrap a model call. The callback returns both the payload and the usage
 * block, so you never have to reach back into a vendor SDK to find it.
 */
export async function traceLlm<T>(
  o: LlmSpanOptions,
  fn: (span: Span) => Promise<{ response: T; usage?: LlmUsage }>,
): Promise<T> {
  const m = getAiMetrics();
  const span = startLlmSpan(o);
  const started = Date.now();
  const labels = { 'service.name': o.serviceName, model: o.model };

  return context.with(trace.setSpan(context.active(), span), async () => {
    try {
      const { response, usage } = await fn(span);

      if (usage) {
        const total = usage.totalTokens ?? usage.inputTokens + usage.outputTokens;
        span.setAttribute(ATTR.LLM_INPUT_TOKENS, usage.inputTokens);
        span.setAttribute(ATTR.LLM_OUTPUT_TOKENS, usage.outputTokens);
        span.setAttribute(ATTR.LLM_TOTAL_TOKENS, total);
        m.llmTokens.add(usage.inputTokens, { ...labels, type: 'input' });
        m.llmTokens.add(usage.outputTokens, { ...labels, type: 'output' });

        const cost = usage.costUsd ?? priceCall(o.model, usage.inputTokens, usage.outputTokens);
        if (cost > 0) {
          span.setAttribute(ATTR.LLM_COST_USD, cost);
          m.llmCost.add(cost, labels);
        }
        if (usage.finishReason) span.setAttribute(ATTR.LLM_FINISH_REASON, usage.finishReason);
        if (usage.responseId) span.setAttribute(ATTR.LLM_RESPONSE_ID, usage.responseId);
        if (usage.responseModel) span.setAttribute(ATTR.LLM_RESPONSE_MODEL, usage.responseModel);
      }

      span.setStatus({ code: SpanStatusCode.OK });
      m.llmCalls.add(1, { ...labels, status: 'success' });
      return response;
    } catch (err) {
      fail(span, err);
      m.llmCalls.add(1, { ...labels, status: 'error' });
      throw err;
    } finally {
      m.llmLatency.record((Date.now() - started) / 1000, labels);
      span.end();
    }
  });
}

export async function traceVectorDb<T>(
  o: VectorDbSpanOptions,
  fn: (span: Span) => Promise<{ results: T[] }>,
): Promise<T[]> {
  const m = getAiMetrics();
  const span = startVectorDbSpan(o);
  const started = Date.now();
  const labels = { 'service.name': o.serviceName, operation: o.operation };

  return context.with(trace.setSpan(context.active(), span), async () => {
    try {
      const { results } = await fn(span);
      span.setAttribute(ATTR.VECTOR_RESULTS_COUNT, results.length);
      span.setStatus({ code: SpanStatusCode.OK });
      m.vectorOps.add(1, { ...labels, status: 'success' });
      return results;
    } catch (err) {
      fail(span, err);
      m.vectorOps.add(1, { ...labels, status: 'error' });
      throw err;
    } finally {
      m.vectorLatency.record((Date.now() - started) / 1000, labels);
      span.end();
    }
  });
}

// ---------------------------------------------------------------------------
// Answer quality — what makes this an *AI* dashboard and not a web one
// ---------------------------------------------------------------------------

export interface ResponseQuality {
  hasCitations: boolean;
  citationCoverage: number;
  citations?: string[];
  sources?: string[];
  isRefusal?: boolean;
  refusalReason?: string;
}

export function recordResponseQuality(
  span: Span,
  serviceName: string,
  q: ResponseQuality,
): void {
  const m = getAiMetrics();
  span.setAttribute(ATTR.HAS_CITATIONS, q.hasCitations);
  span.setAttribute(ATTR.CITATION_COVERAGE, q.citationCoverage);
  span.setAttribute(ATTR.IS_REFUSAL, q.isRefusal ?? false);
  if (q.citations?.length) span.setAttribute(ATTR.CITATIONS, JSON.stringify(q.citations));
  if (q.sources?.length) span.setAttribute(ATTR.SOURCES, JSON.stringify(q.sources));
  if (q.refusalReason) span.setAttribute(ATTR.REFUSAL_REASON, q.refusalReason);

  const labels = { 'service.name': serviceName };
  m.citationCoverage.record(q.citationCoverage, labels);
  m.responses.add(1, { ...labels, has_citations: String(q.hasCitations) });
  if (q.isRefusal) m.refusals.add(1, labels);
}

/**
 * Cheap citation heuristic for RAG answers: a sentence counts as sourced if it
 * ends with a bracketed marker or a link. Crude, but it catches the failure
 * mode that matters — a confident paragraph with nothing behind it.
 */
export function estimateCitationCoverage(
  answer: string,
  sourceCount: number,
): { citationCoverage: number; citations: string[] } {
  const sentences = answer
    .split(/(?<=[.!?])\s+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);

  if (sentences.length === 0) return { citationCoverage: 0, citations: [] };

  const markers = answer.match(/\[[^\]]+\]|https?:\/\/\S+/g) ?? [];
  const cited = sentences.filter((s) => /\[[^\]]+\]|https?:\/\/\S+/.test(s));

  return {
    citationCoverage: sourceCount === 0 ? 0 : cited.length / sentences.length,
    citations: markers,
  };
}

// ---------------------------------------------------------------------------
// Context propagation (HTTP hops, queue payloads, n8n webhooks)
// ---------------------------------------------------------------------------

export function injectTraceContext(carrier: Record<string, string> = {}): Record<string, string> {
  propagation.inject(context.active(), carrier);
  return carrier;
}

export function extractTraceContext(carrier: Record<string, string>) {
  return propagation.extract(context.active(), carrier);
}

// ---------------------------------------------------------------------------
// Lifecycle
// ---------------------------------------------------------------------------

export class AgentLifecycle {
  private timer: ReturnType<typeof setInterval> | null = null;

  constructor(
    private readonly serviceName: string,
    private readonly intervalMs = 30_000,
  ) {}

  start(): this {
    const m = getAiMetrics();
    m.agentUp.add(1, { 'service.name': this.serviceName });
    this.timer = setInterval(() => {
      m.agentUp.add(0, { 'service.name': this.serviceName });
    }, this.intervalMs);
    return this;
  }

  stop(): void {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    getAiMetrics().agentUp.add(-1, { 'service.name': this.serviceName });
  }
}

// ---------------------------------------------------------------------------
// SDK bootstrap
// ---------------------------------------------------------------------------

export interface InitOptions {
  serviceName: string;
  endpoint?: string;
  environment?: string;
  version?: string;
  /** Extra exporters, e.g. console in dev. */
  onStart?: () => void;
}

export function initTelemetry(o: InitOptions): NodeSDK {
  const endpoint = o.endpoint ?? process.env.OTEL_EXPORTER_OTLP_ENDPOINT ?? 'http://otel-collector:4317';

  const sdk = new NodeSDK({
    resource: new Resource({
      [SEMRESATTRS_SERVICE_NAME]: o.serviceName,
      [SEMRESATTRS_SERVICE_VERSION]: o.version ?? '1.0.0',
      [SEMRESATTRS_DEPLOYMENT_ENVIRONMENT]: o.environment ?? process.env.NODE_ENV ?? 'development',
    }),
    traceExporter: new OTLPTraceExporter({ url: endpoint }),
    metricReader: new PeriodicExportingMetricReader({
      exporter: new OTLPMetricExporter({ url: endpoint }),
      exportIntervalMillis: 15_000,
    }),
    instrumentations: [
      getNodeAutoInstrumentations({
        // These two generate more noise than signal in agent workloads.
        '@opentelemetry/instrumentation-fs': { enabled: false },
        '@opentelemetry/instrumentation-dns': { enabled: false },
      }),
    ],
  });

  sdk.start();
  o.onStart?.();

  const shutdown = () => {
    sdk.shutdown().catch((err) => console.error('[otel] shutdown failed', err));
  };
  process.once('SIGTERM', shutdown);
  process.once('SIGINT', shutdown);

  return sdk;
}
