// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * "Night desk" and "day desk" — one instrument panel, two lighting conditions.
 *
 * Every colour resolves through a CSS custom property so a single set of class names
 * works in both themes: `text-bone-faint` is a warm mid-grey on ink at night and a warm
 * dark grey on paper by day. The alternative — `dark:` variants on every element — puts
 * the theme decision in a thousand places and guarantees one of them is wrong.
 *
 * The triplet-and-`<alpha-value>` form matters: it keeps Tailwind's opacity modifiers
 * working, so `border-line/70` still composes. Without it every `/50` in the codebase
 * would silently stop applying.
 *
 * Contrast is measured, not eyeballed. Every foreground/background pair in both themes
 * clears WCAG AA for normal text (4.5:1); the tightest is 5.30:1. The first version of
 * this palette had `bone-faint` at **3.23:1** on ink and used it for every label,
 * timestamp and metadata line at 11px uppercase — which is exactly the "barely visible"
 * text, and exactly the worst case for a low ratio.
 *
 * Four semantic accents, each meaning one thing everywhere, and each with three roles
 * because the right hex differs by role *and* by theme:
 *
 *   ember  a write happened, or a human is required
 *   sage   a read succeeded, or a verdict endorsed
 *   clay   a refusal, an error, a disagreement
 *   dusk   an agent — never a state, only an actor
 *
 * `DEFAULT` is the text-safe value, `line` is for borders, `wash` for fills. On light
 * backgrounds the text-safe accents are much darker than their dark-mode counterparts:
 * `#E9A94F` reads beautifully on ink and is 1.9:1 on paper.
 */
/** @type {import('tailwindcss').Config} */
const token = (name) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: {
          DEFAULT: token('bg'),
          2: token('surface'),
          3: token('surface-2'),
          4: token('surface-3'),
        },
        line: { DEFAULT: token('border'), bright: token('border-strong') },
        bone: {
          DEFAULT: token('text'),
          dim: token('text-dim'),
          faint: token('text-faint'),
        },
        ember: {
          DEFAULT: token('ember'),
          deep: token('ember-line'),
          wash: token('ember-wash'),
        },
        sage: {
          DEFAULT: token('sage'),
          deep: token('sage-line'),
          wash: token('sage-wash'),
        },
        clay: {
          DEFAULT: token('clay'),
          deep: token('clay-line'),
          wash: token('clay-wash'),
        },
        dusk: {
          DEFAULT: token('dusk'),
          deep: token('dusk-line'),
          wash: token('dusk-wash'),
        },
      },
      fontFamily: {
        display: ['Fraunces', 'Georgia', 'serif'],
        sans: ['"Instrument Sans"', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'monospace'],
      },
      fontSize: {
        // 11.5px rather than 11: the smallest type in the app carries labels and
        // timestamps, and half a pixel of size buys more legibility than any amount
        // of colour tuning at this scale.
        '2xs': ['0.71875rem', { lineHeight: '1.05rem', letterSpacing: '0.035em' }],
      },
      boxShadow: {
        lamp: 'var(--lamp-shadow)',
        node: '0 0 0 3px rgb(var(--surface))',
      },
      animation: {
        rise: 'rise 0.5s cubic-bezier(0.16, 1, 0.3, 1) both',
        'slide-in': 'slide-in 0.35s cubic-bezier(0.16, 1, 0.3, 1) both',
        breathe: 'breathe 2.4s ease-in-out infinite',
        sweep: 'sweep 2.2s linear infinite',
      },
      keyframes: {
        rise: {
          from: { opacity: '0', transform: 'translateY(8px)' },
          to: { opacity: '1', transform: 'none' },
        },
        'slide-in': {
          from: { opacity: '0', transform: 'translateX(-6px)' },
          to: { opacity: '1', transform: 'none' },
        },
        breathe: { '0%,100%': { opacity: '1' }, '50%': { opacity: '0.35' } },
        sweep: { from: { transform: 'translateX(-100%)' }, to: { transform: 'translateX(400%)' } },
      },
    },
  },
  plugins: [],
};
