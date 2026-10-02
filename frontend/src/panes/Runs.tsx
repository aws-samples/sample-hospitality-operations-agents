// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Run history: what the agents did, and whether a human thought they were right.
 *
 * The verdict control is the point of the pane. Traces answer "what happened"; no
 * amount of tracing answers "was the agent right", because that answer arrives days
 * later when a person either lets a decision stand or reverses it. That verdict is the
 * ground truth the Phase-5 judges are ultimately measured against, which is why both
 * directions are offered — **endorsed** as well as **overridden**. Collecting only the
 * failures would teach an evaluator that every reviewed decision was wrong.
 */

import { AlertTriangle, Loader2, ThumbsDown, ThumbsUp } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { api, type RunDetail, type RunSummary } from '../api';
import { Markdown } from '../components/Markdown';
import { Trajectory } from '../components/Trajectory';

const TRIGGER_STYLE: Record<string, string> = {
  chat: 'border-dusk-deep bg-dusk-wash text-dusk',
  schedule: 'border-line-bright bg-ink-3 text-bone-dim',
  event: 'border-sage-deep bg-sage-wash text-sage',
};

/** Markdown stripped back to a readable one-line preview.
 *
 *  The ledger shows a truncated excerpt of the answer, and agents write markdown — so
 *  before this the list was full of `## Portfolio review` and `**regional_agent**` with
 *  the syntax showing. Rendering markdown in a two-line clamp is the wrong fix: a
 *  heading and a table inside a list row would wreck the rhythm of the column. What a
 *  preview wants is the prose with the notation taken out. */
