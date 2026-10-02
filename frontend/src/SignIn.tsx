// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Sign-in.
 *
 * The one screen that gets to be composed rather than dense, because it is the only
 * one nobody is working in. An off-centre card against the lamp glow, and the wordmark
 * large enough to establish the type pairing before the panel does.
 *
 * Staff use the accounts they already have in the hotel platform's Cognito pool. There
 * is no new user directory here and no new app client: the platform's SPA client
 * already permits SRP, which is all a form needs.
 */

import { ArrowRight, Loader2 } from 'lucide-react';
import { useState } from 'react';
import { useAuth } from './auth';
import { ThemeToggle } from './components/ThemeToggle';

export function SignIn() {
  const { signIn, error } = useAuth();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setBusy(true);
    try {
      await signIn(email, password);
    } catch {
      /* surfaced through `error` */
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="grid-field grid h-full place-items-center px-6">
      <div className="w-full max-w-[380px] animate-rise">
        {/* The switch is reachable before sign-in too. Someone whose system preference
            disagrees with the room they are sitting in should not have to authenticate
            first to fix it. */}
        <div className="mb-8 flex items-start justify-between gap-4">
          <div>
            <h1 className="font-display text-[34px] font-normal leading-none tracking-tight text-bone">
              Night&nbsp;Desk
            </h1>
            <p className="mt-3 font-mono text-2xs uppercase tracking-[0.14em] text-bone-faint">
              AnyCompany Hotels · agent operations
            </p>
          </div>
          <ThemeToggle />
        </div>

        <form onSubmit={submit} className="panel px-5 py-5 shadow-lamp">
          <label className="block">
            <span className="dial-label">staff email</span>
            <input
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              type="email"
              autoComplete="username"
              autoFocus
              placeholder="you@anycompanyhotels.com"
              className="mt-1.5 w-full border border-line bg-ink px-2.5 py-2 text-[13px] text-bone placeholder-bone-faint outline-none focus:border-line-bright"
            />
          </label>

          <label className="mt-3.5 block">
            <span className="dial-label">password</span>
            <input
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              type="password"
              autoComplete="current-password"
              className="mt-1.5 w-full border border-line bg-ink px-2.5 py-2 text-[13px] text-bone outline-none focus:border-line-bright"
            />
          </label>

          {error && (
            <p className="mt-3.5 border-l-2 border-clay pl-2.5 text-[13px] leading-snug text-clay">
              {error}
            </p>
          )}

          <button
            type="submit"
            disabled={busy || !email || !password}
            className="mt-5 flex w-full items-center justify-center gap-2 border border-ember-deep bg-ember-wash py-2.5 font-mono text-2xs uppercase tracking-[0.14em] text-ember transition-colors hover:bg-ember/15 disabled:opacity-35"
          >
            {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : null}
            sign in
            {!busy && <ArrowRight className="h-3.5 w-3.5" />}
          </button>
        </form>

        <p className="mt-5 text-2xs leading-relaxed text-bone-faint">
          The same directory the property management system uses. What you can see and
          release here follows the groups already on your account.
        </p>
      </div>
    </div>
  );
}
