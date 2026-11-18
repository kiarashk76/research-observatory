"""Read Slack's structured message content without fetching or executing links."""
import html
import re


def _text(value):
    return html.unescape(value) if isinstance(value, str) else ''


def _children(value):
    return value if isinstance(value, list) else []


def render(node, depth=0):
    if depth > 32:
        return '[Slack content nesting exceeds supported depth]'
    if isinstance(node, list):
        return '\n'.join(filter(None, (render(item, depth+1) for item in node)))
    if not isinstance(node, dict):
        return _text(node)
    kind = node.get('type', '')
    children = _children(node.get('elements'))
    child = lambda value: render(value, depth+1)
    if kind == 'table':
        rows = []
        for row in _children(node.get('rows')):
            if not isinstance(row, list):
                rows.append('[Unsupported Slack table row]')
                continue
            cells = [child(cell).replace('|', r'\|').replace('\r\n', '\n').replace('\n', '<br>') for cell in row]
            rows.append('| ' + ' | '.join(cells) + ' |')
        return '[Slack table]\n' + '\n'.join(rows) if rows else '[Empty Slack table]'
    if kind == 'raw_number':
        value = node.get('value', node.get('text'))
        return str(value) if isinstance(value, (int, float, str)) and not isinstance(value, bool) else ''
    if kind in ('raw_text', 'text', 'plain_text', 'mrkdwn', 'markdown'):
        return _text(node.get('text'))
    if kind in ('link', 'button'):
        url = _text(node.get('url'))
        label = child(node.get('text'))
        return f'{label} ({url})' if label and url and label != url else label or url
    if kind == 'user':
        return '<@' + _text(node.get('user_id')) + '>'
    if kind == 'channel':
        return '<#' + _text(node.get('channel_id')) + '>'
    if kind == 'usergroup':
        return '<!subteam^' + _text(node.get('usergroup_id')) + '>'
    if kind == 'broadcast':
        return '<!' + _text(node.get('range')) + '>'
    if kind == 'emoji':
        return ':' + _text(node.get('name')) + ':'
    if kind == 'date':
        return _text(node.get('fallback')) or str(node.get('timestamp', ''))
    if kind == 'rich_text_section':
        return ''.join(child(item) for item in children)
    if kind == 'rich_text_list':
        offset = node.get('offset', 0)
        offset = offset if isinstance(offset, int) and not isinstance(offset, bool) else 0
        indent = node.get('indent', 0)
        indent = indent if isinstance(indent, int) and not isinstance(indent, bool) else 0
        return '\n'.join('  '*min(max(indent, 0), 20) + (str(offset+i+1)+'. ' if node.get('style') == 'ordered' else '- ') + child(item)
                         for i,item in enumerate(children))
    if kind == 'rich_text_preformatted':
        return '```\n' + ''.join(child(item) for item in children) + '\n```'
    if kind == 'rich_text_quote':
        return '\n'.join('> ' + line for line in ''.join(child(item) for item in children).splitlines())
    if kind in ('rich_text', 'context', 'actions'):
        return '\n'.join(filter(None, (child(item) for item in children)))
    if kind == 'image':
        return _text(node.get('alt_text')) or '[Slack image; no text description]'
    if kind == 'divider':
        return '---'
    parts = [child(node.get('text'))]
    parts += [child(field) for field in _children(node.get('fields'))]
    parts += [child(item) for item in _children(node.get('blocks')) + children]
    if node.get('accessory'):
        parts.append(child(node['accessory']))
    result = '\n'.join(filter(None, parts))
    return result or ('[Unsupported Slack block: '+str(kind)+']' if kind else '')


def attachment_text(attachment):
    if not isinstance(attachment, dict):
        return ''
    if attachment.get('type') in ('table', 'rich_text'):
        return render(attachment)
    title = _text(attachment.get('title'))
    url = _text(attachment.get('title_link'))
    if url:
        title = f'{title} ({url})' if title and title != url else url
    parts = [_text(attachment.get('pretext')), title, _text(attachment.get('text'))]
    for field in _children(attachment.get('fields')):
        if isinstance(field, dict):
            parts.append(': '.join(filter(None, (_text(field.get('title')), _text(field.get('value'))))))
    parts.append(render(attachment.get('blocks')))
    return '\n\n'.join(filter(None, parts)) or _text(attachment.get('fallback'))


def incoming_text(event):
    """Prefer actual blocks; keep fallback-only text and attachment content too."""
    fallback = _text(event.get('text')).strip()
    blocks = render(event.get('blocks')).strip()
    attachments = [attachment_text(a) for a in _children(event.get('attachments'))]
    structured = '\n\n'.join(filter(None, [blocks, *attachments]))
    if not structured:
        return fallback
    # Slack's accessibility placeholder isn't the user's table content.
    fallback = re.sub(r'(?:,?\s*)with interactive elements\s*$', '', fallback, flags=re.I).strip()
    def normalize(value):
        # Compare Slack's fallback formatting with the equivalent structured text.
        value = re.sub(r'<(https?://[^>|]+)\|([^>]+)>', lambda m: m[2]+' ('+m[1]+')', value)
        value = re.sub(r'<(https?://[^>]+)>', r'\1', value)
        value = re.sub(r'(?m)^\s*(?:[-•]|\d+[.)])\s+', '', value)
        value = re.sub(r'(?<!\w)[*_~`]([^\n]+?)[*_~`](?!\w)', r'\1', value)
        return re.sub(r'\s+', ' ', value).strip()
    normal = normalize(structured)
    if fallback and normalize(fallback) not in normal:
        # Preserve text outside partial block payloads without repeating paragraphs.
        missing = [line for line in fallback.splitlines() if normalize(line) and normalize(line) not in normal]
        if missing:
            structured = '\n'.join(missing) + '\n\n' + structured
    return structured
