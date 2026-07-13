# Moth Radio README Design

## Goal

Replace the existing README with an approachable, accurate entry point for
Moth Radio, the local-first artist discovery tool packaged as `scrobble-recs`.

## Reader flow

1. **Overview** explains what Moth Radio does, its local-first boundary, and
   what a listener needs to provide.
2. **Algorithms** explains the recommendation pipeline in plain language:
   bounded listening history, direct graph evidence, calibrated relevance,
   serendipity reranking, feedback controls, and temporal evaluation.
3. **Quick start** gives the shortest useful path from installation to a
   persisted recommendation run.
4. **Operational commands** reveals deeper workflows progressively: profile
   inspection, enrichment, exports, the TUI, feedback, evaluation, and cache
   maintenance.
5. **Development** retains only the essential contribution constraints and
   verification commands.

## Content rules

- Use the name **Moth Radio** as the product name; explain once that the
  Python package and CLI are named `scrobble-recs`.
- Keep examples local and copyable; never suggest a live API call is required
  for imports, persisted runs, exports, the TUI, feedback, or evaluation.
- Keep advanced configuration and operational detail after the quick start.
- Describe the 12-month profile window as the configurable default, not an
  invariant.
- Do not claim behavior that is not implemented.

## Validation

- Check command examples and terminology against the CLI/config source.
- Run Markdown-oriented formatting checks if configured; otherwise inspect the
  rendered hierarchy and run the project test/lint/type commands because the
  README is part of the package metadata.
