# Incoming Slack message content

The Slack bridge reads structured blocks as well as the plain-text fallback. Pasted tables retain every row and cell, rich-text links retain their labels and URLs, and lists, quotes, code and attachment blocks are converted to readable text for the supervisor. Slack's generated “with interactive elements” placeholder is replaced by the actual content. Duplicate fallback paragraphs are omitted where they repeat structured content.

Uploaded files still use the existing attachment downloader, with its existing host and size checks. Rendering structured messages does not fetch embedded links or execute interactive controls. Sender allowlists, bot filtering, thread routing, exact commands and duplicate-event handling remain unchanged. Unsupported blocks produce an explicit marker rather than silently disappearing.

No additional Slack scopes are needed for table blocks included in message events. Historical messages are not automatically replayed; resend a previously missed table after the daemon has loaded the updated bridge.
