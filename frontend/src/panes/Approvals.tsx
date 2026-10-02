// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The approval queue — the one screen here with real authority behind it.
 *
 * Everything else in this console reports what agents did. This is where a person
 * decides whether money moves, and the guardrail it drives runs outside the model: a
 * Gateway interceptor refuses the three Tier-2 tools before the tool Lambda is even
 * invoked, and the billing Lambda refuses again independently.
 *
 * Three things the UI is deliberate about:
 *
 *   The approval token is never shown, because the API never returns it. It goes
 *   straight to the agent that spends it. There is nothing to copy and nothing to
 *   paste into a chat, which is the point.
 *
 *   A note is required to approve, and the field says why. The agent's reason records
 *   what it wanted; the note records that a named human agreed, and that is the only
 *   part of the chain carrying authority.
 *
 *   The full scope of the release is shown — action, target, and every argument the
 *   execution will send — because the gates bind the token to all of them. An approver
 *   should see the same facts the machine will check, in the same place. This card
 *   once showed only the amount, while a loyalty approval's points were bound by
 *   neither gate and shown to nobody; a security review found both.
 */

import { AlertTriangle, Check, Loader2, ShieldCheck, X } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { api, type Approval } from '../api';

const ACTION_LABEL: Record<Approval['action'], string> = {
  post_charge: 'Post a charge',
  void_folio: 'Void a folio',
  adjust_loyalty: 'Adjust loyalty points',
};

const STATUS_STYLE: Record<Approval['status'], string> = {
  PENDING: 'border-ember-deep bg-ember-wash text-ember',
  APPROVED: 'border-sage-deep bg-sage-wash text-sage',
  REJECTED: 'border-line-bright bg-ink-3 text-bone-dim',
};

const FILTERS = ['PENDING', 'APPROVED', 'REJECTED', 'ALL'] as const;

