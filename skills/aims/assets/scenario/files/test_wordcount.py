from wordcount import top_words


def test_counts_words():
    assert top_words("a b a", 1) == [("a", 2)]


def test_ignores_case():
    assert top_words("The the THE cat", 2) == [("the", 3), ("cat", 1)]


def test_breaks_ties_alphabetically():
    assert top_words("pear apple fig", 3) == [("apple", 1), ("fig", 1), ("pear", 1)]
