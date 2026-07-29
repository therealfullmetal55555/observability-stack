import {
  IExecuteFunctions,
  INodeExecutionData,
  INodeType,
  INodeTypeDescription,
  NodeOperationError,
} from 'n8n-workflow';

import { ensureTracer, spanFromItem, endSpan, traceIdOf } from '../../shared/otel';

/**
 * OTel Trace — wraps the downstream branch of a workflow in a span.
 *
 * n8n doesn't have a "scope" concept, so the trick is to keep the span on the
 * execution object and end it from a second node. Pair `OTel Trace` (start)
 * with `OTel Trace End` when the traced region is more than one node wide;
 * for a single node this middle-pass-through version is enough.
 */
export class OtelTrace implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'OTel Trace',
    name: 'otelTrace',
    icon: 'file:otel.svg',
    group: ['transform'],
    version: 1,
    subtitle: '={{$parameter["operationName"]}}',
    description: 'Emit an OpenTelemetry span for the items flowing through this node',
    defaults: { name: 'OTel Trace' },
    inputs: ['main'],
    outputs: ['main'],
    credentials: [
      {
        name: 'otelCollectorApi',
        required: false,
        displayOptions: { show: { useInlineConfig: [true] } },
      },
    ],
    properties: [
      {
        displayName: 'Service Name',
        name: 'serviceName',
        type: 'string',
        default: 'n8n-workflow',
        description: 'Shows up as service.name in Grafana. Use the workflow name.',
      },
      {
        displayName: 'Operation Name',
        name: 'operationName',
        type: 'string',
        default: '',
        placeholder: 'defaults to the node name',
        description: 'Becomes agent.<operation>. Keep it stable — alerts key off it.',
      },
      {
        displayName: 'Span Kind',
        name: 'spanKind',
        type: 'options',
        default: 'internal',
        options: [
          { name: 'Internal', value: 'internal' },
          { name: 'Client (outbound call)', value: 'client' },
          { name: 'Producer (queue)', value: 'producer' },
        ],
      },
      {
        displayName: 'Attributes',
        name: 'attributes',
        placeholder: 'Add attribute',
        type: 'fixedCollection',
        typeOptions: { multipleValues: true },
        default: {},
        options: [
          {
            name: 'attribute',
            displayName: 'Attribute',
            values: [
              { displayName: 'Key', name: 'key', type: 'string', default: '' },
              { displayName: 'Value', name: 'value', type: 'string', default: '' },
            ],
          },
        ],
      },
      {
        displayName: 'Record Cost',
        name: 'recordCost',
        type: 'boolean',
        default: false,
        description: 'Whether to read a cost value off the incoming item',
      },
      {
        displayName: 'Cost Field',
        name: 'costField',
        type: 'string',
        default: 'cost_usd',
        displayOptions: { show: { recordCost: [true] } },
        description: 'Field on the item holding USD spent by this step',
      },
      {
        displayName: 'Wait for Downstream',
        name: 'waitForDownstream',
        type: 'boolean',
        default: false,
        description:
          'Whether to keep the span open past this node. Requires an OTel Trace End node further down the branch.',
      },
      {
        displayName: 'Use Inline Collector Config',
        name: 'useInlineConfig',
        type: 'boolean',
        default: false,
        description: 'Whether to override the collector endpoint for this node only',
      },
    ],
  };

  async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
    const items = this.getInputData();
    const serviceName = this.getNodeParameter('serviceName', 0) as string;
    const nodeName = this.getNode().name;
    const operationName =
      (this.getNodeParameter('operationName', 0) as string) || nodeName;
    const spanKind = this.getNodeParameter('spanKind', 0) as string;
    const waitForDownstream = this.getNodeParameter('waitForDownstream', 0) as boolean;
    const recordCost = this.getNodeParameter('recordCost', 0) as boolean;
    const costField = this.getNodeParameter('costField', 0, 'cost_usd') as string;

    const rawAttributes = (this.getNodeParameter('attributes', 0, {}) as {
      attribute?: Array<{ key: string; value: string }>;
    }).attribute ?? [];

    let baseUrl: string | undefined;
    if (this.getNodeParameter('useInlineConfig', 0, false) as boolean) {
      const creds = await this.getCredentials('otelCollectorApi');
      baseUrl = creds.endpoint as string;
    }

    const tracer = ensureTracer(serviceName, baseUrl);
    const workflowId = this.getWorkflow().id ?? 'unknown';
    const executionId = this.getExecutionId();

    const attributes: Record<string, string | number | boolean> = {
      'gen_ai.workflow.id': workflowId,
      'n8n.workflow.id': workflowId,
      'n8n.execution.id': executionId,
      'n8n.node.name': nodeName,
      'n8n.node.type': this.getNode().type,
      'gen_ai.agent.name': serviceName,
      'gen_ai.operation.name': operationName,
    };
    for (const { key, value } of rawAttributes) {
      if (key) attributes[key] = value;
    }

    // One span per item keeps LLM fan-outs readable in Tempo instead of
    // collapsing ten parallel calls into one opaque bar.
    for (const [index, item] of items.entries()) {
      const span = tracer.startSpan(`tool.${operationName}`, {
        kind: mapKind(spanKind),
        attributes,
      });

      if (recordCost) {
        const cost = Number(item.json?.[costField] ?? 0);
        if (Number.isFinite(cost) && cost > 0) {
          span.setAttribute('gen_ai.tool.cost_usd', cost);
        }
      }

      // Hand the trace id to downstream nodes so the whole run can be filtered
      // by it in Loki, even from nodes that don't emit spans themselves.
      item.json._otel = { traceId: traceIdOf(span), spanId: spanFromItem(item) };

      if (waitForDownstream) {
        const spans = (this.getWorkflowStaticData('global').__otelSpans ??= {});
        spans[`${executionId}:${nodeName}:${index}`] = span;
      } else {
        endSpan(span, undefined);
      }
    }

    return [items];
  }
}

function mapKind(kind: string) {
  // Imported lazily so the node still loads on older n8n builds that ship an
  // older @opentelemetry/api without SpanKind re-exported.
  const { SpanKind } = require('@opentelemetry/api');
  switch (kind) {
    case 'client':
      return SpanKind.CLIENT;
    case 'producer':
      return SpanKind.PRODUCER;
    default:
      return SpanKind.INTERNAL;
  }
}
