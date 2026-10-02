// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The shell: a left rail, a status strip, and one pane.
 *
 * Laid out as an instrument panel rather than as a chat app. The rail is always the
 * same width and always in the same place, the strip always carries the same four
 * facts, and the content area is the only thing that changes — which is what lets
 * someone who uses this every night stop looking at the chrome.
 *
 * The serif wordmark against monospace everything-else is the one deliberate
 * incongruity: this is a hotel, and hotels are warm, and the tool that runs one at
 * 03:00 does not have to look like a terminal to behave like one.
 */

import { ClipboardList, LogOut, MessageSquare, ShieldCheck } from 'lucide-react';
import { useCallback, useEffect, useState } from 'react';
import { api, setUnauthorizedHandler, type Property } from './api';
import { useAuth } from './auth';
import { SignIn } from './SignIn';
import { PropertyPicker, rememberedProperty } from './components/PropertyPicker';
import { ThemeToggle } from './components/ThemeToggle';
import { Approvals } from './panes/Approvals';
import { Chat } from './panes/Chat';
import { Runs } from './panes/Runs';

type Pane = 'chat' | 'approvals' | 'runs';

const PANES: { id: Pane; label: string; icon: typeof MessageSquare }[] = [
  { id: 'chat', label: 'copilot', icon: MessageSquare },
  { id: 'approvals', label: 'approvals', icon: ShieldCheck },
  { id: 'runs', label: 'runs', icon: ClipboardList },
];

