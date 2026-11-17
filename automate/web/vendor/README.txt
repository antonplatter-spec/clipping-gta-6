Third-party files used by the dashboard (no build step, no CDN at runtime).

preact.js        Preact 11.2.0 (dist/preact.mjs), MIT, LICENSE-preact.txt
preact-hooks.js  Preact 11.2.0 hooks (hooks/dist/hooks.mjs), MIT, LICENSE-preact.txt
                 Changed: the bare import "preact" now reads "./preact.js" (no import map needed).
htm.js           htm 3.1.1 (dist/htm.mjs), Apache-2.0, LICENSE-htm.txt
lucide-icons.js  Subset of the Lucide icons (lucide-static 1.74.0, icon-nodes.json), ISC, LICENSE-lucide.txt
                 Generated: { name: [[tag, attrs], ...] } for the icons the dashboard uses.

Source-map comments were removed (the .map files are not shipped). Fetched with `npm pack <package>`.
