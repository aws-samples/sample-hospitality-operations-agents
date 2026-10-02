// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The trajectory spine — the centrepiece of this console.
 *
 * Every other agent UI puts the prose first and buries the tool calls behind a
 * disclosure triangle. That is backwards for an operations tool: the prose is the
 * agent's *claim*, and the trajectory is the *evidence*. This project's whole thesis
 * is that an agent can be trusted to act unsupervised because what it did is
 * inspectable, so what it did gets a spine down the middle of the screen.
 *
 * Read it top to bottom. Each node is one tool call, in the order the agent made it.
 * The colour vocabulary is the same everywhere in the app and needs no legend once
 * you have seen it twice:
 *
 *   filled ember   a write landed — the hotel's state changed
 *   hollow sage    a read succeeded
 *   hollow clay    refused or errored, with the platform's own code shown
 *
 * A write node is the only filled one. Scanning a forty-step trajectory for "did this
 * actually change anything" should take no reading at all.
 */

import type { RunStep } from '../api';

/** Tool names arrive Gateway-qualified: `arrivals___assign_room`. Three underscores,
 *  which is what the deployed Gateway uses — split on any run of two or more rather
 *  than assuming a width, because assuming two is a bug this project already had once
 *  and it failed silently on every dispatch. */
function readable(tool: string): { agent: string; action: string } {
  const parts = tool.split(/_{2,}/);
  return parts.length > 1
    ? { agent: parts[0], action: parts.slice(1).join(' ').replace(/_/g, ' ') }
    : { agent: '', action: tool.replace(/_/g, ' ') };
}

function time(ts: string): string {
  return new Date(ts).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

type Tone = 'write' | 'ok' | 'bad' | 'unknown';

function toneOf(step: RunStep): Tone {
  if (step.actionTaken) return 'write';
  if (step.errorCode || step.outcome === 'tool_error' || step.outcome === 'protocol_error')
    return 'bad';
  if (step.outcome === 'ok') return 'ok';
  return 'unknown';
}

const NODE: Record<Tone, string> = {
  write: 'bg-ember border-ember',
  ok: 'bg-ink-2 border-sage',
  bad: 'bg-ink-2 border-clay',
  unknown: 'bg-ink-2 border-line-bright',
};

export function Trajectory({
  steps,
  live = false,
}: {
  steps: RunStep[];
  /** True while the run is still going, so the spine ends in a pulsing node rather
   *  than just stopping — the difference between "finished" and "still working". */
  live?: boolean;
}) {
  if (!steps.length) {
    return live ? (
      <div className="flex items-center gap-2.5 py-1">
        <span className="h-1.5 w-1.5 animate-breathe rounded-full bg-ember" />
        <span className="font-mono text-2xs uppercase tracking-[0.12em] text-bone-faint">
          waiting for the first tool call
        </span>
      </div>
    ) : null;
  }

  const writes = steps.filter((s) => s.actionTaken).length;
  const failures = steps.filter((s) => toneOf(s) === 'bad').length;

  return (
    <div>
      <div className="mb-2.5 flex items-baseline gap-3">
        <span className="dial-label">trajectory</span>
        <span className="font-mono text-2xs text-bone-faint">
          {steps.length} call{steps.length === 1 ? '' : 's'}
          {writes > 0 && <span className="text-ember"> · {writes} wrote</span>}
          {failures > 0 && <span className="text-clay"> · {failures} refused</span>}
        </span>
      </div>

      <ol className="relative">
        {/* The spine. Inset to pass through the centre of each node. */}
        <span
          aria-hidden
          className="absolute bottom-2 left-[3.5px] top-2 w-px bg-line-bright"
        />

        {steps.map((step, index) => {
          const tone = toneOf(step);
          const { agent, action } = readable(step.tool);
          return (
            <li
              key={`${step.ts}-${index}`}
              className="relative flex animate-slide-in items-baseline gap-3 py-[3px] pl-5"
              // Staggered, but capped: a 110-step A5 trajectory should not take
              // eleven seconds to finish appearing.
              style={{ animationDelay: `${Math.min(index, 14) * 22}ms` }}
            >
              <span
                aria-hidden
                className={`absolute left-0 top-[9px] h-2 w-2 rounded-full border shadow-node ${NODE[tone]}`}
              />

              <time className="w-[58px] shrink-0 font-mono text-2xs text-bone-faint">
                {time(step.ts)}
              </time>

              <span className="w-[92px] shrink-0 truncate font-mono text-2xs text-dusk">
                {step.agent || agent || '—'}
              </span>

              <span
                className={`font-mono text-xs ${
                  tone === 'write' ? 'text-ember' : 'text-bone-dim'
                }`}
              >
                {action}
              </span>

              {step.actionTaken && (
                <span className="rounded-sm border border-ember-deep bg-ember-wash px-1.5 py-px font-mono text-2xs uppercase tracking-[0.08em] text-ember">
                  wrote
                </span>
              )}
              {step.errorCode && (
                <span
                  className="truncate rounded-sm border border-clay-deep bg-clay-wash px-1.5 py-px font-mono text-2xs text-clay"
                  title={step.errorCode}
                >
                  {step.errorCode}
                </span>
              )}
            </li>
          );
        })}

        {live && (
          <li className="relative flex items-center gap-3 py-[3px] pl-5">
            <span
              aria-hidden
              className="absolute left-0 top-[7px] h-2 w-2 animate-breathe rounded-full border border-ember bg-ember shadow-node"
            />
            <span className="font-mono text-2xs uppercase tracking-[0.12em] text-bone-faint">
              working
            </span>
          </li>
        )}
      </ol>
    </div>
  );
}
