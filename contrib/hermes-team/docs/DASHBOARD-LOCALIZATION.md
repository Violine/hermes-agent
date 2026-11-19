# Russian dashboard and unified theme

This work adds Russian localization for the Hermes dashboard and its bundled Kanban and achievements interfaces, plus a standalone readable theme and the Hermes Team panel. The patch set was prepared against Hermes **0.20.6**, base revision `5cc1369fa298021f8c740de154ff8c37c30bdcc8`. That revision is a compatibility reference, not a promise that the patch applies to current upstream.

## Source files

- `dashboard/patches/hermes-0.20.6-russian-ui.patch` updates React sources, translation dictionaries, and bundled plugin assets through Hermes' existing i18n hooks. It does not rewrite content in the DOM.
- `dashboard/themes/team-readable.yaml` provides a standalone theme with system sans-serif text, monospaced code, consistent sizing, and Kanban scrolling styles.
- `dashboard/index.js`, `dashboard/style.css`, and `dashboard/kanban/` are the Team panel and board UI source assets.
- `dashboard/patches/hermes-0.20.6-kanban-usability.patch` and `hermes-0.20.6-folder-picker.patch` are upstream source patches for the native Kanban surface and folder chooser.

Model identifiers, API payloads, file paths, user content, and terminal output remain unchanged by localization. Some third-party catalog descriptions and external documentation may still be in their original language.

## Build and publish

From a compatible Hermes checkout, first run `git apply --check` for each patch. Resolve any mismatch against the exact source; do not ignore conflicts or force-apply an old patch. Build the dashboard with `npm run build` from its `web/` package. The upstream Vite build emits into Hermes' configured `hermes_cli/web_dist` directory. Run the source and handwritten plugin tests before publishing.

For an installation, first make and verify a target-specific backup of the editable source and generated assets. Copy new hashed assets before replacing `index.html`, then reload the browser. The team plugin assets and theme are installed through the corresponding Hermes plugin/theme directories. These paths and service/container assumptions must be read from the target installation rather than copied from another host.

The theme and plugin assets can usually be updated without stopping workers, but backend or plugin lifecycle code may require a controlled service restart. Check the target's actual process model before publishing.

Historical test scope and its limits are recorded in [VALIDATION.md](VALIDATION.md). See [Kanban usability](KANBAN-USABILITY.md) for user-visible status behavior.
