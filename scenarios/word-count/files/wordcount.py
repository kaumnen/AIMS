import re
from collections import Counter


def words(text):
    return re.findall(r"\w+", text)


def top_words(text, n):
    counts = Counter(words(text))
    ranked = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    return ranked[:n]