export function App() {
  const { session, loading, signOut } = useAuth();
  const [pane, setPane] = useState<Pane>('chat');
  const [propertyId, setPropertyId] = useState('');
  const [openRunId, setOpenRunId] = useState<string | null>(null);
  const [pending, setPending] = useState<number | null>(null);
  const [properties, setProperties] = useState<Property[]>([]);
  const [boundToOne, setBoundToOne] = useState(false);
  const [propertiesLoading, setPropertiesLoading] = useState(true);
  const [propertiesError, setPropertiesError] = useState<string | null>(null);

  // A 401 anywhere means the hour-long ID token expired. Sign out cleanly rather than
  // letting every pane show its own error banner, which reads as an outage.
  useEffect(() => setUnauthorizedHandler(signOut), [signOut]);

  // The pending count lives in the strip so an approval waiting on a human is visible
  // from whichever pane you happen to be on. That is the whole reason it is in the
  // chrome and not only inside the approvals pane.
  const refreshPending = useCallback(async () => {
    if (!session) return;
    try {
      setPending((await api.listApprovals('PENDING')).count);
    } catch {
      setPending(null);
    }
  }, [session]);

  useEffect(() => {
    void refreshPending();
    const timer = setInterval(() => void refreshPending(), 30000);
    return () => clearInterval(timer);
  }, [refreshPending, pane]);

  // Fetched once per sign-in, not polled: a chain's property list does not change
  // during a shift, and the reply is already scoped to this operator by the platform.
  useEffect(() => {
    if (!session) return;
    let cancelled = false;
    void (async () => {
      try {
        const { properties: list, scope } = await api.listProperties();
        if (cancelled) return;
        setProperties(list);
        setBoundToOne(scope.boundToOneProperty);
        // A scoped user's property comes from their token, so only a chain-level
        // operator has a remembered choice to restore.
        if (!scope.boundToOneProperty) {
          setPropertyId(rememberedProperty(session.email, list));
        }
      } catch (err) {
        if (!cancelled) {
          setPropertiesError(err instanceof Error ? err.message : 'Could not load properties.');
        }
      } finally {
        if (!cancelled) setPropertiesLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [session]);

  if (loading) {
    return (
      <div className="grid h-full place-items-center">
        <span className="dial-label animate-breathe">loading</span>
      </div>
    );
  }
  if (!session) return <SignIn />;

  const scope = session.propertyId ?? propertyId;

  return (
    <div className="grid h-full grid-cols-[168px_1fr] grid-rows-[auto_1fr] overflow-hidden">
      {/* ── Rail ─────────────────────────────────────────────────────────── */}
      <aside className="row-span-2 flex flex-col border-r border-line bg-ink-2">
        <div className="border-b border-line px-4 py-3.5">
          <h1 className="font-display text-[17px] font-normal leading-none tracking-tight text-bone">
            Night&nbsp;Desk
          </h1>
          <p className="mt-1.5 font-mono text-2xs uppercase tracking-[0.1em] text-bone-faint">
            AnyCompany Hotels
          </p>
        </div>

        <nav className="flex flex-col py-2">
          {PANES.map(({ id, label, icon: Icon }) => {
            const active = pane === id;
            return (
              <button
                key={id}
                onClick={() => setPane(id)}
                className={`relative flex items-center gap-2.5 px-4 py-2 font-mono text-2xs uppercase tracking-[0.1em] transition-colors ${
                  active ? 'text-bone' : 'text-bone-faint hover:text-bone-dim'
                }`}
              >
                {/* The active marker is a rail segment, not a pill — it reads as a
                    switch thrown on a panel. */}
                <span
                  aria-hidden
                  className={`absolute inset-y-1 left-0 w-[2px] ${active ? 'bg-ember' : 'bg-transparent'}`}
                />
                <Icon className={`h-3.5 w-3.5 ${active ? 'text-ember' : ''}`} />
                {label}
                {id === 'approvals' && !!pending && (
                  <span className="ml-auto border border-ember-deep bg-ember-wash px-1 font-mono text-2xs text-ember">
                    {pending}
                  </span>
                )}
              </button>
            );
          })}
        </nav>

        <div className="mt-auto space-y-2 border-t border-line px-4 py-3">
          <p className="truncate text-2xs text-bone-dim" title={session.email}>
            {session.email}
          </p>
          <p className="font-mono text-2xs uppercase tracking-[0.08em] text-bone-faint">
            {session.groups.join(' · ') || 'no groups'}
          </p>
          <div className="flex items-center justify-between gap-2">
            <button
              onClick={signOut}
              className="flex items-center gap-1.5 font-mono text-2xs uppercase tracking-[0.1em] text-bone-faint transition-colors hover:text-clay"
            >
              <LogOut className="h-3 w-3" /> sign out
            </button>
            <ThemeToggle compact />
          </div>
        </div>
      </aside>

      {/* ── Status strip ─────────────────────────────────────────────────── */}
      <header className="flex items-center gap-5 border-b border-line bg-ink-2 px-5 py-2">
        <div className="flex items-center gap-2">
          <span className="dial-label">property</span>
          <PropertyPicker
            properties={properties}
            loading={propertiesLoading}
            error={propertiesError}
            value={scope}
            onChange={setPropertyId}
            email={session.email}
            boundToOneProperty={boundToOne || !!session.propertyId}
          />
        </div>

        <span className="dial-label">
          scope
          <span className={`ml-2 ${scope ? 'text-sage' : 'text-dusk'}`}>
            {scope ? 'single property' : 'chain'}
          </span>
        </span>

        {session.mayApprove ? (
          <span className="dial-label">
            authority<span className="ml-2 text-ember">may release funds</span>
          </span>
        ) : (
          <span className="dial-label">
            authority<span className="ml-2 text-bone-dim">read &amp; override</span>
          </span>
        )}

        <span className="ml-auto dial-label">
          pending
          <span className={`ml-2 ${pending ? 'text-ember' : 'text-bone-dim'}`}>
            {pending ?? '—'}
          </span>
        </span>
      </header>

      {/* ── Pane ─────────────────────────────────────────────────────────── */}
      <main className="grid-field min-h-0 overflow-hidden">
        {pane === 'chat' && <Chat propertyId={scope} />}
        {pane === 'approvals' && (
          <Approvals
            onOpenRun={(runId) => {
              setOpenRunId(runId);
              setPane('runs');
            }}
          />
        )}
        {pane === 'runs' && (
          <Runs propertyId={scope} openRunId={openRunId} onOpened={() => setOpenRunId(null)} />
        )}
      </main>
    </div>
  );
}
