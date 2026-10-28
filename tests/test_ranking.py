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


def test_being_cited_by_the_topic_outweighs_citing_it():
    foundational = score([], "x", None, 2020, 0, TODAY, cited_in=10, cites_out=0, max_links=10)
    review = score([], "x", None, 2020, 0, TODAY, cited_in=0, cites_out=10, max_links=10)
    assert foundational > review
