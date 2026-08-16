import genre_flattening


def resolve(*sources):
    return genre_flattening.resolve_canonical_genre(sources)


def test_aliases_are_normalized_before_platform_priority():
    assert resolve(("series", "현판", None)).canonical_genre == "현대판타지"
    assert resolve(("kakao", "로판", None)).canonical_genre == "로맨스판타지"

    result = resolve(
        ("series", "판타지", None),
        ("kakao", "현판", None),
    )
    assert result.canonical_genre == "현대판타지"
    assert result.candidates == ("현대판타지", "판타지")
    assert result.review_required is False
    assert result.state == "resolved"


def test_novelpia_modifier_first_tag_recovers_first_content_genre():
    result = resolve((
        "novelpia",
        "고수위",
        ("고수위", "하렘", "현대판타지", "집착"),
    ))
    assert result.canonical_genre == "현대판타지"
    assert result.candidates == ("현대판타지",)


def test_novelpia_modifier_without_content_genre_does_not_become_misc():
    result = resolve((
        "novelpia",
        "TS",
        ("TS", "약피폐", "인터넷방송"),
    ))
    assert result.canonical_genre is None
    assert result.candidates == ()
    assert result.review_required is False
    assert result.state == "missing"


def test_platform_priority_is_kakao_then_series_then_novelpia():
    result = resolve(
        ("novelpia", "스포츠", ("스포츠",)),
        ("series", "현판", None),
        ("kakao", "판타지", None),
    )
    assert result.canonical_genre == "판타지"
    assert result.candidates == ("판타지", "현대판타지", "스포츠")
    assert result.review_required is False

    without_kakao = resolve(
        ("novelpia", "스포츠", ("스포츠",)),
        ("series", "현판", None),
    )
    assert without_kakao.canonical_genre == "현대판타지"
    assert without_kakao.candidates == ("현대판타지", "스포츠")

    only_novelpia = resolve(("novelpia", "SF", ("SF",)))
    assert only_novelpia.canonical_genre == "SF"


def test_conflicting_specific_genres_follow_platform_priority():
    result = resolve(
        ("series", "무협", None),
        ("kakao", "BL", None),
    )
    assert result.canonical_genre == "BL"
    assert result.candidates == ("BL", "무협")
    assert result.review_required is False


def test_romance_conflict_follows_kakao():
    result = resolve(
        ("series", "현판", None),
        ("kakao", "로맨스", None),
    )
    assert result.canonical_genre == "로맨스"
    assert result.candidates == ("로맨스", "현대판타지")
    assert result.review_required is False


def test_lower_priority_content_genre_does_not_override_higher_priority_style_label():
    result = resolve(
        ("series", "라이트노벨", None),
        ("novelpia", "판타지", ("판타지",)),
    )
    assert result.canonical_genre == "라이트노벨"
    assert result.candidates == ("라이트노벨", "판타지")

    assert resolve(
        ("novelpia", "기타", ("기타",)),
    ).canonical_genre == "기타"
