"""Step 4 core: metadata.json -> YouTube / Facebook / Instagram payloads (pure functions)."""
from __future__ import annotations

from publish_fakes import SAMPLE_META

from autoclipper.publish import metadata as m
from autoclipper.publish.settings import InstagramTarget, YouTubeTarget


def test_normalize_hashtags():
    assert m.normalize_hashtags(["#a", "b", " #c d ", "##e", "", "#", "#A", 5, None, "#चैलेंजर"]) == \
        ["#a", "#b", "#cd", "#e", "#चैलेंजर"]
    assert m.normalize_hashtags(None) == []


def test_youtube_payload_sample_hi():
    p = m.youtube_payload(SAMPLE_META["hi"], "hi", YouTubeTarget())
    sn, st = p["snippet"], p["status"]
    assert sn["title"] == SAMPLE_META["hi"]["title"]
    assert sn["description"].startswith(SAMPLE_META["hi"]["description"])
    assert sn["description"].endswith("#भाषण #Shorts")
    assert sn["tags"] == [t.lstrip("#") for t in SAMPLE_META["hi"]["hashtags"]]
    assert sn["categoryId"] == "22" and sn["defaultLanguage"] == "hi" and sn["defaultAudioLanguage"] == "hi"
    assert st == {"privacyStatus": "private", "selfDeclaredMadeForKids": False, "containsSyntheticMedia": True}


def test_youtube_shorts_tag_not_duplicated_and_optional():
    meta = dict(SAMPLE_META["en"], hashtags=["#shorts", "#x"])
    p = m.youtube_payload(meta, "en", YouTubeTarget())
    assert p["snippet"]["description"].count("#") == 2
    p = m.youtube_payload(SAMPLE_META["en"], "en", YouTubeTarget(add_shorts_hashtag=False))
    assert "#Shorts" not in p["snippet"]["description"]


def test_youtube_title_limit_and_angle_brackets():
    title = "<b>" + "word " * 40 + ">"
    p = m.youtube_payload({"title": title, "description": "a <script> b", "hashtags": []}, "en", YouTubeTarget())
    t = p["snippet"]["title"]
    assert len(t) <= 100 and t.endswith("…") and "<" not in t and ">" not in t
    assert not t[:-1].endswith(" ")
    assert "<" not in p["snippet"]["description"] and ">" not in p["snippet"]["description"]


def test_youtube_title_exactly_100_kept():
    t = "x" * 100
    assert m.youtube_payload({"title": t}, "en", YouTubeTarget())["snippet"]["title"] == t


def test_youtube_description_5000_bytes_devanagari():
    desc = "अ" * 3000  # 3 bytes each = 9000 bytes
    p = m.youtube_payload({"title": "t", "description": desc, "hashtags": ["#x"]}, "hi", YouTubeTarget())
    d = p["snippet"]["description"]
    assert len(d.encode("utf-8")) <= 5000
    assert len(d.encode("utf-8")) >= 4998  # cut on a code point boundary, not earlier
    d.encode("utf-8").decode("utf-8")  # valid UTF-8


def test_youtube_tags_500_rule():
    tags = [f"#{i}" + "a" * 98 for i in range(6)]  # 99 + comma each -> 5 fit (99*5 + 4 = 499)
    out = m.youtube_tags(m.normalize_hashtags(tags + ["#b"]))
    assert len(out) == 5  # 6th does not fit; "b" (1 + comma -> 501) does not fit either
    assert sum(len(t) for t in out) + len(out) - 1 <= 500
    # a tag with a space counts its quotes
    assert m.youtube_tag_cost("a b") == 5
    assert m.youtube_tags(["#" + "x" * 500]) == ["x" * 500]
    assert m.youtube_tags(["#" + "x" * 501]) == []


def test_facebook_payload():
    p = m.facebook_payload(SAMPLE_META["en"])
    assert p["title"] == SAMPLE_META["en"]["title"]
    assert p["description"].endswith(" ".join(SAMPLE_META["en"]["hashtags"]))
    long = m.facebook_payload({"title": "x " * 200})
    assert len(long["title"]) <= 255


def test_instagram_caption_sample_and_limits():
    p = m.instagram_payload(SAMPLE_META["hi"], InstagramTarget())
    assert p["caption"].startswith(SAMPLE_META["hi"]["title"] + "\n\n")
    assert p["caption"].endswith("#भाषण") and p["share_to_feed"] is True
    meta = {"title": "Title", "description": "word " * 1000, "hashtags": [f"#t{i}" for i in range(40)]}
    p = m.instagram_payload(meta, InstagramTarget(share_to_feed=False))
    c = p["caption"]
    assert len(c) <= 2200
    assert c.startswith("Title\n\n")
    assert c.endswith("#t29") and "#t30" not in c  # 30 hashtags kept, description cut first
    assert m.count_hashtags(c) == 30
    assert p["share_to_feed"] is False


def test_empty_metadata():
    assert m.youtube_payload({}, "en", YouTubeTarget())["snippet"]["title"] == ""
    assert m.instagram_payload({}, InstagramTarget())["caption"] == ""
