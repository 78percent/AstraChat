"""Validated, ordered persona worldbooks; pure matching without file or model I/O."""
import re
from .context_budget import estimate_tokens

MAX_TOKENS = 3000

def validate_entries(value):
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError('invalid_worldbook')
    entries, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            raise ValueError('invalid_worldbook')
        identity, content, title = item.get('id'), item.get('content'), item.get('title', '')
        if (not isinstance(identity, str) or not re.fullmatch('[A-Za-z0-9_-]{1,64}', identity)
                or identity in seen or not isinstance(title, str) or len(title) > 100
                or not isinstance(content, str) or not content.strip() or '\0' in content or len(content) > 6000
                or type(item.get('enabled')) is not bool or item.get('mode') not in ('always', 'keyword')
                or item.get('position') not in ('before', 'after')):
            raise ValueError('invalid_worldbook')
        keys = item.get('keywords', [])
        if (not isinstance(keys, list) or len(keys) > 20
                or any(not isinstance(k, str) or not k.strip() or len(k) > 50 or '\0' in k for k in keys)
                or (item['mode'] == 'keyword' and not keys)):
            raise ValueError('invalid_worldbook_keywords')
        seen.add(identity)
        entries.append(dict(id=identity, title=title, content=content, enabled=item['enabled'],
                            mode=item['mode'], position=item['position'], keywords=list(dict.fromkeys(k.strip() for k in keys))))
    if sum(estimate_tokens(e['content']) + 8 for e in entries if e['enabled']) > MAX_TOKENS:
        raise ValueError('worldbook_budget_exceeded')
    return entries

def select_entries(entries, text):
    before, after, matched = [], [], []
    folded = text.casefold()
    for entry in entries:
        if entry['enabled'] and (entry['mode'] == 'always' or any(k.casefold() in folded for k in entry['keywords'])):
            (before if entry['position'] == 'before' else after).append(entry['content'])
            matched.append(entry['id'])
    return tuple(before), tuple(after), matched
