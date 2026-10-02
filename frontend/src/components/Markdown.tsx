// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Agent output, rendered as what it actually is.
 *
 * Every agent in this system replies in markdown — A1 reports assignments as a table,
 * A4 numbers its four audit categories, A5 emits ranked tables with bold figures. The
 * first version of this console rendered all of it inside `whitespace-pre-wrap`, so a
 * table arrived as a wall of pipe characters and `**Not assigned (7)**` arrived with
 * the asterisks showing. It looked broken because it was.
 *
 * GFM is on for tables specifically. Styling lives in `.prose-desk` in index.css
 * rather than in a plugin, so the output reads as part of the panel instead of as an
 * article pasted into it.
 */

import { memo } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

export const Markdown = memo(function Markdown({ children }: { children: string }) {
  return (
    <div className="prose-desk">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          // Long uuids in a table cell would otherwise force horizontal scroll on the
          // whole answer. Wrapping them is worth the slight ugliness.
          // Props are named rather than spread, so nothing the model wrote reaches the
          // DOM except what is listed here.
          code: ({ children }) => <code className="break-all">{children}</code>,
          // Agents are told never to link anywhere, but if one ever does, it does not
          // get to navigate this tab. `href` is the model's text, and is safe only
          // because react-markdown's default urlTransform drops any protocol but
          // http(s), mailto, irc and xmpp -- so `javascript:` never arrives here. Do
          // not pass a custom urlTransform without keeping that property.
          a: ({ children, href, title }) => (
            <a href={href} title={title} target="_blank" rel="noreferrer noopener">
              {children}
            </a>
          ),
          table: ({ children }) => (
            <div className="-mx-1 overflow-x-auto px-1">
              <table>{children}</table>
            </div>
          ),
        }}
      >
        {children}
      </ReactMarkdown>
    </div>
  );
});