function plain(markdown: string): string {
  return markdown
    .replace(/```[\s\S]*?```/g, ' ')      // fenced code: never useful in a preview
    .replace(/^#{1,6}\s+/gm, '')           // heading markers
    .replace(/^\s*[-*+]\s+/gm, '')        // list bullets
    .replace(/^\s*>\s?/gm, '')            // block quotes
    .replace(/^\s*\|.*\|\s*$/gm, ' ')   // whole table rows
    .replace(/\*\*([^*]+)\*\*/g, '$1')  // bold
    .replace(/(^|\W)_([^_]+)_(?=\W|$)/g, '$1$2') // italic, without eating snake_case
    .replace(/`([^`]+)`/g, '$1')            // inline code
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1') // links
    .replace(/\s+/g, ' ')
    .trim();
}

function relative(ts: string): string {
  const seconds = (Date.now() - new Date(ts).getTime()) / 1000;
  if (seconds < 90) return 'just now';
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`;
  return new Date(ts).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

export function Runs({
  propertyId,
  openRunId,
  onOpened,
}: {
  propertyId: string;
  openRunId: string | null;
  onOpened: () => void;
}) {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selected, setSelected] = useState<RunDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setRuns((await api.listRuns(propertyId || null)).runs);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load run history.');
    } finally {
      setLoading(false);
    }
  }, [propertyId]);

  useEffect(() => {
    void load();
  }, [load]);

  const open = useCallback(async (runId: string) => {
    try {
      setSelected(await api.getRun(runId));
      setReason('');
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not open that run.');
    }
  }, []);

  // Deep link from the approvals pane. Depends on the id alone -- the first version
  // also depended on the two callbacks, whose identities changed on every parent
  // render, so it re-opened the run in a loop.
  useEffect(() => {
    if (!openRunId) return;
    void open(openRunId);
    onOpened();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openRunId]);

  const record = async (verdict: 'endorsed' | 'overridden') => {
    if (!selected || !reason.trim()) {
      setError('Say why. A bare verdict is a weak signal; the reason is the useful part.');
      return;
    }
    setBusy(true);
    try {
      await api.override(selected.runId, verdict, reason.trim());
      await open(selected.runId);
      await load();
      setReason('');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not record that.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full">
      {/* The ledger. Dense, monospace, scannable — this is a log, not a feed. */}
      <div className="w-[38%] min-w-[300px] shrink-0 overflow-y-auto border-r border-line">
        <div className="sticky top-0 z-10 flex items-baseline justify-between border-b border-line bg-ink/90 px-4 py-2.5 backdrop-blur">
          <span className="dial-label">
            {propertyId ? 'runs · this property' : 'runs · chain-wide'}
          </span>
          <span className="font-mono text-2xs text-bone-faint">{runs.length}</span>
        </div>

        {loading ? (
          <p className="px-4 py-4">
            <Loader2 className="h-3.5 w-3.5 animate-spin text-bone-faint" />
          </p>
        ) : runs.length === 0 ? (
          <p className="px-4 py-6 text-[13px] leading-relaxed text-bone-faint">
            No runs yet. Ask something in Copilot, or arm the schedules.
          </p>
        ) : (
          <ul>
            {runs.map((run, index) => {
              const active = selected?.runId === run.runId;
              return (
                <li key={run.runId}>
                  <button
                    onClick={() => void open(run.runId)}
                    style={{ animationDelay: `${Math.min(index, 10) * 18}ms` }}
                    className={`animate-slide-in w-full border-b border-line px-4 py-2.5 text-left transition-colors ${
                      active ? 'bg-ember/[0.07]' : 'hover:bg-ink-2'
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      {/* A left edge marker rather than a border: the active row reads
                          as selected without shifting any text. */}
                      <span
                        aria-hidden
                        className={`h-3 w-px ${active ? 'bg-ember' : 'bg-transparent'}`}
                      />
                      <span
                        className={`border px-1.5 py-px font-mono text-2xs uppercase tracking-[0.08em] ${
                          TRIGGER_STYLE[run.trigger] ?? 'border-line bg-ink-3 text-bone-faint'
                        }`}
                      >
                        {run.trigger}
                      </span>
                      <span className="font-mono text-2xs text-bone-faint">
                        {relative(run.ts)}
                      </span>

                      {run.status === 'queued' && (
                        <span className="border border-ember-deep bg-ember-wash px-1.5 py-px font-mono text-2xs text-ember">
                          queued
                        </span>
                      )}
                      {run.outcome === 'failed' && (
                        <span className="border border-clay-deep bg-clay-wash px-1.5 py-px font-mono text-2xs text-clay">
                          failed
                        </span>
                      )}
                      {run.humanOverride && (
                        <span
                          className={`ml-auto font-mono text-2xs ${
                            run.humanOverride.verdict === 'endorsed' ? 'text-sage' : 'text-ember'
                          }`}
                        >
                          {run.humanOverride.verdict}
                        </span>
                      )}
                    </div>

                    <p className="mt-1.5 line-clamp-2 pl-3 text-[13px] leading-snug text-bone">
                      {plain(run.excerpt || run.prompt || '—')}
                    </p>
                    <p className="mt-1 pl-3 font-mono text-2xs text-bone-faint">
                      {run.delegations.map((d) => d.replace(/_agent$/, '')).join(' → ') ||
                        'no delegation'}
                      {run.durationSeconds ? ` · ${Math.round(run.durationSeconds)}s` : ''}
                    </p>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </div>

      {/* The record. */}
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-2xl px-6 py-6">
          {error && (
            <div className="mb-4 flex items-start gap-2 border border-clay-deep bg-clay-wash px-3 py-2.5 text-[13px] text-clay">
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {!selected ? (
            <p className="pt-10 text-center text-[13px] text-bone-faint">
              Pick a run to see what it did.
            </p>
          ) : (
            <div className="animate-rise">
              <p className="break-all font-mono text-2xs text-bone-faint">
                {selected.runId}
                {selected.askedBy && <span> · asked by {selected.askedBy}</span>}
              </p>

              {selected.prompt && (
                <p className="mt-3 border-l-2 border-line-bright bg-ink-2 px-3 py-2 text-[13px] leading-relaxed text-bone-dim">
                  {selected.prompt}
                </p>
              )}

              <div className="mt-5">
                {selected.status === 'complete' ? (
                  <Markdown>{selected.answer ?? '_No answer recorded._'}</Markdown>
                ) : (
                  <p className="flex items-center gap-2 font-mono text-2xs uppercase tracking-[0.12em] text-bone-dim">
                    <Loader2 className="h-3 w-3 animate-spin" />
                    {selected.status}
                  </p>
                )}
              </div>

              {selected.answerTruncated && (
                <p className="mt-2 font-mono text-2xs text-bone-faint">
                  the stored answer was truncated for storage
                </p>
              )}

              {selected.steps.length > 0 && (
                <div className="mt-6 border-t border-line pt-4">
                  <Trajectory steps={selected.steps} live={selected.status !== 'complete'} />
                </div>
              )}

              <section className="panel mt-6 px-4 py-3.5">
                <h3 className="font-display text-[15px] font-normal text-bone">
                  Was this right?
                </h3>
                <p className="mt-1.5 text-2xs leading-relaxed text-bone-dim">
                  Your verdict is the ground truth the evaluators are scored against.
                  Both answers are useful — recording only the mistakes would teach it
                  that every reviewed decision was wrong.
                </p>

                {selected.humanOverride && (
                  <p className="mt-3 border-l-2 border-line-bright pl-3 text-[13px] text-bone">
                    <span
                      className={
                        selected.humanOverride.verdict === 'endorsed'
                          ? 'font-mono text-2xs uppercase tracking-[0.1em] text-sage'
                          : 'font-mono text-2xs uppercase tracking-[0.1em] text-ember'
                      }
                    >
                      {selected.humanOverride.verdict}
                    </span>{' '}
                    by {selected.humanOverride.by} — “{selected.humanOverride.reason}”
                  </p>
                )}

                <input
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                  placeholder={
                    selected.humanOverride
                      ? 'Change the verdict — say why.'
                      : 'What did it get right or wrong, and how do you know?'
                  }
                  className="mt-3 w-full border border-line bg-ink px-2.5 py-2 text-[13px] text-bone placeholder-bone-faint outline-none focus:border-line-bright"
                />
                <div className="mt-2 flex gap-2">
                  <button
                    onClick={() => void record('endorsed')}
                    disabled={busy}
                    className="flex items-center gap-1.5 border border-sage-deep px-3 py-1.5 font-mono text-2xs uppercase tracking-[0.1em] text-sage transition-colors hover:bg-sage-wash disabled:opacity-35"
                  >
                    <ThumbsUp className="h-3 w-3" /> it was right
                  </button>
                  <button
                    onClick={() => void record('overridden')}
                    disabled={busy}
                    className="flex items-center gap-1.5 border border-ember-deep px-3 py-1.5 font-mono text-2xs uppercase tracking-[0.1em] text-ember transition-colors hover:bg-ember-wash disabled:opacity-35"
                  >
                    <ThumbsDown className="h-3 w-3" /> i overrode it
                  </button>
                </div>
              </section>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
