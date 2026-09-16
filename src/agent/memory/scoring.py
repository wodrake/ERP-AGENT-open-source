"""COLD 层检索打分（纯标准库实现）。

没有引入向量库，用的是 BM25 + 时间衰减 + 引用频次 + 置信度的加权分。
理由：项目场景里 COLD 层的候选量是**每位用户几百条**这个量级，BM25 在这个
规模上足够好，而且零依赖、零额外延迟、可解释（能说出"为什么召回这条"）。
真到需要语义模糊匹配时再叠向量召回即可。
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import List, Optional, Sequence

_CJK_RANGE = "\u4e00-\u9fff"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[" + _CJK_RANGE + r"]+")


def _is_cjk(ch: str) -> bool:
    return bool(ch) and "\u4e00" <= ch <= "\u9fff"


def tokenize(text: str) -> List[str]:
    """中英混合分词：英文按词，中文按字 + 二元组（兼顾召回与噪声）。"""
    tokens: List[str] = []
    for match in _TOKEN_RE.finditer((text or "").lower()):
        piece = match.group(0)
        if not piece:
            continue
        if _is_cjk(piece[0]):
            if len(piece) == 1:
                tokens.append(piece)
                continue
            tokens.extend(piece)
            tokens.extend(piece[i:i + 2] for i in range(len(piece) - 1))
        else:
            tokens.append(piece)
    return tokens


def bm25(query_tokens: Sequence[str], docs: Sequence[Sequence[str]],
         k1: float = 1.5, b: float = 0.75) -> List[float]:
    """标准 BM25，返回每条 doc 的原始分（未归一化）。"""
    n_docs = len(docs)
    if n_docs == 0:
        return []
    df: dict = {}
    lengths: List[int] = []
    for tokens in docs:
        lengths.append(len(tokens))
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1
    avgdl = (sum(lengths) / n_docs) or 1.0

    scores: List[float] = []
    for tokens, dl in zip(docs, lengths):
        if not tokens:
            scores.append(0.0)
            continue
        tf: dict = {}
        for term in tokens:
            tf[term] = tf.get(term, 0) + 1
        total = 0.0
        for term in set(query_tokens):
            freq = tf.get(term, 0)
            if not freq:
                continue
            idf = math.log(1 + (n_docs - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5))
            denom = freq + k1 * (1 - b + b * (dl / avgdl))
            total += idf * (freq * (k1 + 1)) / denom
        scores.append(total)
    return scores


def normalize(values: Sequence[float]) -> List[float]:
    if not values:
        return []
    top = max(values)
    if top <= 0:
        return [0.0] * len(values)
    return [v / top for v in values]


def recency_score(age_days: float, half_life_days: float = 30.0) -> float:
    """指数衰减：半衰期默认 30 天。"""
    if half_life_days <= 0:
        return 1.0
    return math.pow(0.5, max(0.0, age_days) / half_life_days)


def access_score(access_count: int, cap: int = 10) -> float:
    if access_count <= 0:
        return 0.0
    return min(1.0, access_count / float(cap))


def combined_score(
    text_score: float,
    age_days: float,
    access_count: int,
    confidence: float,
    *,
    half_life_days: float = 30.0,
    weight_text: float = 0.6,
    weight_recency: float = 0.2,
    weight_access: float = 0.1,
    weight_confidence: float = 0.1,
) -> float:
    return (
        weight_text * max(0.0, min(1.0, text_score))
        + weight_recency * recency_score(age_days, half_life_days)
        + weight_access * access_score(access_count)
        + weight_confidence * max(0.0, min(1.0, confidence))
    )


def rank(query: str, candidates: Sequence, config=None, now: Optional[datetime] = None):
    """给候选记忆打分并按分数降序返回 ``(item, score)`` 列表。"""
    if not candidates:
        return []
    from .config import DEFAULT_MEMORY_CONFIG

    cfg = config or DEFAULT_MEMORY_CONFIG
    query_tokens = tokenize(query)
    if not query_tokens:
        return []

    docs = [tokenize(item.searchable_text()) for item in candidates]
    raw = bm25(query_tokens, docs)
    norm = normalize(raw)

    now_dt = now or datetime.now()
    scored = []
    for item, text_score, raw_score in zip(candidates, norm, raw):
        # 一个查询词都没命中的直接排除：否则时间衰减/置信度这些"先验分"
        # 会把完全不相关的记忆托过阈值，检索就失去意义了。
        if raw_score <= 0:
            continue
        score = combined_score(
            text_score,
            item.age_days(now_dt),
            item.access_count,
            item.confidence,
            half_life_days=cfg.half_life_days,
            weight_text=cfg.weight_text,
            weight_recency=cfg.weight_recency,
            weight_access=cfg.weight_access,
            weight_confidence=cfg.weight_confidence,
        )
        scored.append((item, score))

    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored
