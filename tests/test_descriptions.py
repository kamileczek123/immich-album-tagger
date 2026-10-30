from immich_album_tagger.descriptions import (
    is_tag_line,
    merge_keyword_line,
    merge_tag_line,
    normalize_keywords,
    tag_line_of,
)


def test_tag_line_added_to_empty_and_existing_text():
    assert merge_tag_line("", ["a", "b/c"], "#") == "#a #b/c"
    assert merge_tag_line("Fajerwerki\n\n", ["a"], "#") == "Fajerwerki\n#a"


def test_tag_line_is_replaced_not_duplicated():
    text = merge_tag_line("Opis", ["a"], "#")
    assert merge_tag_line(text, ["b"], "#") == "Opis\n#b"
    assert merge_tag_line(text, [], "#") == "Opis"


def test_user_hashtags_mixed_with_words_are_not_owned():
    assert not is_tag_line("Super #wakacje", "#")
    assert merge_tag_line("Super #wakacje", ["a"], "#") == "Super #wakacje\n#a"
    assert tag_line_of("x\n#a #b", "#") == "#a #b"


def test_keyword_line():
    label = "Słowa kluczowe: "
    text = merge_keyword_line("Sylwester u Ani.", ["sylwester", "zielona"], label)
    assert text == "Sylwester u Ani.\nSłowa kluczowe: sylwester zielona"
    assert (
        merge_keyword_line(text, ["nowy rok"], label)
        == "Sylwester u Ani.\nSłowa kluczowe: nowy rok"
    )
    assert merge_keyword_line(text, [], label) == "Sylwester u Ani."


def test_normalize_keywords():
    assert normalize_keywords([" Zielona ", "zielona", "Nowy   Rok", ""]) == ["zielona", "nowy rok"]
