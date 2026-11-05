"""Keyword candidates for Korean/Latin text, without service-specific dictionaries.

BM25 ranks text already in the eligible Chroma collection. RRF combines ranks,
not incomparable cosine/BM25 scores. Neither score establishes factual support.
"""

import math
import re
from collections import Counter


def terms(text: str) -> Counter[str]:
    tokens: list[str] = []
    for word in re.findall(r"[a-z0-9]+|[가-힣]+", text.casefold()):
        tokens.append(word)
        if re.fullmatch(r"[가-힣]+", word):
            tokens.extend(word[i : i + 2] for i in range(len(word) - 1))
    return Counter(tokens)


def keyword_ranking(query: str, documents: dict[str, str]) -> list[str]:
    query_terms = terms(query)
    corpus = {key: terms(text) for key, text in documents.items()}
    if not corpus or not query_terms:
        return []
    lengths = {key: sum(tokens.values()) for key, tokens in corpus.items()}
    average = sum(lengths.values()) / len(corpus) or 1
    frequencies = Counter(token for tokens in corpus.values() for token in tokens)
    scores: dict[str, float] = {}
    for key, tokens in corpus.items():
        score = 0.0
        for token in query_terms.keys() & tokens.keys():
            idf = math.log(
                1 + (len(corpus) - frequencies[token] + 0.5) / (frequencies[token] + 0.5)
            )
            count = tokens[token]
            score += idf * count * 2.5 / (count + 1.5 * (0.25 + 0.75 * lengths[key] / average))
        if score > 0:
            scores[key] = score
    return sorted(scores, key=lambda key: (-scores[key], key))


def fuse_rankings(*rankings: list[str]) -> list[str]:
    scores: Counter[str] = Counter()
    for ranking in rankings:
        for rank, key in enumerate(ranking, start=1):
            scores[key] += 1 / (60 + rank)
    return sorted(scores, key=lambda key: (-scores[key], key))
