"""The local Pawchive index: storing artists' images, finding edited
copies of them, and building it politely. No test here touches the
network - pawchive is played by a fake session."""
import io
import json
import os
import random
import shutil
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests import _path  # noqa: F401  (puts the project root on sys.path)

from PIL import Image, ImageFilter

from core import pawchive_index
from core.pawchive_index import PawchiveIndex, fingerprints


def _picture(seed: int, size=(300, 420)) -> Image.Image:
    """A picture with real structure - blobs of colour - unique per seed."""
    rng = random.Random(seed)
    small = Image.new("RGB", (12, 16))
    small.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                   for _ in range(12 * 16)])
    return small.resize(size, Image.BICUBIC).filter(ImageFilter.GaussianBlur(3))


def _encode(image: Image.Image, fmt="PNG", **kwargs) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, fmt, **kwargs)
    return buffer.getvalue()


def _sha(n: int) -> str:
    return f"{n:064x}"


def _path_for(sha: str, ext="png") -> str:
    return f"/{sha[:2]}/{sha[2:4]}/{sha}.{ext}"


class _TempIndex(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="hatate-pawindex-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.index = PawchiveIndex(os.path.join(self.folder, "index.db"))

    def _row(self, post, page, sha, image, service="fanbox", user="42", title="t"):
        h8, h16 = fingerprints(_encode(image))
        return (service, user, str(post), page, sha, _path_for(sha), title,
                f"{h8:016x}", f"{h16:064x}")


class TestTheIndex(_TempIndex):
    def test_an_edited_copy_is_found_and_scored_as_the_same_picture(self):
        self.index.add_creator("fanbox", "42", "Some Artist")
        original = _picture(1)
        self.index.add_images([self._row(100, 0, _sha(1), original),
                               self._row(101, 0, _sha(2), _picture(2))])
        # Resized to a third and re-saved as a lossy JPEG: a different file,
        # the same picture.
        edited = original.resize((100, 140), Image.LANCZOS)
        _, local16 = fingerprints(_encode(edited, "JPEG", quality=70))
        matches = self.index.search(local16)
        self.assertTrue(matches)
        best = matches[0]
        self.assertEqual(best.url, "https://pawchive.pw/fanbox/user/42/post/100")
        self.assertFalse(best.exact)
        self.assertGreaterEqual(best.similarity, 90.0)
        self.assertEqual(best.artist, "Some Artist")
        self.assertEqual(best.thumb_url,
                         f"https://img.pawchive.pw/thumbnail/data{_path_for(_sha(1))}")

    def test_a_different_picture_is_not_a_match(self):
        self.index.add_images([self._row(100, 0, _sha(1), _picture(1))])
        _, local16 = fingerprints(_encode(_picture(99)))
        self.assertEqual(self.index.search(local16), [])

    def test_the_same_sha256_is_the_very_same_file(self):
        self.index.add_images([self._row(100, 0, _sha(7), _picture(7))])
        [match] = self.index.search(None, _sha(7))
        self.assertTrue(match.exact)
        self.assertEqual(match.similarity, 100.0)

    def test_one_result_per_post_on_its_closest_image(self):
        picture = _picture(3)
        self.index.add_images([
            self._row(100, 0, _sha(10), _picture(11)),
            self._row(100, 1, _sha(12), picture),
            self._row(100, 2, _sha(13), picture.resize((150, 210))),
        ])
        _, local16 = fingerprints(_encode(picture))
        [match] = self.index.search(local16)
        self.assertEqual(match.page_index, 1)
        self.assertEqual(match.distance, 0)

    def test_new_images_are_searchable_straight_away(self):
        _, local16 = fingerprints(_encode(_picture(5)))
        self.assertEqual(self.index.search(local16), [])     # loads the (empty) cache
        self.index.add_images([self._row(100, 0, _sha(5), _picture(5))])
        self.assertEqual(len(self.index.search(local16)), 1)

    def test_removing_an_artist_removes_their_images(self):
        self.index.add_creator("fanbox", "42", "A")
        self.index.add_images([self._row(100, 0, _sha(1), _picture(1))])
        self.index.remove_creator("fanbox", "42")
        self.assertEqual(self.index.creators(), [])
        self.assertTrue(self.index.is_empty())

    def test_artists_report_what_was_indexed(self):
        self.index.add_creator("fanbox", "42", "A")
        self.index.add_images([self._row(100, 0, _sha(1), _picture(1)),
                               ("fanbox", "42", "101", 0, _sha(2), _path_for(_sha(2)), "t", None, None)])
        [creator] = self.index.creators()
        self.assertEqual((creator.image_count, creator.unfingerprinted), (2, 1))

    def test_a_lightly_cropped_copy_still_surfaces_as_one_to_review(self):
        """MEASURED: a 3% crop each side sat at distance 28 - past the old
        cut-off of 24 - while the nearest different picture was at 41."""
        self.assertGreaterEqual(pawchive_index.MAX_MATCH_DISTANCE, 28)
        self.assertLess(pawchive_index.MAX_MATCH_DISTANCE, 41)
        self.assertLess(pawchive_index.similarity_for(28), 75.0)    # can't satisfy a fallback

    def test_the_scale_keeps_variants_below_the_auto_import_line(self):
        self.assertEqual(pawchive_index.similarity_for(0), 100.0)
        self.assertEqual(pawchive_index.similarity_for(6), 90.0)
        self.assertLess(pawchive_index.similarity_for(9), 90.0)     # a measured variant
        self.assertEqual(pawchive_index.similarity_for(24), 70.0)

    def test_links_name_the_artist(self):
        self.assertEqual(pawchive_index.creator_from_url(
            "https://pawchive.pw/patreon/user/6714576/post/38740818"), ("patreon", "6714576"))
        self.assertEqual(pawchive_index.creator_from_url("https://pawchive.pw/fanbox/user/9"),
                         ("fanbox", "9"))
        self.assertIsNone(pawchive_index.creator_from_url("https://example.com/user/1"))


class _FakePawchive:
    """Plays pawchive's API and thumbnail host from a dict of posts."""

    def __init__(self, posts, name="Artist", thumbnails=None, rate_limit_first=False):
        self.posts = posts
        self.name = name
        self.thumbnails = thumbnails or {}
        self.requests = []
        self._limited = rate_limit_first

    def get(self, url, params=None, timeout=None, headers=None):
        self.requests.append((url, dict(params or {})))
        if self._limited:
            self._limited = False
            return SimpleNamespace(status_code=429, headers={"Retry-After": "0"},
                                   json=lambda: None, content=b"")
        if url.endswith("/profile"):
            return self._json({"name": self.name})
        if url.endswith("/posts"):
            offset = int((params or {}).get("o", 0))
            return self._json(self.posts[offset:offset + pawchive_index.PAGE_SIZE])
        body = self.thumbnails.get(url.split("/thumbnail/data", 1)[-1])
        if body is None:
            return SimpleNamespace(status_code=404, headers={}, json=lambda: None, content=b"")
        return SimpleNamespace(status_code=200, headers={}, json=lambda: None, content=body)

    @staticmethod
    def _json(body):
        return SimpleNamespace(status_code=200, headers={}, json=lambda: body,
                               content=json.dumps(body).encode())


def _post(post_id, shas, ext="png"):
    files = [{"name": f"{i}.{ext}", "path": _path_for(sha, ext)} for i, sha in enumerate(shas)]
    return {"id": str(post_id), "title": f"post {post_id}", "file": files[0],
            "attachments": files}


class TestIndexingAnArtist(_TempIndex):
    def _crawl(self, fake, **kwargs):
        return pawchive_index.index_creator(self.index, "fanbox", "42", session=fake,
                                            delay=0, **kwargs)

    def test_every_page_of_posts_and_every_image_is_indexed(self):
        posts = [_post(1000 + i, [_sha(1000 + i)]) for i in range(pawchive_index.PAGE_SIZE + 3)]
        thumbs = {_path_for(_sha(1000 + i)): _encode(_picture(i))
                  for i in range(pawchive_index.PAGE_SIZE + 3)}
        fake = _FakePawchive(posts, name="TEKU", thumbnails=thumbs)
        result = self._crawl(fake)
        self.assertEqual((result.posts, result.images, result.new), (53, 53, 53))
        self.assertIsNone(result.error)
        [creator] = self.index.creators()
        self.assertEqual((creator.name, creator.image_count, creator.post_count), ("TEKU", 53, 53))
        listing_offsets = [p.get("o") for u, p in fake.requests if u.endswith("/posts")]
        self.assertEqual(listing_offsets, [0, 50])

    def test_a_refresh_fetches_only_what_is_new(self):
        posts = [_post(1, [_sha(1)]), _post(2, [_sha(2)])]
        thumbs = {_path_for(_sha(n)): _encode(_picture(n)) for n in (1, 2, 3)}
        self._crawl(_FakePawchive(posts, thumbnails=thumbs))
        fake = _FakePawchive([_post(3, [_sha(3)])] + posts, thumbnails=thumbs)
        result = self._crawl(fake)
        self.assertEqual(result.new, 1)
        thumbnail_requests = [u for u, _ in fake.requests if "/thumbnail/" in u]
        self.assertEqual(thumbnail_requests,
                         [f"https://img.pawchive.pw/thumbnail/data{_path_for(_sha(3))}"])

    def test_non_images_are_skipped_and_missing_thumbnails_counted(self):
        post = _post(1, [_sha(1), _sha(2)])
        post["attachments"].append({"name": "set.zip", "path": _path_for(_sha(3), "zip")})
        fake = _FakePawchive([post], thumbnails={_path_for(_sha(1)): _encode(_picture(1))})
        result = self._crawl(fake)
        self.assertEqual((result.images, result.unfingerprinted), (2, 1))
        self.assertFalse(any(".zip" in u for u, _ in fake.requests))

    def test_stopping_keeps_what_was_done_and_the_next_run_carries_on(self):
        posts = [_post(i, [_sha(i)]) for i in range(1, 6)]
        thumbs = {_path_for(_sha(i)): _encode(_picture(i)) for i in range(1, 6)}
        fake = _FakePawchive(posts, thumbnails=thumbs)

        def should_stop():
            thumbnail_calls = sum(1 for u, _ in fake.requests if "/thumbnail/" in u)
            return thumbnail_calls >= 2
        result = self._crawl(fake, should_stop=should_stop)
        self.assertTrue(result.stopped)
        self.assertEqual(len(self.index.known_pages("fanbox", "42")), 2)
        result = self._crawl(_FakePawchive(posts, thumbnails=thumbs))
        self.assertEqual(result.new, 3)
        self.assertEqual(len(self.index.known_pages("fanbox", "42")), 5)

    def test_being_rate_limited_is_waited_out_not_fatal(self):
        fake = _FakePawchive([_post(1, [_sha(1)])],
                             thumbnails={_path_for(_sha(1)): _encode(_picture(1))},
                             rate_limit_first=True)
        result = self._crawl(fake)
        self.assertIsNone(result.error)
        self.assertEqual(result.new, 1)

    def test_an_edited_copy_of_an_indexed_image_is_found(self):
        """End to end: index through the fake site, then search with a
        resized, re-saved copy of one of its pictures."""
        pictures = {i: _picture(i) for i in range(1, 4)}
        posts = [_post(i, [_sha(i)]) for i in pictures]
        thumbs = {_path_for(_sha(i)): _encode(p.resize((200, 280))) for i, p in pictures.items()}
        self._crawl(_FakePawchive(posts, thumbnails=thumbs))
        _, local16 = fingerprints(_encode(pictures[2].resize((600, 840)), "JPEG", quality=80))
        best = self.index.search(local16)[0]
        self.assertEqual(best.url, "https://pawchive.pw/fanbox/user/42/post/2")
        self.assertGreaterEqual(best.similarity, 90.0)


class TestFindingArtistsByName(unittest.TestCase):
    CREATORS = [
        {"id": "1", "name": "TEKU", "service": "patreon", "favorited": 50, "public_id": "teku"},
        {"id": "2", "name": "Tekuho", "service": "fanbox", "favorited": 900, "public_id": "tekuho"},
        {"id": "3", "name": "Someone", "service": "fanbox", "favorited": 10, "public_id": "x"},
    ]

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="hatate-creators-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.cache = os.path.join(self.folder, "creators.json")

    def test_the_list_is_downloaded_once_then_kept(self):
        class Session:
            calls = 0

            def get(inner, url, headers=None, timeout=None):
                Session.calls += 1
                return SimpleNamespace(json=lambda: self.CREATORS, raise_for_status=lambda: None)
        found = pawchive_index.find_creators("tek", cache_path=self.cache, session=Session())
        self.assertEqual([c["name"] for c in found], ["Tekuho", "TEKU"])   # most favourited first
        pawchive_index.find_creators("some", cache_path=self.cache, session=Session())
        self.assertEqual(Session.calls, 1)


class TestTheIndexAsASearchEngine(_TempIndex):
    def _collect(self, entry):
        from core import search_engine
        candidates, errors = [], []
        with patch.object(search_engine.engine_runner, "_shared_pawchive_index", return_value=self.index):
            found = search_engine._collect_pawchive_index(entry, candidates, errors)
        return found, candidates, errors

    def _entry(self, image=None, sha=None):
        from core.models import ImageEntry
        path = os.path.join(self.folder, "local.jpg")
        if image is not None:
            image.save(path, "JPEG", quality=85)
        entry = ImageEntry(path=path)
        entry.hydrus_hash = sha
        return entry

    def test_an_empty_index_costs_nothing_not_even_a_file_read(self):
        entry = self._entry()                          # the file does not exist
        found, candidates, errors = self._collect(entry)
        self.assertEqual((found, candidates, errors), (False, [], []))

    def test_an_edited_copy_becomes_a_measured_candidate(self):
        from core.engines import reports_real_similarity
        picture = _picture(8)
        self.index.add_images([self._row(100, 0, _sha(8), picture)])
        found, candidates, errors = self._collect(self._entry(picture.resize((150, 210))))
        self.assertTrue(found)
        [candidate] = candidates
        self.assertEqual(candidate.engine, "Pawchive index")
        self.assertEqual(candidate.source_name, "Pawchive")
        self.assertTrue(reports_real_similarity(candidate.engine))
        self.assertGreaterEqual(candidate.similarity, 90.0)

    def test_the_very_same_file_is_labelled_as_an_exact_copy(self):
        self.index.add_images([self._row(100, 0, _sha(9), _picture(9))])
        _, candidates, _ = self._collect(self._entry(_picture(50), sha=_sha(9)))
        self.assertEqual(candidates[0].engine, "Pawchive")      # sendable as the original

    def test_it_never_makes_the_search_wait(self):
        from core.rate_limit import engine_interval
        self.assertEqual(engine_interval("pawchiveindex", {}, 5.0, 8.0), (0.0, 0.0))
        self.assertEqual(engine_interval("pawchive", {}, 5.0, 8.0), (5.0, 8.0))

    def test_it_runs_in_the_first_wave(self):
        from core.config import Settings
        from core.search_engine import _engine_waves
        s = Settings()
        s.primary_engine, s.secondary_engine_mode = "saucenao", "fallback"
        s.enable_pawchive, s.enable_pawchive_index = True, True
        s.extras_only_as_fallback = True
        self.assertEqual(_engine_waves(s, None)[0][:3], ["saucenao", "pawchive", "pawchiveindex"])
        s.enable_pawchive_index = False
        self.assertNotIn("pawchiveindex", _engine_waves(s, None)[0])

    def test_its_thumbnail_names_the_image_in_a_multi_image_post(self):
        from core import search_engine
        from core.config import Settings
        from core.models import MatchCandidate
        shas = [_sha(21), _sha(22), _sha(23)]
        post = {"post": {"id": "9", "user": "1", "service": "fanbox", "has_full": True,
                         "file": {"path": _path_for(shas[0])},
                         "attachments": [{"path": _path_for(s)} for s in shas]}}
        local = os.path.join(self.folder, "edited.jpg")
        _picture(1).save(local, "JPEG")                 # not byte-identical to any page
        candidate = MatchCandidate(
            url="https://pawchive.pw/fanbox/user/1/post/9",
            thumb_url=f"https://img.pawchive.pw/thumbnail/data{_path_for(shas[2])}")
        with patch.object(search_engine.remote, "fetch_text", return_value=json.dumps(post)), \
             patch.object(search_engine.remote, "download_bytes") as download:
            search_engine._resolve_multipage_candidate(candidate, 3, Settings(), local)
        download.assert_not_called()
        self.assertEqual(candidate.page_index, 2)


if __name__ == "__main__":
    unittest.main()
