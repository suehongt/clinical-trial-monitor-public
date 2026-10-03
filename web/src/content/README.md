# Help content maintenance

The in-app `/help` page reads its content from two JSON files:

- `user-guide.json` — user guide sections and optional internal links.
- `release-notes.json` — user-facing release notes, newest release first.

Both files use `schemaVersion: 1` and bilingual `{ "zh": "…", "en": "…" }`
text fields. Keep `updatedAt` current whenever content changes. For a new release,
prepend one object to `releases`, change the former current release to
`"status": "previous"`, and keep exactly one `"status": "current"` entry.

Run `npm test` from `web/` after editing. The help-content contract test checks
the schema, bilingual text, safe internal links, unique identifiers, and
newest-first release ordering.
