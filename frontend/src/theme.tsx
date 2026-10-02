// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Theme state: night or day, remembered, and never flashing.
 *
 * Three requirements, and the third is the one that is easy to get wrong.
 *
 *   1. Remember the operator's choice. A night-shift user should not re-pick it daily.
 *   2. Respect the system preference when they have not chosen.
 *   3. Apply it *before first paint*. React mounts after the browser has already
 *      painted, so setting the attribute in an effect shows the wrong theme for one
 *      frame — a white flash on a console someone is using in a dark room. The
 *      inline script in index.html sets `data-theme` synchronously; this module reads
 *      back what that script decided rather than deciding again.
 */

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from 'react';

export type Theme = 'dark' | 'light';

const STORAGE_KEY = 'night-desk-theme';

interface ThemeState {
  theme: Theme;
  toggle: () => void;
}

const ThemeContext = createContext<ThemeState | undefined>(undefined);

/** Whatever the boot script already applied. Falls back for tests and SSR. */
function current(): Theme {
  const applied = document.documentElement.dataset.theme;
  return applied === 'light' ? 'light' : 'dark';
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>(current);

  const apply = useCallback((next: Theme) => {
    // `dark` is the default in :root, so the attribute is only present for light —
    // which keeps the CSS to one override block instead of two.
    if (next === 'light') document.documentElement.dataset.theme = 'light';
    else delete document.documentElement.dataset.theme;

    // Keeps form controls, scrollbars and the browser chrome in step with the page.
    document
      .querySelector('meta[name="color-scheme"]')
      ?.setAttribute('content', next === 'light' ? 'light' : 'dark');
    document
      .querySelector('meta[name="theme-color"]')
      ?.setAttribute('content', next === 'light' ? '#F7F3EC' : '#0F0D0B');

    try {
      localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Private browsing. The theme still applies for this session.
    }
    setTheme(next);
  }, []);

  const toggle = useCallback(
    () => apply(current() === 'light' ? 'dark' : 'light'),
    [apply],
  );

  // Follow the system only while the operator has made no explicit choice.
  useEffect(() => {
    let stored: string | null = null;
    try {
      stored = localStorage.getItem(STORAGE_KEY);
    } catch {
      /* ignore */
    }
    if (stored) return;

    const query = window.matchMedia('(prefers-color-scheme: light)');
    const follow = (event: MediaQueryListEvent | MediaQueryList) => {
      if (event.matches) document.documentElement.dataset.theme = 'light';
      else delete document.documentElement.dataset.theme;
      setTheme(event.matches ? 'light' : 'dark');
    };
    query.addEventListener('change', follow);
    return () => query.removeEventListener('change', follow);
  }, []);

  return <ThemeContext.Provider value={{ theme, toggle }}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeState {
  const context = useContext(ThemeContext);
  if (!context) throw new Error('useTheme must be used inside ThemeProvider');
  return context;
}
