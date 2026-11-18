# Research digest reading view

Library opens on Research digests. Topic sections display paper titles and brief summaries directly on the page. Each paper has a source link and native expandable details for citation, motivation, methods, results, comparisons, limitations and read depth. Expanded paper details are retained across dashboard refreshes. Search matches topic names, paper entries and issue dates. All evidence, reports and the existing library filters remain available.

The current research-reading subscription uses four topics: Transformers, Non-stationary RL, Neuro-inspired AI and Continual learning. These are explicit topic labels supplied with each digest, not keywords guessed by the browser. Future topics can be added in the same structure. The original issue's eight featured entries have been organized using its cumulative reading list's topic tags; paper summaries and scientific claims are unchanged.

Supervisors publish optional `digest_record.sections`, each with `topic`, optional `summary`, and `papers`. Paper entries have `title`, `url`, `summary`, `details` (Markdown) and optional `canonical_id`. Keep issue-wide synthesis, search coverage and caveats in `text` without repeating the paper entries. The existing Markdown export includes the topic sections and all paper details. Older unstructured digests stay readable inline.

This presentation does not change reading schedules, scientific verification, or experiment grants, and requires no model calls to render or regroup saved material.
