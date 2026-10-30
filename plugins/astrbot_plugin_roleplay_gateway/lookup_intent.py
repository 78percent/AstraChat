"""Shared, standard-library-only research intent; no I/O or model calls."""
from __future__ import annotations

from dataclasses import dataclass
import re


@dataclass(frozen=True)
class LookupPlan:
    kind: str
    query: str = ""
    city: str = ""
    repo: str = ""
    day_offset: int = 0


_NEGATIVE = re.compile(r"(?:不要|不用|别|无需|不需要).{0,5}(?:联网|搜索|查询|查资料)|\b(?:do not|don't|without)\s+(?:search|browse)", re.I)
_QUERY = re.compile(r"(?:^|\s)/(?:查|搜索|search)\s*|(?:帮我|请|麻烦)?(?:查一下|查询|搜索|搜一下|查一查|查资料|查查)|帮我查|联网(?:查|搜索)?", re.I)
_TEACH = re.compile(r"教我|讲解|解释(?:一下|下)?|讲讲|什么是|是什么(?:意思|原理|东西)?|\b(?:explain|teach me|what is)\b", re.I)
_CHAT_IDENTITY = re.compile(r"(?:你|我|我们|咱们).{0,20}(?:是什么(?:角色|人|身份|关系)|(?:颜色|爱好|喜好|心情|名字|生日).{0,8}是什么)|今天是什么心情")
_GH = re.compile(r"https?://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9_.-]{1,100})(?=[/?#\s，。！？、]|$)", re.I)
_GH_FIND = re.compile(r"(?:找|搜索|查).{0,24}(?:开源|github|GitHub).{0,12}(?:项目|仓库)?|(?:github|GitHub).{0,12}(?:项目|仓库).{0,12}(?:推荐|搜索|查找)", re.I)


def _clean(text: str, wake_names=()) -> str:
    value = text.strip()
    # Native @ display text is optional and never supplies identity authority.
    value = re.sub(r"^\s*@[^\s\u2005]+[\s\u2005]+", "", value)
    for name in sorted((n for n in wake_names if isinstance(n, str) and n), key=len, reverse=True):
        if value.startswith(name):
            value = value[len(name):].lstrip(" ，,：:、\u2005")
            break
    return value


def _city(text: str) -> str:
    english = re.search(r"weather\s+(?:in|for)\s+(.+?)(?:\?|$)", text, re.I)
    prefix = english.group(1) if english else re.split(r"天气|weather", text, maxsplit=1, flags=re.I)[0]
    # A leading name/greeting before a comma is not part of a location.
    prefix = re.split(r"[，,：:]", prefix)[-1]
    prefix = re.sub(r"(?:今天|明天|后天|今日|明日|现在|当前|今早|今晚|早上|晚上)|\b(?:today|tomorrow)\b", "", prefix, flags=re.I)
    prefix = re.sub(r"^(?:(?:请|帮我|麻烦|给我|查一下|查查|查一查|查询|看看|搜索|搜一下|查|看一下|想知道|我想知道|了解一下|在)\s*)+", "", prefix)
    prefix = re.sub(r"^/(?:查|搜索|search)\s*", "", prefix, flags=re.I)
    prefix = prefix.strip(" 的地?？。！! \t\n")
    if (not 1 <= len(prefix) <= 40 or re.search(r"这里|那里|我这|本地|附近|所在|位置|本周|周末|下周|几天|未来|哪[里个]|怎么样|如何", prefix)
            or re.search(r"\b(?:here|there|local|my location)\b", prefix, re.I)
            or re.search(r"[<>\[\]{};/\\@]", prefix)):
        return ""
    return prefix


def classify_lookup(text: str, wake_names=()) -> LookupPlan | None:
    """Conservative explicit queries; the caller still enforces wake/access rules."""
    if not isinstance(text, str) or not text.strip() or len(text) > 2000 or "\0" in text:
        return None
    value = _clean(text, wake_names)
    if _NEGATIVE.search(value):
        return None
    gh = _GH.search(value)
    if gh and not re.search(r"github\.com/[^\s]*[?].*(?:token|key|secret)=", value, re.I):
        repo = f"{gh.group(1)}/{gh.group(2).removesuffix('.git')}"
        return LookupPlan("github_repo", query=value, repo=repo)
    if _TEACH.search(value) and not _QUERY.search(value) and not _CHAT_IDENTITY.search(value) and not re.search(r"(?:今天|明天|后天).{0,20}天气", value):
        return LookupPlan("knowledge", query=value)
    if re.search(r"天气|\bweather\b", value, re.I):
        if re.search(r"(?:喜欢|讨厌|爱这|天气真好|天气不错|天气好好|天气很好|天气好舒服)", value) and not _QUERY.search(value):
            return None
        offset = 2 if "后天" in value else 1 if re.search(r"明天|明日|\btomorrow\b", value, re.I) else 0
        return LookupPlan("weather", query=value, city=_city(value), day_offset=offset)
    if _GH_FIND.search(value):
        match = _QUERY.search(value)
        query = value[match.end():].strip(" ：:，,。") if match else value
        return LookupPlan("github_search", query=query or value)
    match = _QUERY.search(value)
    if match:
        query = value[match.end():].strip(" ：:，,。")
        return LookupPlan("search", query=query)
    return None


def research_eligible(text: str, wake_names=()) -> bool:
    return classify_lookup(text, wake_names) is not None
