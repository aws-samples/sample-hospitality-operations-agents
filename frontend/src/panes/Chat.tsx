// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The copilot pane.
 *
 * There is no token streaming, and that is a hard limit rather than a shortcut: API
 * Gateway's REST integration timeout is 29 seconds and these runs take 30 to 650, so a
 * synchronous streamed response would time out on every real question. What streams
 * instead is the *trajectory* — one node per tool call, appearing as the agent makes
 * it. For an operations console that is the better artifact: it is the audit trail,
 * being written in front of you.
 *
 * The polling here is deliberate in a way the first version was not. That version put
 * `turns` in the effect's dependency array and called `setTurns` inside the tick, so
 * every response re-ran the effect, which cleared the interval and immediately fired
 * another request. It never honoured the two-second interval at all: it polled as fast
 * as the network allowed. One day of light use produced 450 404s and 33 throttled
 * requests — an 18% error rate against an endpoint that was working correctly.
 *
 * The fix is to key the poller on the *run id* alone. One interval per run, torn down
 * when the run completes or the id changes, and state written through a ref-free
 * functional update so the effect never depends on what it produces.
 */

import { AlertTriangle, CornerDownLeft, Loader2, MessageSquarePlus } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import { ApiError, api, type RunDetail } from '../api';
import { Markdown } from '../components/Markdown';
import { Trajectory } from '../components/Trajectory';

interface Turn {
  id: number;
  prompt: string;
  runId: string | null;
  detail: RunDetail | null;
  error: string | null;
}

const POLL_MS = 2000;

const OPENERS = [
  {
    label: 'Pre-assign rooms',
    hint: 'A1 · writes',
    prompt: 'Pre-assign rooms for the unassigned arrivals at this property.',
  },
  {
    label: 'Night-audit readiness',
    hint: 'A4 · advises',
    prompt: 'Are we ready for tonight’s audit?',
  },
  {
    label: 'Housekeeping board',
    hint: 'A2 · writes',
    prompt: 'Sequence and assign the open housekeeping tasks at this property.',
  },
  {
    label: 'Portfolio movement',
    hint: 'A5 · advises · clear the property first',
    prompt:
      'How did the portfolio do yesterday? Which properties moved most against their trend?',
  },
];

/** Polls one run until it completes. Mounted per in-flight run, so its only
 *  dependency is the id — which is what stops the storm. */