export function Approvals({ onOpenRun }: { onOpenRun: (runId: string) => void }) {
  const [approvals, setApprovals] = useState<Approval[]>([]);
  const [mayApprove, setMayApprove] = useState(false);
  const [status, setStatus] = useState<(typeof FILTERS)[number]>('PENDING');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [outcome, setOutcome] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const data = await api.listApprovals(status);
      setApprovals(data.approvals);
      setMayApprove(data.youMayApprove);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load the queue.');
    } finally {
      setLoading(false);
    }
  }, [status]);

  useEffect(() => {
    void load();
    // Proposals arrive from agent runs, not from this browser, so the queue refreshes
    // itself or an operator sits looking at a stale empty screen.
    const timer = setInterval(() => void load(), 15000);
    return () => clearInterval(timer);
  }, [load]);

  const decide = async (approval: Approval, approve: boolean) => {
    const note = (notes[approval.id] ?? '').trim();
    if (approve && !note) {
      setError('Approving requires a note. Say why you are releasing this.');
      return;
    }
    setBusy(approval.id);
    setError(null);
    try {
      if (approve) {
        const result = await api.approve(approval.id, note);
        setOutcome(result.note);
        if (result.executionRunId) onOpenRun(result.executionRunId);
      } else {
        setOutcome((await api.reject(approval.id, note || 'Rejected.')).note);
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'The decision failed.');
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-3xl px-6 py-6">
        <header className="animate-rise mb-5 flex flex-wrap items-end justify-between gap-4">
          <div>
            <h2 className="flex items-center gap-2 font-display text-[20px] font-normal tracking-tight text-bone">
              <ShieldCheck className="h-4 w-4 text-ember" />
              Approval queue
            </h2>
            <p className="mt-1.5 max-w-xl text-[13px] leading-relaxed text-bone-dim">
              Agents may reorganise work freely and may never move money on their own.
              {!mayApprove && (
                <span className="text-clay">
                  {' '}
                  Your account can read this queue but not release anything.
                </span>
              )}
            </p>
          </div>

          <div className="flex border border-line">
            {FILTERS.map((f) => (
              <button
                key={f}
                onClick={() => setStatus(f)}
                className={`px-2.5 py-1 font-mono text-2xs uppercase tracking-[0.1em] transition-colors ${
                  status === f
                    ? 'bg-ink-4 text-bone'
                    : 'text-bone-faint hover:text-bone-dim'
                }`}
              >
                {f.toLowerCase()}
              </button>
            ))}
          </div>
        </header>

        {error && (
          <div className="mb-4 flex items-start gap-2 border border-clay-deep bg-clay-wash px-3 py-2.5 text-[13px] text-clay">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}
        {outcome && (
          <div className="mb-4 border border-sage-deep bg-sage-wash px-3 py-2.5 text-[13px] text-sage">
            {outcome}
          </div>
        )}

        {loading ? (
          <p className="flex items-center gap-2 font-mono text-2xs uppercase tracking-[0.12em] text-bone-faint">
            <Loader2 className="h-3 w-3 animate-spin" /> loading
          </p>
        ) : approvals.length === 0 ? (
          <p className="panel px-4 py-10 text-center text-[13px] text-bone-dim">
            Nothing {status.toLowerCase()}.
            {status === 'PENDING' && ' No agent is waiting on a human right now.'}
          </p>
        ) : (
          <ul className="space-y-3">
            {approvals.map((approval, index) => (
              <li
                key={approval.id}
                className="panel animate-rise px-4 py-3.5"
                style={{ animationDelay: `${Math.min(index, 8) * 35}ms` }}
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <h3 className="text-[14px] text-bone">
                      {ACTION_LABEL[approval.action] ?? approval.action}
                      {approval.amount !== undefined && (
                        <span className="ml-2 font-mono text-ember">
                          ${Number(approval.amount).toFixed(2)}
                        </span>
                      )}
                    </h3>
                    <p className="mt-1 break-all font-mono text-2xs text-bone-faint">
                      {approval.folioId
                        ? `folio ${approval.folioId}`
                        : `guest ${approval.guestId}`}
                    </p>
                    {approval.arguments && Object.keys(approval.arguments).length > 0 && (
                      <dl className="mt-2.5 grid grid-cols-[max-content_1fr] gap-x-3 gap-y-1 border-l-2 border-line pl-3">
                        {Object.entries(approval.arguments).map(([name, value]) => (
                          <div key={name} className="contents">
                            <dt className="font-mono text-2xs uppercase tracking-[0.08em] text-bone-faint">
                              {name}
                            </dt>
                            <dd className="break-words font-mono text-2xs text-bone">
                              {String(value)}
                            </dd>
                          </div>
                        ))}
                      </dl>
                    )}
                    <p className="mt-2.5 text-[13px] leading-relaxed text-bone">
                      {approval.reason}
                    </p>
                    <p className="mt-2 font-mono text-2xs text-bone-faint">
                      {approval.proposedBy} · {new Date(approval.createdAt).toLocaleString()}
                      {approval.runId && (
                        <button
                          onClick={() => onOpenRun(approval.runId as string)}
                          className="ml-2 text-dusk underline decoration-dusk-deep underline-offset-2 hover:decoration-dusk"
                        >
                          see the run
                        </button>
                      )}
                    </p>
                  </div>
                  <span
                    className={`shrink-0 border px-2 py-px font-mono text-2xs uppercase tracking-[0.1em] ${STATUS_STYLE[approval.status]}`}
                  >
                    {approval.status.toLowerCase()}
                  </span>
                </div>

                {approval.status === 'PENDING' && mayApprove && (
                  <div className="mt-3.5 border-t border-line pt-3">
                    <input
                      value={notes[approval.id] ?? ''}
                      onChange={(e) =>
                        setNotes((prev) => ({ ...prev, [approval.id]: e.target.value }))
                      }
                      placeholder="Why are you releasing this? Recorded against your name."
                      className="w-full border border-line bg-ink px-2.5 py-2 text-[13px] text-bone placeholder-bone-faint outline-none focus:border-line-bright"
                    />
                    <div className="mt-2 flex items-center gap-2">
                      <button
                        onClick={() => void decide(approval, true)}
                        disabled={busy === approval.id}
                        className="flex items-center gap-1.5 border border-sage-deep bg-sage-wash px-3 py-1.5 font-mono text-2xs uppercase tracking-[0.1em] text-sage transition-colors hover:bg-sage/15 disabled:opacity-35"
                      >
                        {busy === approval.id ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          <Check className="h-3 w-3" />
                        )}
                        release
                      </button>
                      <button
                        onClick={() => void decide(approval, false)}
                        disabled={busy === approval.id}
                        className="flex items-center gap-1.5 border border-line px-3 py-1.5 font-mono text-2xs uppercase tracking-[0.1em] text-bone-dim transition-colors hover:border-clay-deep hover:text-clay disabled:opacity-35"
                      >
                        <X className="h-3 w-3" />
                        refuse
                      </button>
                      <p className="ml-1 font-mono text-2xs leading-tight text-bone-faint">
                        bound to this action, target and every argument above · expires in 15 min
                      </p>
                    </div>
                  </div>
                )}

                {approval.decidedBy && (
                  <p className="mt-3 border-t border-line pt-2.5 text-2xs text-bone-dim">
                    <span className="font-mono uppercase tracking-[0.1em]">
                      {approval.status === 'APPROVED' ? 'released' : 'refused'}
                    </span>{' '}
                    by {approval.decidedBy} — “{approval.decisionNote}”
                    {approval.executionRunId && (
                      <button
                        onClick={() => onOpenRun(approval.executionRunId as string)}
                        className="ml-2 text-dusk underline decoration-dusk-deep underline-offset-2 hover:decoration-dusk"
                      >
                        see what happened
                      </button>
                    )}
                  </p>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
