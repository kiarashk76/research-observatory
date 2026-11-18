# Workspace research chat

Research chat is an on-demand Claude CLI discussion partner, separate from the Slack supervisor. It cannot assign work, launch jobs, edit research notes, or alter reports. No model is started by opening the page, polling a conversation, changing workspaces, or saving memory. Only Send starts a session. Stop reply cancels it. Failures are saved without automatic retries.

Each workspace owns its conversations and an editable memory document. Select Auto, primary, secondary or tertiary for subscription billing, independently of model/effort. Account switching resumes the same archived CLI conversation with current workspace memory. The three accounts are credentials/capacity choices, not separate personalities. ChatGPT/Codex is not involved. Settings live under `research_chat` in config.yaml; the existing Claude accounts remain unchanged.

The chat can search linked library papers, digests, study records, project reports, plots and saved Research Notes. Read-only MCP tools provide scoped evidence: search_evidence, read_evidence (including PDF pages and text offsets), and view_figure (images and rendered PDF pages). Source IDs are resolved against the selected workspace; paths are never accepted from the model. A fourth MCP tool, propose_notes_edit, may submit a pending Research Notes change only within its active conversation/workspace. It cannot accept changes or consume supervisor edit permissions. Read notes first for revision/section identifiers; the human reviews and accepts/rejects proposals in the existing Research Notes sidebar. Stale revisions are rejected. No shell, editor, native file tools, skills, hooks, other MCP servers or subagents are offered. Original files are not writable through chat tools. Answers should cite evidence and distinguish reproduction results from unverified claims. Complete literature records and registered full-text sources in the publishing session are included for linked papers, along with library PDFs when available locally; external URLs are references, not automatically fetched papers.

The Discuss with Claude button in the notes toolbar opens chat. If desired, check Include the notes draft copied from the editor before sending. Saved notes are available without that checkbox. Unsaved notes are not silently submitted. The existing editor recovery draft remains intact.

Memory additions are generated in the same reply, without a second model call, and only applied if no human/concurrent memory edit occurred since the reply began. Memory is visible/editable in the side panel, scoped to its workspace, shared across that workspace's conversations/accounts, and separate from supervisor memory. It should capture durable human preferences and decisions, not replace evidence. Saving an empty memory document clears it. Unsaved memory and message drafts stay in that browser.

Canonical conversations, messages and memory are records in runtime/lab.sqlite3; private CLI archives live under runtime/research-chat/{project}/{conversation}. They are excluded from the evidence catalog. Restart interruptions preserve messages and do not trigger retries. The browser authenticates with the same protected dashboard credentials; all chat read/write HTTP routes require that authentication, even on loopback. Existing Host/Origin checks apply.

PDF rendering uses the workspace-local pypdfium2 build (5.15.2) under runtime/research-chat-deps, installed with:

```
python -m pip install --target runtime/research-chat-deps pypdfium2==5.15.2
```

The editor bundle includes the chat UI. Rebuild with `runtime/tools/node/bin/node frontend/research-notes/build.mjs`. Unit tests cover workspace isolation, symlink/path authorization, memory conflicts, HTTP authentication, interruption recovery and duplicate message submission. Browser tests use an offline CLI fixture to check account switching, workspace memory, cancellation, drafts and mobile layout without consuming subscriptions.


The floating chat bubble is available throughout the dashboard except the full Research chat tab, where the conversation is already visible. Opening it reveals a non-modal side panel without navigating away from Library, Research Story or Notes. It reuses the same chat surface as the full tab: conversation selection, account/model/effort controls, draft and memory remain intact. The panel header names the workspace and provides memory, expand and minimize controls. Escape from inside the panel minimizes it. Minimizing or navigating does not cancel a server-side reply; use Stop reply for cancellation. A minimized panel does not poll for updates. Opening, expanding or minimizing never submits a message. Workspace switching clears the old surface and binds the panel to the new workspace before loading its records.

Long replies scroll within the panel while the composer stays in its own bounded region. The chat follows new messages when at the bottom or after sending. Scrolling up preserves the reading position; Latest messages returns to the bottom. This also works when reopening the minimized panel.


Live replies use the Claude CLI's partial JSONL events and an authenticated, workspace-scoped event stream to the browser. The UI shows activity (thinking, evidence searches, reading source titles/pages, viewing figures, preparing proposals) and decodes the reply from StructuredOutput incrementally. Raw JSON, private thinking text, credentials and tool-returned document contents are not shown as activity. This adds no model calls. If the live connection drops, normal status polling reconnects without restarting inference. Stop keeps an already-generated partial reply marked interrupted.

Enter sends; Shift+Enter inserts a newline, with IME composition respected. The trash button deletes the selected stopped/idle conversation and its messages from dashboard history, removing its private application archive. Shared workspace memory, evidence, reports and notes are retained. A running reply must be stopped first. The underlying Claude CLI may retain its own session files outside the dashboard archive. Both chat surfaces use the same live renderer and controls.

The bubble has a single compact conversation row with icon buttons. Account/model/effort settings live in a popover opened from the small settings summary beside Send; they do not consume message space while closed. Escape or an outside click closes the popover. The chat toolbar omits the redundant Research Notes shortcut; use the dashboard navigation to open notes. The full tab also uses a compact settings summary beside Send; opening the popover leaves the reading area unchanged.

The full Research chat tab provides Full screen and Hide/Show memory controls beside its title. Full screen expands the existing conversation without restarting it; Escape exits. Memory visibility is remembered per workspace in the browser. Navigating away or switching workspaces clears full-screen styling and restores page scrolling. These layout actions never submit a message or change memory.

The Research chat page fills the available viewport below its heading and the dashboard header. Conversation messages take the flexible height, with the composer at the bottom. Taller windows expand the reading area without a fixed pixel cap. Other dashboard pages keep their normal scrolling layout.

## Figures and small plotting edits

`show_figure` attaches an existing image or rendered PDF page to the conversation, with its source link. The bubble and full tab display the same figure attachments, including during live responses. Clicking a figure opens the larger image; download links retain the original generated file.

`run_plot` accepts explicit workspace evidence IDs and a Python script. `INPUTS` maps IDs to staged copies; `OUTPUT_DIR` is the writable output folder. It can regenerate plots, adjust labels/legends/colors/fonts, or format tables from available source data. Original files are never writable. Generated PNG/JPEG previews, PDFs/data files and the generating script are stored as derivative workspace artifacts, with source IDs and SHA-256 input hashes. Newly generated artifacts can be read or used as inputs in subsequent tool calls. To make a clean plotting change, use the original code/data; do not fabricate numeric results from an image.

Plot execution runs under Landlock and a seccomp filter: no network, process spawning, GPU access, credentials or writes outside its scratch/output folders. The process is CPU-only, single-threaded, capped at 30 CPU seconds/60 wall seconds and 2 GB address space. Inputs are limited to 16 sources of at most 20 MB each; output is limited to 12 supported files/64 MB. A parent-death signal prevents orphan plotting processes. Larger computations belong to engineer assignments. This tool is only for explicit user-requested small plotting/data-formatting work, never background research.

Figure/data/script downloads require dashboard authentication and matching workspace ownership. Generated research outputs remain workspace evidence if their chat is deleted; deleting a conversation removes discussion history, not research artifacts. Notes edits still require a separate reviewed proposal.
