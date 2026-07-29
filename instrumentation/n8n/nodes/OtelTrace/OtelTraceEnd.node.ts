import {
  IExecuteFunctions,
  INodeExecutionData,
  INodeType,
  INodeTypeDescription,
  NodeOperationError,
} from 'n8n-workflow';

import { endSpan } from '../../shared/otel';

/**
 * OTel Trace End — closes the span opened by a matching OTel Trace node.
 *
 * The span lives in n8n's global static data under
 * `${executionId}:${startNodeName}:${itemIndex}`. If it's missing you get a
 * loud error rather than a silently dangling span, because a trace that never
 * ends looks like a hang in Tempo and wastes an afternoon.
 */
export class OtelTraceEnd implements INodeType {
  description: INodeTypeDescription = {
    displayName: 'OTel Trace End',
    name: 'otelTraceEnd',
    icon: 'file:otel.svg',
    group: ['transform'],
    version: 1,
    description: 'Close the span opened by an OTel Trace node',
    defaults: { name: 'OTel Trace End' },
    inputs: ['main'],
    outputs: ['main'],
    properties: [
      {
        displayName: 'Start Node Name',
        name: 'startNode',
        type: 'string',
        default: '',
        required: true,
        description: 'Name of the OTel Trace node that opened the span',
      },
      {
        displayName: 'Mark as Error',
        name: 'markAsError',
        type: 'boolean',
        default: false,
        description: 'Whether to close the span with an error status regardless of the outcome',
      },
      {
        displayName: 'Error Message',
        name: 'errorMessage',
        type: 'string',
        default: '',
        displayOptions: { show: { markAsError: [true] } },
      },
      {
        displayName: 'Error Field',
        name: 'errorField',
        type: 'string',
        default: 'error',
        description:
          'Item field that, when present, marks the span as failed. Leave as `error` to follow n8n convention.',
      },
    ],
  };

  async execute(this: IExecuteFunctions): Promise<INodeExecutionData[][]> {
    const items = this.getInputData();
    const startNode = this.getNodeParameter('startNode', 0) as string;
    const markAsError = this.getNodeParameter('markAsError', 0) as boolean;
    const errorMessage = this.getNodeParameter('errorMessage', 0, '') as string;
    const errorField = this.getNodeParameter('errorField', 0, 'error') as string;

    const executionId = this.getExecutionId();
    const spans: Record<string, any> =
      this.getWorkflowStaticData('global').__otelSpans ?? {};

    for (const [index, item] of items.entries()) {
      const key = `${executionId}:${startNode}:${index}`;
      const span = spans[key];

      if (!span) {
        throw new NodeOperationError(
          this.getNode(),
          `No open span for "${startNode}" (execution ${executionId}, item ${index}). ` +
            'Check that the start node has "Wait for Downstream" enabled and that both nodes run on the same branch.',
        );
      }

      let error: Error | undefined;
      if (markAsError) {
        error = new NodeOperationError(this.getNode(), errorMessage || 'marked as error');
      } else if (item.json?.[errorField]) {
        error = new Error(String(item.json[errorField]));
      }

      if (error) {
        span.setAttribute('gen_ai.tool.status', 'error');
      }
      endSpan(span, error);
      delete spans[key];
    }

    return [items];
  }
}
