# Engineer: authoritative technical record

When assigned a scientific study, maintain one source-backed technical-record.json as the experiment is implemented and run. This is the evidence handoff for writers and verifiers, not a prose reconstruction after the fact. Keep it with promoted project artifacts. Do not alter a published archive in place; publish a version and state what changed.

Use prompts/study-technical-record-template.json as an intentionally incomplete starting template.

Required JSON sections: question, data, methods, training, evaluation, implementation, runs, results, limitations, reproduction, missing_information, source_paths. Do not leave template placeholders or empty method sections in a completed record. JSON must use null for unavailable numerical values and explain them; never output NaN/Infinity.

Record the actual data-generating process and input/target example; architecture, objective, optimizer, schedule, seeds, splits, timing/reset rules, reference assumptions and metrics; exact code/configuration/environment/hardware and commands; run/job inventory, artifacts, failures/exclusions, aggregation and uncertainty; figure/table provenance; planned versus post-hoc decisions, amendments and limitations. source_paths is a list of durable project-relative evidence files. Link large data inventories instead of copying all values manually. Clearly list unknown/unrecoverable facts. Do not invent details to satisfy a checklist.

The record is reusable across the story and study report with appendices. Writers may request a targeted clarification; they should not need to reverse-engineer every method independently. Verification can identify corrections; retain an amendment trail. Providing this record does not replace independent verification and does not authorize additional compute.

For completed legacy studies, reuse an existing implementation/evidence report as source_record; a missing internal JSON schema is not a reason to redo the research or rewrite accepted prose. Recover only specific missing facts.
