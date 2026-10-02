// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The console's HTTP client. One place that knows about auth and envelopes.
 *
 * Every path is relative to `/api`, never an absolute URL, because CloudFront
 * serves this app and the API from the same origin -- the S3 bucket on `/` and API
 * Gateway on `/api/*`. That is why there is no CORS configuration anywhere in this
 * project and no preflight on any request. It also means the API's hostname never
 * appears in the bundle, so a redeploy that changes it needs no frontend rebuild.
 */

import { idToken } from './auth';

/** The foundation's envelope, which our API deliberately reuses so the console
 *  parses one shape whether the data came from us or was passed through from the
 *  hotel platform. */
interface Envelope<T> {
  success: boolean;
  data?: T;
  error?: { code: string; message: string; details?: Record<string, unknown> };
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }

  /** A run that has not written its first row yet. Not an error to show anyone.
   *  A predicate rather than a magic string repeated at three call sites. */
  get isPending(): boolean {
    return this.status === 404 && this.code === 'RUN_NOT_FOUND';
  }
}

/** Called when the API rejects our token. Set once by AuthProvider.
 *
 *  The ID token lives an hour. A console left open across a shift eventually 401s on
 *  everything, and the first version surfaced that as an error banner on every pane at
 *  once -- which reads as an outage rather than as "sign in again". */
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(handler: () => void): void {
  onUnauthorized = handler;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const token = await idToken();
  if (!token) {
    throw new ApiError(401, 'NO_SESSION', 'Your session has expired. Sign in again.');
  }

  const response = await fetch(`/api${path}`, {
    method,
    headers: {
      Authorization: token,
      ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });

  let payload: Envelope<T>;
  try {
    payload = (await response.json()) as Envelope<T>;
  } catch {
    // API Gateway's own errors -- a rejected authorizer, a throttle -- are not our
    // envelope. Surfacing the status is more useful than a JSON parse error.
    throw new ApiError(response.status, 'UNEXPECTED_RESPONSE', `HTTP ${response.status}`);
  }

  if (!response.ok || !payload.success) {
    if (response.status === 401) onUnauthorized?.();
    throw new ApiError(
      response.status,
      payload.error?.code ?? 'UNKNOWN',
      payload.error?.message ?? `HTTP ${response.status}`,
    );
  }
  return payload.data as T;
}

// --------------------------------------------------------------------------- //
// Shapes, matching what the three Lambdas return
// --------------------------------------------------------------------------- //

export interface RunSummary {
  runId: string;
  /** `queued` here means enqueued and never executed -- a message that died in the
   *  DLQ. Surfaced deliberately: before, such a run left no trace at all. */
  status?: 'queued' | 'complete';
  ts: string;
  propertyId: string | null;
  operatingDate: string | null;
  agent: string;
  trigger: string;
  outcome: string;
  delegations: string[];
  durationSeconds?: number;
  prompt?: string;
  excerpt: string;
  truncated: boolean;
  humanOverride?: HumanOverride | null;
  errorCode?: string;
}

export interface HumanOverride {
  verdict: 'overridden' | 'endorsed';
  reason: string;
  by: string;
  at: string;
}

export interface RunStep {
  ts: string;
  agent: string;
  tool: string;
  outcome: string;
  errorCode?: string;
  /** Present only on a *successful* write, so this never implies money moved that
   *  did not. */
  actionTaken?: string;
  inputsHash: string;
}

export interface RunDetail {
  runId: string;
  /** Three states, not two. `queued` exists because the chat route now records a run
   *  the moment it is enqueued -- before that, this endpoint 404'd for the first
   *  seconds of every run and the console treated the happy path as an error. */
  status: 'queued' | 'running' | 'complete';
  prompt?: string | null;
  askedBy?: string | null;
  summary: RunSummary | null;
  answer: string | null;
  answerTruncated: boolean;
  steps: RunStep[];
  stepCount: number;
  humanOverride?: HumanOverride | null;
}

export interface Approval {
  id: string;
  action: 'post_charge' | 'void_folio' | 'adjust_loyalty';
  status: 'PENDING' | 'APPROVED' | 'REJECTED';
  propertyId: string;
  folioId?: string;
  guestId?: string;
  amount?: number;
  /** Every argument the execution will send, keyed by the tool's own argument
   *  name. Both gates bind all of them, so this is what the approver is agreeing to
   *  -- not the headline amount alone. */
  arguments?: Record<string, string | number>;
  reason: string;
  runId?: string;
  proposedBy: string;
  createdAt: string;
  decidedBy?: string;
  decidedAt?: string;
  decisionNote?: string;
  executionRunId?: string;
}

export interface ChatStarted {
  runId: string;
  propertyId: string | null;
  poll: { url: string; intervalSeconds: number; note: string };
}

/** One row of the platform's own scoped property list. `region` is null on every
 *  property in this dataset, so nothing groups by it. */
export interface Property {
  propertyId: string;
  name: string;
  city: string | null;
  state: string | null;
  region: string | null;
}

export interface PropertyScope {
  /** The caller carries `custom:property_id`, so the platform returned exactly
   *  their property and there is nothing to choose between. */
  boundToOneProperty: boolean;
  region: string | null;
}

export const api = {
  startChat: (body: { prompt: string; propertyId?: string; operatingDate?: string }) =>
    request<ChatStarted>('POST', '/chat', body),

  /** Scoped by the platform from the caller's own token — see the properties
   *  Lambda. The console never filters this list itself. */
  listProperties: () =>
    request<{ properties: Property[]; scope: PropertyScope }>('GET', '/properties'),

  listRuns: (propertyId?: string | null, limit = 50) =>
    request<{ runs: RunSummary[]; count: number }>(
      'GET',
      `/runs?limit=${limit}${propertyId ? `&propertyId=${encodeURIComponent(propertyId)}` : ''}`,
    ),

  getRun: (runId: string) => request<RunDetail>('GET', `/runs/${encodeURIComponent(runId)}`),

  override: (runId: string, verdict: 'overridden' | 'endorsed', reason: string) =>
    request<{ humanOverride: HumanOverride }>(
      'POST',
      `/runs/${encodeURIComponent(runId)}/override`,
      { verdict, reason },
    ),

  listApprovals: (status = 'PENDING') =>
    request<{ approvals: Approval[]; count: number; youMayApprove: boolean }>(
      'GET',
      `/approvals?status=${status}`,
    ),

  propose: (body: Record<string, unknown>) =>
    request<{ approval: Approval }>('POST', '/approvals', body),

  approve: (id: string, note: string) =>
    request<{ status: string; executionRunId: string; note: string }>(
      'POST',
      `/approvals/${encodeURIComponent(id)}/approve`,
      { note },
    ),

  reject: (id: string, note: string) =>
    request<{ status: string; note: string }>(
      'POST',
      `/approvals/${encodeURIComponent(id)}/reject`,
      { note },
    ),
};
