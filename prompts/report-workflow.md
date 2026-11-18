# Current report and Research story standard

This standard supersedes earlier narrative, single-PDF and automatic editorial-review instructions.

Research story is only a fixed table generated from the study registry. Each study has a title, a one- or two-sentence purpose stored in question, paper_pdf and detailed_pdf. The supervisor maintains those fields through update_study. Do not write a long narrative, add plot galleries, create story_section.md, polish the page or run an editorial review loop. Update only the affected row when its purpose or PDF paths change. A table update does not authorize report production.

Each study retains TWO separate PDFs:
- Paper-section PDF: standalone, readable scientific explanation for humans. Explain the question, method, concrete data/input/target example, experiment/comparisons, main results/figures, conclusions and limitations. Define unfamiliar metrics. Use LaTeX and existing sources when useful.
- Detailed-report PDF: reproducibility and technical detail, including code/configuration/environment, exact commands, run inventory, extra results, evidence mappings, exclusions and provenance gaps. Generate repetitive tables and inventories with local code.

The supervisor assigns an appropriate specialist to write or revise the specific needed PDF. Reuse accepted reports and editable sources. Do not regenerate existing good PDFs merely because the table changed. Never mass-rewrite other studies, transcribe inventories with a model, add duplicate reviewers or rerun research during documentation work. Retired assignments stay retired unless the human explicitly reauthorizes their scope.

Documentation assignments use work_kind=documentation, study_id, document_phase=draft/revise and document_kind=paper or detailed. Reuse the existing source_record or structured technical_record as factual evidence. A missing/conflicting fact calls for one targeted clarification. Save incremental drafts/checkpoints. Keep the existing no-helper and model-usage safeguards; no special five-minute writing deadline applies.

Register exact PDF paths via update_study. Table rows can exist while PDFs are unavailable; never invent links or claim a draft has been verified. The source/number/QA checks and scientific verification still apply to the reports themselves. Figures belong inside PDFs; no figure embeds or narrative draft are required for the table. Format changes require an explicit new user instruction. Ordinary Slack research discussion changes content, not the table layout or this report standard.

The lab remains paused after the user's stop request. This format update, a Slack acknowledgement, login renewal or allowance reset is not permission to resume model work.

Keep one current row per study, with a Status column. Update the same row and replace its links when reports change; add a row only for a new study. Do not display older versions. Set execution for study progress and status_note for a concise explanation of report work, pending review or a blocker. Be accurate: queued or paused writing is not active writing. Historical files are not automatically deleted.

Per-task and documentation-phase output-token limits have been removed. Do not stop, split, or replace work due to an old token allowance. Output usage is still recorded; actual provider limits and the other current scheduling/resource settings still apply.