function useRunPoll(
  runId: string | null,
  active: boolean,
  onUpdate: (detail: RunDetail) => void,
  onError: (message: string) => void,
) {
  // Held in refs so the callbacks changing identity on every parent render cannot
  // restart the interval. The effect depends on the run id and nothing else.
  const update = useRef(onUpdate);
  const fail = useRef(onError);
  update.current = onUpdate;
  fail.current = onError;

  useEffect(() => {
    if (!runId || !active) return;
    let cancelled = false;

    const tick = async () => {
      try {
        const detail = await api.getRun(runId);
        if (!cancelled) update.current(detail);
      } catch (err) {
        // A run that has not written its first row yet is not an error. The API now
        // records a `queued` row at enqueue time so this is rare, but a redrive can
        // still open the window.
        if (cancelled || (err instanceof ApiError && err.isPending)) return;
        fail.current(err instanceof Error ? err.message : 'Lost track of this run.');
      }
    };

    void tick();
    const timer = setInterval(tick, POLL_MS);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [runId, active]);
}

function TurnCard({
  turn,
  onUpdate,
  onError,
}: {
  turn: Turn;
  onUpdate: (detail: RunDetail) => void;
  onError: (message: string) => void;
}) {
  const done = turn.detail?.status === 'complete';
  useRunPoll(turn.runId, !done && !turn.error, onUpdate, onError);

  const delegations = turn.detail?.summary?.delegations ?? [];
  const failed = turn.detail?.summary?.outcome === 'failed';

  return (
    <article className="animate-rise">
      {/* The question. Right-aligned and quiet — it is context, not content. */}
      <div className="mb-2 flex justify-end">
        <p className="max-w-[75%] border-r-2 border-ember-deep bg-ink-3 px-3 py-1.5 text-right text-[13px] text-bone">
          {turn.prompt}
        </p>
      </div>

      {turn.error && (
        <div className="flex items-start gap-2 border border-clay-deep bg-clay-wash px-3 py-2.5 text-[13px] text-clay">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{turn.error}</span>
        </div>
      )}

      {turn.runId && !turn.error && (
        <div
          className={`panel ${!done ? 'sweeping' : ''} px-4 py-3.5`}
        >
          {/* Who answered, and the run id. Both matter to an operator who disagrees:
              one tells them which specialist to argue with, the other is what they
              quote when they do. */}
          <header className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1.5">
            {delegations.length > 0 ? (
              <div className="flex items-center gap-1.5">
                {delegations.map((agent, i) => (
                  <span key={`${agent}-${i}`} className="flex items-center gap-1.5">
                    {i > 0 && <span className="font-mono text-2xs text-bone-faint">→</span>}
                    <span className="border border-dusk-deep bg-dusk-wash px-1.5 py-px font-mono text-2xs text-dusk">
                      {agent.replace(/_agent$/, '')}
                    </span>
                  </span>
                ))}
              </div>
            ) : (
              <span className="dial-label">orchestrator</span>
            )}

            <span className="ml-auto font-mono text-2xs text-bone-faint" title={turn.runId}>
              {turn.runId.slice(0, 8)}
              {typeof turn.detail?.summary?.durationSeconds === 'number' && (
                <span className="text-bone-faint"> · {Math.round(turn.detail.summary.durationSeconds)}s</span>
              )}
            </span>
          </header>

          {done ? (
            failed ? (
              <div className="border border-clay-deep bg-clay-wash px-3 py-2 text-[13px] text-clay">
                This run failed: {turn.detail?.summary?.errorCode ?? 'no reason recorded'}.
                {turn.detail?.answer ? <> The agent got as far as: {turn.detail.answer}</> : null}
              </div>
            ) : (
              <Markdown>{turn.detail?.answer ?? '_The agent produced no text._'}</Markdown>
            )
          ) : (
            <p className="flex items-center gap-2 font-mono text-2xs uppercase tracking-[0.12em] text-bone-dim">
              <Loader2 className="h-3 w-3 animate-spin" />
              {turn.detail?.status === 'queued'
                ? 'queued'
                : `working · ${turn.detail?.stepCount ?? 0} calls`}
            </p>
          )}

          {(turn.detail?.steps?.length ?? 0) > 0 && (
            <div className="mt-4 border-t border-line pt-3">
              <Trajectory steps={turn.detail!.steps} live={!done} />
            </div>
          )}
        </div>
      )}
    </article>
  );
}

export function Chat({ propertyId }: { propertyId: string }) {
  const [prompt, setPrompt] = useState('');
  const [turns, setTurns] = useState<Turn[]>([]);
  const [sending, setSending] = useState(false);
  const nextId = useRef(0);
  const bottom = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [turns.length]);

  const patch = useCallback((id: number, changes: Partial<Turn>) => {
    setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, ...changes } : t)));
  }, []);

  /** Clear the transcript.
   *
   * Purely a view reset, and deliberately so: `POST /chat` mints a fresh run id per
   * question and sends no prior turns, so two questions in this pane never shared a
   * conversation to begin with. What this discards is the transcript, not context.
   *
   * An in-flight run is not cancelled — dropping its card unmounts the poller, and the
   * run finishes and lands in Runs either way. That is the honest behaviour: the console
   * cannot recall a run the Runtime has already been handed. */
  const reset = useCallback(() => {
    setTurns([]);
    setPrompt('');
  }, []);

  const send = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || sending) return;
      setSending(true);
      setPrompt('');

      const id = nextId.current++;
      setTurns((prev) => [...prev, { id, prompt: trimmed, runId: null, detail: null, error: null }]);

      try {
        const started = await api.startChat({
          prompt: trimmed,
          // Omitted when chain-wide, which is what A5 needs. A2 will refuse a
          // chain-wide run and say why.
          ...(propertyId ? { propertyId } : {}),
        });
        patch(id, { runId: started.runId });
      } catch (err) {
        patch(id, {
          error: err instanceof Error ? err.message : 'Could not start the run.',
        });
      } finally {
        setSending(false);
      }
    },
    [propertyId, sending, patch],
  );

  return (
    <div className="flex h-full flex-col">
      <div className="flex-1 overflow-y-auto">
        {/* Only once there is something to clear. On an empty transcript you are already
            in a new chat, and the empty state is the pane's first impression. Matches
            the Runs ledger header so the two panes share one piece of chrome. */}
        {turns.length > 0 && (
          <div className="sticky top-0 z-10 flex items-baseline justify-between border-b border-line bg-ink/90 px-6 py-2.5 backdrop-blur">
            <span className="dial-label">
              {turns.length} {turns.length === 1 ? 'question' : 'questions'}
            </span>
            <button
              type="button"
              onClick={reset}
              className="flex items-center gap-1.5 border border-line px-3 py-1.5 font-mono text-2xs uppercase tracking-[0.1em] text-bone-dim transition-colors hover:border-ember-deep hover:text-ember"
            >
              <MessageSquarePlus className="h-3 w-3" /> new chat
            </button>
          </div>
        )}

        <div className="mx-auto max-w-3xl space-y-6 px-6 py-6">
          {turns.length === 0 && (
            <div className="animate-rise pt-8">
              <h2 className="font-display text-[22px] font-normal tracking-tight text-bone">
                Ask the operations orchestrator
              </h2>
              <p className="mt-2 max-w-xl text-[13px] leading-relaxed text-bone-dim">
                It routes to one of five specialists and reports which one answered.
                Room assignment and housekeeping act on their own authority. Anything
                touching money comes back here as a proposal for a human to release.
              </p>

              <div className="mt-6 grid gap-px border border-line bg-line sm:grid-cols-2">
                {OPENERS.map((o) => (
                  <button
                    key={o.label}
                    onClick={() => void send(o.prompt)}
                    className="group bg-ink-2 px-4 py-3 text-left transition-colors hover:bg-ink-3"
                  >
                    <span className="block text-[13px] text-bone group-hover:text-ember">
                      {o.label}
                    </span>
                    <span className="mt-1 block font-mono text-2xs uppercase tracking-[0.1em] text-bone-faint">
                      {o.hint}
                    </span>
                  </button>
                ))}
              </div>

              <p className="mt-5 font-mono text-2xs text-bone-faint">
                Runs take one to four minutes. Tool calls appear as they happen.
              </p>
            </div>
          )}

          {turns.map((turn) => (
            <TurnCard
              key={turn.id}
              turn={turn}
              onUpdate={(detail) => patch(turn.id, { detail })}
              onError={(message) => patch(turn.id, { error: message })}
            />
          ))}
          <div ref={bottom} />
        </div>
      </div>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          void send(prompt);
        }}
        className="border-t border-line bg-ink"
      >
        <div className="mx-auto flex max-w-3xl items-end gap-2 px-6 py-4">
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                void send(prompt);
              }
            }}
            rows={1}
            placeholder={propertyId ? 'Ask about this property…' : 'Ask about the portfolio…'}
            className="flex-1 resize-none border border-line bg-ink-2 px-3 py-2.5 text-[13px] text-bone placeholder-bone-faint outline-none transition-colors focus:border-line-bright"
          />
          <button
            type="submit"
            disabled={sending || !prompt.trim()}
            className="flex h-[42px] items-center gap-1.5 border border-ember-deep bg-ember-wash px-4 font-mono text-2xs uppercase tracking-[0.12em] text-ember transition-colors hover:bg-ember/15 disabled:opacity-35"
          >
            {sending ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <CornerDownLeft className="h-3.5 w-3.5" />
            )}
            ask
          </button>
        </div>
      </form>
    </div>
  );
}
