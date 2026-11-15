"""One set of AI Act role words: the generated summaries (pattern 4) and the page's AI Act lines read the same words."""

from scripts import build, summaries


def test_published_role_words_are_the_generators(dataset):
    published = build.export_dataset(dataset)
    assert published["dataset"]["display"]["authority_roles"] == summaries.ROLE_WORDS
    roles = {r for j in published["jurisdictions"] for b in j.get("related_bodies", []) for r in b.get("authority_roles") or []}
    assert roles <= set(summaries.ROLE_WORDS)  # every role in the data has its words
