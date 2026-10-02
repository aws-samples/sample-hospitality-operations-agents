// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The lighting switch.
 *
 * Labelled with the theme you would be *switching to*, not the one you are in — a
 * moon icon while already dark is a small riddle, and an operations tool should not
 * pose any.
 */

import { Moon, Sun } from 'lucide-react';
import { useTheme } from '../theme';

export function ThemeToggle({ compact = false }: { compact?: boolean }) {
  const { theme, toggle } = useTheme();
  const target = theme === 'dark' ? 'day' : 'night';
  const Icon = theme === 'dark' ? Sun : Moon;

  return (
    <button
      onClick={toggle}
      title={`Switch to ${target}`}
      aria-label={`Switch to ${target} theme`}
      className="flex items-center gap-1.5 font-mono text-2xs uppercase tracking-[0.1em] text-bone-faint transition-colors hover:text-ember"
    >
      <Icon className="h-3 w-3" />
      {!compact && target}
    </button>
  );
}
