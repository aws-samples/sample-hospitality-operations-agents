// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The property selector in the status strip.
 *
 * It replaced a free-text field that expected a UUID, which failed in the way you would
 * expect: the decision log contains both `cc097a19-50ac-…` and `c097a19-50ac-…` — the
 * same hotel, one dropped character, one run that executed against nothing.
 *
 * Filterable rather than a `<select>`, for a specific reason. Every property in this
 * chain is named `AnyCompany Bay <something>`, and a native select's type-ahead matches
 * only the *start* of an option's label — so typing "austin" in a dropdown of fifty
 * options that all begin with the same two words finds nothing at all. The filter here
 * matches name, city and state anywhere in the string, which is how someone actually
 * remembers a property.
 *
 * What it does not do is decide who may see what. The list arrives already scoped by the
 * platform from the operator's own token; a housekeeper's request returns exactly one
 * row. See `infra/lambdas/console/properties/index.py`.
 */

import { Building2, Check, ChevronsUpDown, Loader2, Search, X } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { Property } from '../api';

/** Survives a reload so an operator who works one hotel does not re-pick it every
 *  morning. Keyed per signed-in user: a shared browser must not carry one person's
 *  scope into another's session. */
const storageKey = (email: string) => `night-desk-property:${email}`;

export function rememberedProperty(email: string, available: Property[]): string {
  try {
    const saved = localStorage.getItem(storageKey(email));
    if (saved && available.some((p) => p.propertyId === saved)) return saved;
  } catch {
    /* private mode, or storage disabled */
  }
  return '';
}

function remember(email: string, propertyId: string) {
  try {
    if (propertyId) localStorage.setItem(storageKey(email), propertyId);
    else localStorage.removeItem(storageKey(email));
  } catch {
    /* ignore */
  }
}

const place = (p: Property) => [p.city, p.state].filter(Boolean).join(' ');

export function PropertyPicker({
  properties,
  loading,
  error,
  value,
  onChange,
  email,
  boundToOneProperty,
}: {
  properties: Property[];
  loading: boolean;
  error: string | null;
  value: string;
  onChange: (propertyId: string) => void;
  email: string;
  boundToOneProperty: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState('');
  const box = useRef<HTMLDivElement>(null);
  const search = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!open) return;
    search.current?.focus();
    const away = (e: MouseEvent) => {
      if (!box.current?.contains(e.target as Node)) setOpen(false);
    };
    const escape = (e: KeyboardEvent) => e.key === 'Escape' && setOpen(false);
    document.addEventListener('mousedown', away);
    document.addEventListener('keydown', escape);
    return () => {
      document.removeEventListener('mousedown', away);
      document.removeEventListener('keydown', escape);
    };
  }, [open]);

  const matches = useMemo(() => {
    const q = filter.trim().toLowerCase();
    if (!q) return properties;
    return properties.filter((p) =>
      `${p.name} ${place(p)}`.toLowerCase().includes(q),
    );
  }, [properties, filter]);

  const selected = properties.find((p) => p.propertyId === value) ?? null;

  const choose = (propertyId: string) => {
    onChange(propertyId);
    remember(email, propertyId);
    setOpen(false);
    setFilter('');
  };

  if (loading) {
    return (
      <span className="flex items-center gap-2 font-mono text-2xs text-bone-faint">
        <Loader2 className="h-3 w-3 animate-spin" /> loading properties
      </span>
    );
  }

  // A property-scoped user has nothing to choose. Show them the hotel's name — which
  // is the other half of what this endpoint is for, and what they could never see when
  // the field held a raw UUID.
  if (boundToOneProperty || properties.length === 1) {
    const only = selected ?? properties[0];
    return (
      <span className="flex items-center gap-2 font-mono text-2xs text-bone">
        <Building2 className="h-3 w-3 text-bone-faint" />
        {only ? (
          <>
            {only.name}
            {place(only) && <span className="text-bone-faint">· {place(only)}</span>}
          </>
        ) : (
          <span className="text-clay">no property access</span>
        )}
      </span>
    );
  }

  if (error) {
    return (
      <span className="flex items-center gap-2 font-mono text-2xs text-clay" title={error}>
        <X className="h-3 w-3" /> property list unavailable
      </span>
    );
  }

  return (
    <div ref={box} className="relative">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-[330px] items-center gap-2 border border-line bg-ink px-2 py-1 text-left font-mono text-2xs text-bone transition-colors hover:border-line-bright"
      >
        <Building2 className="h-3 w-3 shrink-0 text-bone-faint" />
        <span className="truncate">
          {selected ? (
            <>
              {selected.name}
              {place(selected) && (
                <span className="text-bone-faint"> · {place(selected)}</span>
              )}
            </>
          ) : (
            <span className="text-dusk">All properties · chain-wide</span>
          )}
        </span>
        <ChevronsUpDown className="ml-auto h-3 w-3 shrink-0 text-bone-faint" />
      </button>

      {open && (
        <div className="absolute left-0 top-[calc(100%+4px)] z-30 w-[330px] border border-line-bright bg-ink-2 shadow-xl">
          <div className="flex items-center gap-2 border-b border-line px-2 py-1.5">
            <Search className="h-3 w-3 shrink-0 text-bone-faint" />
            <input
              ref={search}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              onKeyDown={(e) => {
                // Enter picks the only remaining match, so "aus" + Enter is the
                // whole interaction.
                if (e.key === 'Enter' && matches.length === 1) choose(matches[0].propertyId);
              }}
              placeholder="filter by name, city or state…"
              className="w-full bg-transparent font-mono text-2xs text-bone placeholder-bone-faint outline-none"
            />
          </div>

          <div className="max-h-[320px] overflow-y-auto">
            {/* Chain-wide first, and always visible: it is the default, and hiding it
                behind a filter that does not match it would be a trap. */}
            <button
              type="button"
              onClick={() => choose('')}
              className="flex w-full items-center gap-2 border-b border-line px-2.5 py-2 text-left font-mono text-2xs transition-colors hover:bg-ink-3"
            >
              <Check
                className={`h-3 w-3 shrink-0 ${value ? 'opacity-0' : 'text-ember'}`}
              />
              <span className={value ? 'text-bone-dim' : 'text-ember'}>
                All properties · chain-wide
              </span>
            </button>

            {matches.length === 0 ? (
              <p className="px-2.5 py-3 font-mono text-2xs text-bone-faint">
                Nothing matches “{filter}”.
              </p>
            ) : (
              matches.map((p) => {
                const active = p.propertyId === value;
                return (
                  <button
                    key={p.propertyId}
                    type="button"
                    onClick={() => choose(p.propertyId)}
                    title={p.propertyId}
                    className="flex w-full items-start gap-2 px-2.5 py-2 text-left transition-colors hover:bg-ink-3"
                  >
                    <Check
                      className={`mt-px h-3 w-3 shrink-0 ${active ? 'text-ember' : 'opacity-0'}`}
                    />
                    <span className="min-w-0">
                      <span
                        className={`block truncate text-[12px] ${active ? 'text-ember' : 'text-bone'}`}
                      >
                        {p.name}
                      </span>
                      {place(p) && (
                        <span className="block font-mono text-2xs text-bone-faint">
                          {place(p)}
                        </span>
                      )}
                    </span>
                  </button>
                );
              })
            )}
          </div>

          <p className="border-t border-line px-2.5 py-1.5 font-mono text-2xs text-bone-faint">
            {properties.length} properties you may act on
          </p>
        </div>
      )}
    </div>
  );
}
