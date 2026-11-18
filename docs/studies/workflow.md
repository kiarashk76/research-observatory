# Research story and study reports

Research story is a fixed table: **Study | Brief summary | Paper section PDF | Detailed report PDF**. It is generated from the study registry; agents cannot replace the layout with Markdown. The supervisor updates a study title, question/purpose and PDF paths through update_study. A row update is a metadata change, not permission to create or rewrite reports.

A study is a scientific question. An assignment is an agent work order. An experiment job is a server process running code. One study can involve several assignments and experiment jobs. Registering a study does not dispatch any of them.

Each study has two separate reports. The paper-section PDF is a standalone scientific explanation for humans. The detailed-report PDF gives implementation/configuration, complete known evidence and reproduction details. Reuse existing LaTeX and accepted artifacts. The supervisor assigns an appropriate specialist to a specific missing/changed report; the page's brief summary is the supervisor's responsibility. Missing PDFs display Not available yet. Existing PDFs are linked immediately, without implying new verification.

The current prompts/report-workflow.md is automatically included in supervisor and documentation prompts. It supersedes earlier living-paper, single-PDF and editorial-cycle requirements. Ordinary Slack discussions affect research content, not this layout. Explicit user instruction is required to change the standard.

Update rows with update_study using study_id and study_record fields title, question, paper_pdf and detailed_pdf. Existing study execution/verification metadata stays separate. No narrative story_draft, figure gallery or Markdown link check is required for table publication. Reports themselves still need source evidence, numerical/QA checks and supervisor review to be marked complete. Scientific verified status still requires an accepted passing verifier. Link availability alone does not establish correctness.

The platform generates notes/research-story.md from these rows and preserves earlier narrative versions. Direct write_note attempts at that generated path are rejected. No model creates the table HTML, no Codex editorial cycle runs for it, and old feedback is omitted from agent story context. Updating a row does not start model sessions.

Documentation keeps its existing controls: one selected study at a time, one assigned PDF, artifact/checkpoint continuation, no helpers, configured model/session safeguards and the normal platform watchdog. There is no special five-minute or fifteen-minute documentation time cap. Use document_kind=paper or detailed. The fixed-table change does not unpause the lab, requeue retired assignments or regenerate accepted PDFs.

Keep one current row per study, with a Status column. Update the same row and replace its links when reports change; add a row only for a new study. Do not display older versions. Set execution for study progress and status_note for a concise explanation of report work, pending review or a blocker. Be accurate: queued or paused writing is not active writing. Historical files are not automatically deleted.

Per-task and documentation-phase output-token limits have been removed. Do not stop, split, or replace work due to an old token allowance. Output usage is still recorded; actual provider limits and the other current scheduling/resource settings still apply.
