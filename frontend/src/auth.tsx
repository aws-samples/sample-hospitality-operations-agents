// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Sign-in against the foundation's existing Cognito user pool.
 *
 * Staff use the accounts they already have. There is no new user directory and no
 * new app client: the foundation's SPA client already permits `USER_SRP_AUTH`,
 * which is all a login form needs, and creating an app client would have added a
 * resource to a pool this project is only allowed to add *users* to. That
 * constraint is also why there is no Hosted UI here -- Hosted UI would need
 * callback URLs registered on a client we must not modify.
 *
 * The ID token, not the access token, is what goes to our API. Only the ID token
 * carries `cognito:groups` and `custom:property_id`, and those two claims are the
 * entire authorization model on both sides: our API scopes reads by them, and the
 * foundation's own `verify_property_access` gates writes by them.
 */

import { Amplify } from 'aws-amplify';
import {
  fetchAuthSession,
  getCurrentUser,
  signIn as amplifySignIn,
  signOut as amplifySignOut,
} from 'aws-amplify/auth';
import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';

const USER_POOL_ID = import.meta.env.VITE_USER_POOL_ID ?? '';
const CLIENT_ID = import.meta.env.VITE_USER_POOL_CLIENT_ID ?? '';

Amplify.configure({
  Auth: { Cognito: { userPoolId: USER_POOL_ID, userPoolClientId: CLIENT_ID } },
});

export interface Session {
  email: string;
  groups: string[];
  propertyId: string | null;
  /** True when this account may release money movement. Mirrors the API's own
   *  check rather than replacing it: the API is authoritative and re-checks, but a
   *  disabled button is a better experience than a 403. */
  mayApprove: boolean;
  /** Chain-level or regional, so no single property is implied. */
  chainWide: boolean;
}

interface AuthState {
  session: Session | null;
  loading: boolean;
  error: string | null;
  signIn: (email: string, password: string) => Promise<void>;
  signOut: () => void;
}

const APPROVER_GROUPS = ['Admin', 'Manager'];
const CHAIN_GROUPS = ['Admin', 'Manager', 'RevenueManager', 'RegionalManager'];

const AuthContext = createContext<AuthState | undefined>(undefined);

/** The current ID token, refreshed by Amplify when it is close to expiring.
 *  Read per request rather than held in state: the token lives an hour and a
 *  console left open over a shift would otherwise start silently 401ing. */
export async function idToken(): Promise<string | null> {
  try {
    const tokens = (await fetchAuthSession()).tokens;
    return tokens?.idToken?.toString() ?? null;
  } catch {
    return null;
  }
}

function sessionFrom(claims: Record<string, unknown>): Session {
  const raw = claims['cognito:groups'];
  const groups = Array.isArray(raw) ? raw.map(String) : typeof raw === 'string' ? raw.split(',') : [];
  return {
    email: String(claims.email ?? claims['cognito:username'] ?? 'unknown'),
    groups,
    propertyId: (claims['custom:property_id'] as string) || null,
    mayApprove: groups.some((g) => APPROVER_GROUPS.includes(g)),
    chainWide: !claims['custom:property_id'] && groups.some((g) => CHAIN_GROUPS.includes(g)),
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      await getCurrentUser();
      const payload = (await fetchAuthSession()).tokens?.idToken?.payload;
      setSession(payload ? sessionFrom(payload as Record<string, unknown>) : null);
    } catch {
      setSession(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const signIn = useCallback(
    async (email: string, password: string) => {
      setError(null);
      try {
        // A stale session from a previous user would make signIn throw rather than
        // replace it, which reads to an operator as "the password is wrong".
        await amplifySignOut().catch(() => undefined);
        const { nextStep } = await amplifySignIn({ username: email, password });
        if (nextStep.signInStep !== 'DONE') {
          // NEW_PASSWORD_REQUIRED and MFA are real pool states this console does not
          // implement. Saying so is better than looping on a form that will not
          // proceed.
          throw new Error(
            `This account needs ${nextStep.signInStep} before it can be used here. ` +
              'Complete it in the PMS console first.',
          );
        }
        await load();
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Sign-in failed.');
        throw err;
      }
    },
    [load],
  );

  const signOut = useCallback(() => {
    void amplifySignOut().finally(() => setSession(null));
  }, []);

  return (
    <AuthContext.Provider value={{ session, loading, error, signIn, signOut }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error('useAuth must be used inside AuthProvider');
  return context;
}
