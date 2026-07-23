/**
 * Shared OTel bootstrap for the n8n node package.
 *
 * n8n loads nodes into one long-lived process, so the tracer is created once
 * and cached by service name. Config resolution order:
 *   1. explicit endpoint from the node's credentials
 *   2. OTEL_EXPORTER_OTLP_ENDPOINT env var
 *   3. local collector
 */

import { context, trace, Span, SpanStatusCode } from '@opentelemetry/api';
import { OTLPTraceExporter } from '@opentelemetry/exporter-trace-otlp-http';
import { Resource } from '@opentelemetry/resources';
import { BatchSpanProcessor, TracerProvider } from '@opentelemetry/sdk-trace-node';

const PROVIDERS = new Map<string, TracerProvider>();

export function ensureTracer(serviceName: string, endpoint?: string) {
  if (!PROVIDERS.has(serviceName)) {
    const url =
      endpoint ??
      process.env.OTEL_EXPORTER_OTLP_ENDPOINT ??
      'http://localhost:4318';

    const provider = new TracerProvider({
      resource: new Resource({
        'service.name': serviceName,
        'service.namespace': 'n8n',
        'n8n.version': process.env.N8N_VERSION ?? 'unknown',
      }),
    });

    provider.addSpanProcessor(
      new BatchSpanProcessor(
        new OTLPTraceExporter({ url: `${url.replace(/\/$/, '')}/v1/traces` }),
        { scheduledDelayMillis: 2000, maxExportBatchSize: 256 },
      ),
    );

    PROVIDERS.set(serviceName, provider);
  }

  return PROVIDERS.get(serviceName)!.getTracer('n8n-instrumentation', '1.0.0');
}

export function endSpan(span: Span, error?: Error): void {
  if (error) {
    span.setStatus({ code: SpanStatusCode.ERROR, message: error.message });
    span.recordException(error);
  } else {
    span.setStatus({ code: SpanStatusCode.OK });
  }
  span.end();
}

export function traceIdOf(span: Span): string {
  return span.spanContext().traceId;
}

/** Kept for nodes that nest inside a manually-activated context. */
export function activeTraceId(): string | undefined {
  return trace.getSpan(context.active())?.spanContext().traceId;
}

export function spanFromItem(item: { json?: Record<string, any> }): string | undefined {
  return item.json?._otel?.spanId;
}
