"""Translate common model Markdown to Slack mrkdwn, preserving code verbatim."""
import re


def message_text(text):
    # Code often contains **, # and leading minus signs with literal meanings.
    code = []
    prefix = '\x00SLACKCODE'
    while prefix in text:
        prefix += '_'
    def protect(match):
        code.append(match[0])
        return prefix + str(len(code) - 1) + '\x00'
    value = re.sub(r'```[\s\S]*?(?:```|\Z)|`[^`\n]+`', protect, text)
    value = re.sub(r'\*\*([^\n]+?)\*\*', r'*\1*', value)
    value = re.sub(r'(?<!\w)__([^\n]+?)__(?!\w)', r'*\1*', value)
    value = re.sub(r'(?m)^#{1,6}[ \t]+(.+?)[ \t]*$',
                   lambda m: '*' + m[1].strip('* ') + '*', value)
    value = re.sub(r'(?m)^([ \t\u00a0]*)[-+*][ \t]+',
                   lambda m: m[1].replace('\u00a0', ' ') + ('◦ ' if m[1] else '• '), value)
    return re.sub(re.escape(prefix) + r'(\d+)\x00', lambda m: code[int(m[1])], value)
