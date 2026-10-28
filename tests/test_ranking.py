from datetime import date

from scientia.ranking import query_terms, score

TODAY = date(2026, 1, 1)


def test_query_terms_drop_stopwords_fields_and_plurals():
    assert query_terms("neural operators for PDEs") == ["neural", "operator", "pde"]
    assert query_terms("ti:transformer AND cat:cs.LG") == ["transformer", "cs", "lg"]


def test_relevance_dominates():
    terms = query_terms("fourier neural operator")
    on_topic = score(terms, "Fourier neural operators", None, 2024, 5, TODAY)
    off_topic = score(terms, "Graph coloring heuristics", None, 2024, 500, TODAY)
    assert on_topic > off_topic


def test_scores_are_bounded():
    assert 0 <= score(["x"], "x", "x", 2026, 10**6, TODAY) <= 1
