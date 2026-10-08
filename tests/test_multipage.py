"""Picking the right page of a multi-image post.

A Pixiv artwork can hold dozens of images under one URL, and Pixiv's
/ajax/illust/{id} response describes only the first. So a match on page
5 was shown, downloaded and compared as page 1 - the wrong picture,
presented with nothing to indicate anything was off.
"""
import io
import unittest
from unittest import mock

from PIL import Image, ImageDraw

from . import _path  # noqa: F401
from core.multipage import (
    MAX_PLAUSIBLE_DISTANCE, NEAR_EXACT_DISTANCE, check_page,
    choose_matching_page, dimensions_contradict_page, dimensions_identify_page,
    page_index_from_thumbnail, page_url_for_index,
)


def page_art(index):
    """Pages that differ the way real multi-image sets do - distinct
    layouts rather than subtle variations."""
    image = Image.new("RGB", (400, 600), (245, 240, 235))
    draw = ImageDraw.Draw(image)
    layouts = [
        lambda: draw.rectangle([20, 20, 380, 300], fill=(20, 40, 160)),
        lambda: draw.ellipse([40, 200, 360, 560], fill=(200, 60, 60)),
        lambda: [draw.rectangle([20, 40 + i * 90, 380, 100 + i * 90], fill=(30, 150, 70))
                 for i in range(5)],
        lambda: draw.polygon([(200, 30), (380, 560), (20, 560)], fill=(240, 190, 20)),
        lambda: [draw.ellipse([40 + (i % 2) * 180, 40 + (i // 2) * 180,
                               180 + (i % 2) * 180, 180 + (i // 2) * 180], fill=(120, 20, 140))
                 for i in range(4)],
    ]
    layouts[index % len(layouts)]()
    return image


def thumbnail_bytes(image, size=(250, 375)):
    buffer = io.BytesIO()
    image.resize(size, Image.LANCZOS).save(buffer, "JPEG", quality=80)
    return buffer.getvalue()


class TestChoosingThePage(unittest.TestCase):
    def setUp(self):
        self.pages = [(i, thumbnail_bytes(page_art(i))) for i in range(5)]

    def test_identifies_each_page_in_turn(self):
        for target in range(5):
            with self.subTest(page=target):
                match = choose_matching_page(page_art(target), self.pages)
                self.assertTrue(match.confident)
                self.assertEqual(match.index, target)

    def test_a_later_page_is_found_not_just_the_first(self):
        """The whole point: page 1 was always what got used."""
        match = choose_matching_page(page_art(3), self.pages)
        self.assertEqual(match.index, 3)

    def test_single_page_post_is_not_considered(self):
        self.assertIsNone(choose_matching_page(page_art(0), [(0, thumbnail_bytes(page_art(0)))]))

    def test_empty_input(self):
        self.assertIsNone(choose_matching_page(page_art(0), []))


class TestRefusingToGuess(unittest.TestCase):
    """Being wrong about the page is precisely the bug being fixed, so
    an unclear answer must leave the default alone rather than swap in a
    different wrong picture."""

    def test_indistinguishable_pages_are_not_chosen_between(self):
        identical = [(i, thumbnail_bytes(page_art(0))) for i in range(4)]
        match = choose_matching_page(page_art(0), identical)
        self.assertFalse(match.confident)
        self.assertIn("indistinguishable", match.reason)

    def test_local_image_absent_from_the_post(self):
        """A wrong match shouldn't be compounded by confidently picking a
        page from it. Measured: a genuine page scores 0-5 against its own
        thumbnail, unrelated artwork 20+."""
        pages = [(i, thumbnail_bytes(page_art(i))) for i in range(5)]
        unrelated = Image.new("RGB", (400, 600), (250, 245, 240))
        draw = ImageDraw.Draw(unrelated)
        draw.ellipse([30, 30, 370, 370], fill=(80, 140, 200))
        draw.rectangle([60, 400, 340, 560], fill=(220, 120, 40))
        draw.line([(30, 390), (370, 390)], fill=(0, 0, 0), width=6)
        match = choose_matching_page(unrelated, pages)
        self.assertFalse(match.confident)

    def test_a_featureless_local_image_is_refused(self):
        """A flat image hashes to almost no set bits, which sits
        spuriously close to everything - there's nothing to decide on."""
        pages = [(i, thumbnail_bytes(page_art(i))) for i in range(3)]
        self.assertIsNone(choose_matching_page(Image.new("RGB", (400, 600), (0, 0, 0)), pages))

    def test_unreadable_local_image(self):
        pages = [(i, thumbnail_bytes(page_art(i))) for i in range(3)]
        self.assertIsNone(choose_matching_page(b"not an image", pages))

    def test_unreadable_thumbnails(self):
        self.assertIsNone(choose_matching_page(page_art(0), [(0, b"junk"), (1, b"junk")]))


class TestPageUrlRewriting(unittest.TestCase):
    ORIGINAL = "https://i.pximg.net/img-original/img/2020/01/02/03/04/05/12345678_p0.jpg"

    def test_rewrites_to_another_page(self):
        self.assertTrue(page_url_for_index(self.ORIGINAL, 3).endswith("12345678_p3.jpg"))

    def test_page_zero_is_unchanged(self):
        self.assertEqual(page_url_for_index(self.ORIGINAL, 0), self.ORIGINAL)

    def test_only_the_last_marker_is_replaced(self):
        """REGRESSION GUARD: a directory in the path could contain "_p0"
        too, and rewriting that would break the URL entirely."""
        tricky = "https://i.pximg.net/img-original/_p0_dir/999_p0.jpg"
        self.assertEqual(page_url_for_index(tricky, 2),
                         "https://i.pximg.net/img-original/_p0_dir/999_p2.jpg")

    def test_a_url_without_the_marker_is_refused(self):
        """Better no URL than one that merely looks plausible."""
        self.assertIsNone(page_url_for_index("https://example.com/image.jpg", 2))
        self.assertIsNone(page_url_for_index("", 2))


class TestPixivPageMetadata(unittest.TestCase):
    def test_page_count(self):
        import json
        from core.boorus import pixiv
        single = json.dumps({"error": False, "body": {"pageCount": 1, "urls": {"original": "x"}}})
        multi = json.dumps({"error": False, "body": {"pageCount": 7, "urls": {"original": "x"}}})
        self.assertEqual(pixiv.parse_page_count(single, "u"), 1)
        self.assertEqual(pixiv.parse_page_count(multi, "u"), 7)

    def test_missing_page_count_means_one(self):
        import json
        from core.boorus import pixiv
        body = json.dumps({"error": False, "body": {"urls": {"original": "x"}}})
        self.assertEqual(pixiv.parse_page_count(body, "u"), 1)

    def test_pages_api_url_for_both_url_forms(self):
        from core.boorus import pixiv
        self.assertEqual(pixiv.pages_api_url("https://www.pixiv.net/artworks/12345678"),
                         "https://www.pixiv.net/ajax/illust/12345678/pages")
        self.assertEqual(
            pixiv.pages_api_url("https://www.pixiv.net/member_illust.php?mode=medium&illust_id=999"),
            "https://www.pixiv.net/ajax/illust/999/pages")

    def test_parses_the_page_list(self):
        import json
        from core.boorus import pixiv
        body = json.dumps({"error": False, "body": [
            {"urls": {"small": "s0", "original": "o0"}},
            {"urls": {"small": "s1", "original": "o1"}}]})
        pages = pixiv.parse_pages(body, "u")
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[1]["urls"]["original"], "o1")

    def test_error_response_yields_no_pages(self):
        import json
        from core.boorus import pixiv
        self.assertEqual(pixiv.parse_pages(json.dumps({"error": True}), "u"), [])


class TestPageIndexFromSauceNaoThumbnail(unittest.TestCase):
    """SauceNAO indexes Pixiv per image, so its own thumbnail URL names
    the page it matched - the answer this module otherwise pays a
    download per page to work out."""

    MANGA = "https://img1.saucenao.com/res/pixiv/8986/manga/89861006_p1.jpg?auth=abc&exp=1787684400"

    def test_reads_the_page_off_a_manga_thumbnail(self):
        """The case that started this: /ajax/illust described p0 while
        the local file was p1, and SauceNAO said so all along."""
        self.assertEqual(page_index_from_thumbnail(self.MANGA, "89861006"), 1)

    def test_double_digit_pages(self):
        url = "https://img1.saucenao.com/res/pixiv/10475/manga/104759687_p32.jpg"
        self.assertEqual(page_index_from_thumbnail(url, "104759687"), 32)

    def test_single_page_thumbnail_form(self):
        url = "https://img1.saucenao.com/res/pixiv/7305/73053388_p0_master1200.jpg"
        self.assertEqual(page_index_from_thumbnail(url, "73053388"), 0)

    def test_historical_index_is_read_too(self):
        url = "https://img1.saucenao.com/res/pixiv_historical/460/manga/4600491_p2.jpg"
        self.assertEqual(page_index_from_thumbnail(url, "4600491"), 2)

    def test_a_different_illust_id_is_refused(self):
        """REGRESSION GUARD: the index only means anything if the
        thumbnail is of the artwork we're actually looking at. Across
        1473 real results the two always agreed, so a disagreement means
        something unmodelled - not something to act on."""
        self.assertIsNone(page_index_from_thumbnail(self.MANGA, "12345678"))

    def test_old_thumbnails_carry_no_page(self):
        """The _s/_m forms name no page at all. No marker, no claim."""
        self.assertIsNone(
            page_index_from_thumbnail("https://img1.saucenao.com/res/pixiv/972/9725787_s.jpg",
                                      "9725787"))
        self.assertIsNone(
            page_index_from_thumbnail("https://img1.saucenao.com/res/pixiv/3446/34466861_m.jpg",
                                      "34466861"))

    def test_only_saucenao_pixiv_thumbnails(self):
        """IQDB's thumbnails are a different host and say nothing about
        pages; SauceNAO's booru thumbnails are content-hashed."""
        self.assertIsNone(page_index_from_thumbnail("https://iqdb.org/thumbs/1_p3.jpg", "1"))
        self.assertIsNone(page_index_from_thumbnail(
            "https://img3.saucenao.com/booru/f/f/ffa93b4481cfbaffdee9406bd572d20b_2.jpg", "1"))
        self.assertIsNone(page_index_from_thumbnail(
            "https://evil.example.com/res/pixiv/1/manga/89861006_p1.jpg", "89861006"))

    def test_missing_inputs(self):
        self.assertIsNone(page_index_from_thumbnail(None, "89861006"))
        self.assertIsNone(page_index_from_thumbnail("", "89861006"))
        self.assertIsNone(page_index_from_thumbnail(self.MANGA, None))


class TestDimensionsAsConfirmation(unittest.TestCase):
    """Pixiv's page list carries every page's size, so an exact unique
    hit settles which page it is without downloading anything."""

    PAGES = [(3474, 6642), (1986, 2938), (800, 600)]

    def test_a_unique_exact_size_identifies_the_page(self):
        self.assertEqual(dimensions_identify_page((1986, 2938), self.PAGES), 1)

    def test_a_size_shared_by_two_pages_identifies_nothing(self):
        pages = [(1000, 1000), (1000, 1000), (800, 600)]
        self.assertIsNone(dimensions_identify_page((1000, 1000), pages))

    def test_a_resized_local_file_identifies_nothing(self):
        """No exact hit is not a contradiction - it just means the
        dimensions can't answer and something else has to."""
        self.assertIsNone(dimensions_identify_page((993, 1469), self.PAGES))

    def test_unknown_local_size(self):
        self.assertIsNone(dimensions_identify_page(None, self.PAGES))


class TestDimensionsAsRefutation(unittest.TestCase):
    """Aspect ratio, not exact size, because the local file is routinely
    a resized copy."""

    def test_a_resize_of_the_named_page_is_not_contradicted(self):
        pages = [(3474, 6642), (1986, 2938)]
        self.assertFalse(dimensions_contradict_page((993, 1469), pages, 1))

    def test_a_portrait_local_file_contradicts_a_landscape_page(self):
        """REGRESSION GUARD, from a real result: SauceNAO named a page
        with a 0.574 ratio for a local file at 1.157. Whatever it says,
        that is not the page this file came from."""
        pages = [(800, 1000), (574, 1000)]
        self.assertTrue(dimensions_contradict_page((1157, 1000), pages, 1))

    def test_the_widest_confirmed_real_case_still_passes(self):
        """4.4% out, and independently confirmed correct - the tolerance
        exists to clear cases like this one."""
        pages = [(1000, 1000), (676, 1000)]
        self.assertFalse(dimensions_contradict_page((707, 1000), pages, 1))

    def test_unknown_sizes_never_contradict(self):
        self.assertFalse(dimensions_contradict_page(None, [(1, 1), (2, 2)], 1))
        self.assertFalse(dimensions_contradict_page((10, 10), [(1, 1), (None, None)], 1))
        self.assertFalse(dimensions_contradict_page((10, 10), [(1, 1)], 5))


class TestTiesAreMarked(unittest.TestCase):
    """A caller holding a second opinion needs to tell "these pages are
    the same picture" from "none of them look like this at all"."""

    def test_indistinguishable_pages_are_flagged(self):
        art = page_art(0)
        pages = [(0, thumbnail_bytes(art)), (1, thumbnail_bytes(art))]
        match = choose_matching_page(art, pages)
        self.assertFalse(match.confident)
        self.assertTrue(match.indistinguishable)

    def test_a_post_without_the_image_is_not_flagged(self):
        pages = [(i, thumbnail_bytes(page_art(i))) for i in range(3)]
        stranger = Image.new("RGB", (400, 600), (255, 255, 255))
        ImageDraw.Draw(stranger).polygon([(0, 0), (400, 0), (0, 600)], fill=(10, 10, 10))
        match = choose_matching_page(stranger, pages)
        self.assertFalse(match.confident)
        self.assertFalse(match.indistinguishable)

    def test_a_confident_match_is_not_flagged(self):
        pages = [(i, thumbnail_bytes(page_art(i))) for i in range(5)]
        match = choose_matching_page(page_art(3), pages)
        self.assertTrue(match.confident)
        self.assertFalse(match.indistinguishable)


class TestCheckingOnePage(unittest.TestCase):
    """Asking "is it THIS page?" costs one download whatever the post's
    size, where asking "which page is it?" costs one per page."""

    def test_the_right_page_is_confirmed(self):
        art = page_art(2)
        check = check_page(art, thumbnail_bytes(art))
        self.assertTrue(check.confirmed)
        self.assertLessEqual(check.distance, 5)

    def test_a_different_page_is_refused(self):
        check = check_page(page_art(2), thumbnail_bytes(page_art(4)))
        self.assertFalse(check.confirmed)
        self.assertGreater(check.distance, 5)

    def test_plausible_is_not_good_enough(self):
        """There's no runner-up here to be better than, so a merely
        close reading isn't evidence - only a near-exact one is."""
        self.assertGreater(MAX_PLAUSIBLE_DISTANCE, NEAR_EXACT_DISTANCE)
        with mock.patch("core.multipage.hamming_distance",
                        return_value=NEAR_EXACT_DISTANCE + 1):
            self.assertFalse(check_page(page_art(0), thumbnail_bytes(page_art(0))).confirmed)

    def test_a_featureless_local_image_confirms_nothing(self):
        """REGRESSION GUARD: a near-flat image hashes to almost no set
        bits and lands close to everything, so it would confirm any page
        put in front of it."""
        flat = Image.new("RGB", (400, 600), (128, 128, 128))
        check = check_page(flat, thumbnail_bytes(flat))
        self.assertFalse(check.confirmed)
        self.assertIn("featureless", check.reason)

    def test_unreadable_images(self):
        self.assertFalse(check_page(b"not an image", thumbnail_bytes(page_art(0))).confirmed)
        self.assertFalse(check_page(page_art(0), b"junk").confirmed)


class TestResolvingAMultipageCandidate(unittest.TestCase):
    """The wiring: two independent signals, and which one wins when they
    disagree."""

    ARTWORK = "https://www.pixiv.net/artworks/89861006"

    def _pages_json(self, sizes):
        import json
        return json.dumps({"error": False, "body": [
            {"urls": {
                "small": f"https://i.pximg.net/c/540x540_70/89861006_p{i}_master1200.jpg",
                "regular": f"https://i.pximg.net/img-master/89861006_p{i}_master1200.jpg",
                "original": f"https://i.pximg.net/img-original/89861006_p{i}.jpg"},
             "width": w, "height": h}
            for i, (w, h) in enumerate(sizes)]})

    def _candidate(self, thumb_page=None):
        from core.models import MatchCandidate
        thumb = (f"https://img1.saucenao.com/res/pixiv/8986/manga/89861006_p{thumb_page}.jpg"
                 if thumb_page is not None else
                 "https://iqdb.org/thumbs/2021/nope.jpg")
        return MatchCandidate(
            url=self.ARTWORK, source_name="Pixiv", engine="SauceNAO", thumb_url=thumb,
            direct_file_url="https://i.pximg.net/img-original/89861006_p0.jpg",
            preview_url="https://i.pximg.net/img-master/89861006_p0_master1200.jpg",
            width=3474, height=6642,
        )

    def _resolve(self, candidate, sizes, local_size, page_thumbs=None, page_count=None,
                 local_source="/local/file.jpg"):
        """Runs the resolver with the network and the local file stubbed."""
        from unittest import mock
        from core import search_engine
        from core.config import Settings

        thumbs = page_thumbs or {}
        with mock.patch.object(search_engine.remote, "fetch_text",
                               return_value=self._pages_json(sizes)) as fetch, \
             mock.patch.object(search_engine.multipage_resolve, "image_dimensions", return_value=local_size), \
             mock.patch.object(search_engine.remote, "download_bytes",
                               side_effect=lambda url, *a, **k: thumbs.get(url)) as download:
            search_engine._resolve_multipage_candidate(
                candidate, page_count or len(sizes), Settings(), local_source,
            )
        return fetch, download

    def _page_thumbs(self, images):
        return self._page_thumbs_at(dict(enumerate(images)))

    def _page_thumbs_at(self, images_by_index):
        return {f"https://i.pximg.net/c/540x540_70/89861006_p{i}_master1200.jpg":
                thumbnail_bytes(image) for i, image in images_by_index.items()}

    def test_dimensions_confirm_saucenaos_page_without_downloading_anything(self):
        """The cheap path: one page is exactly the local file's size and
        it's the one SauceNAO named, so nothing needs hashing."""
        candidate = self._candidate(thumb_page=1)
        _, download = self._resolve(
            candidate, [(3474, 6642), (1986, 2938)], local_size=(1986, 2938),
        )
        self.assertEqual(candidate.page_index, 1)
        self.assertEqual(candidate.direct_file_url,
                         "https://i.pximg.net/img-original/89861006_p1.jpg")
        download.assert_not_called()

    def test_the_chosen_pages_own_dimensions_are_kept(self):
        """The artwork record's dimensions describe page 1. The page list
        has this page's real ones, so the size comparison keeps working
        instead of going blank."""
        candidate = self._candidate(thumb_page=1)
        self._resolve(candidate, [(3474, 6642), (1986, 2938)], local_size=(1986, 2938))
        self.assertEqual((candidate.width, candidate.height), (1986, 2938))

    def test_hashing_overrules_a_stale_saucenao_index(self):
        """REGRESSION GUARD, from a real result: an edited post left
        SauceNAO naming page 5 of a four-page post where the images said
        page 4. Hashing is authoritative wherever it has an answer."""
        candidate = self._candidate(thumb_page=2)
        self._resolve(candidate, [(900, 600)] * 3, local_size=(450, 300),
                      page_thumbs=self._page_thumbs([page_art(i) for i in range(3)]),
                      local_source=page_art(1))
        self.assertEqual(candidate.page_index, 1)
        self.assertEqual(candidate.direct_file_url,
                         "https://i.pximg.net/img-original/89861006_p1.jpg")

    def test_an_unhashable_local_file_falls_back_to_saucenao(self):
        """Nothing to compare against - a thumbnail that wouldn't
        download, say. The named page is bounds- and shape-checked, and
        page 1 is otherwise certain to be wrong."""
        candidate = self._candidate(thumb_page=2)
        self._resolve(candidate, [(900, 600)] * 3, local_size=(450, 300),
                      local_source="/does/not/exist.jpg")
        self.assertEqual(candidate.page_index, 2)

    def test_a_page_beyond_the_post_is_ignored(self):
        """REGRESSION GUARD, from a real result: SauceNAO named page 5 of
        a post that now has four. Bounds-check before believing it."""
        candidate = self._candidate(thumb_page=4)
        self._resolve(candidate, [(900, 600)] * 4, local_size=(450, 300))
        self.assertEqual(candidate.page_index, 0)
        self.assertEqual(candidate.direct_file_url,
                         "https://i.pximg.net/img-original/89861006_p0.jpg")

    def test_a_page_of_the_wrong_shape_is_ignored(self):
        candidate = self._candidate(thumb_page=1)
        self._resolve(candidate, [(1000, 1000), (574, 1000)], local_size=(1157, 1000))
        self.assertEqual(candidate.page_index, 0)

    def test_saucenao_breaks_a_tie_hashing_refuses(self):
        """The gap this fills: near-identical pages that hashing will not
        choose between, where SauceNAO already knows the answer. Two real
        results in the sample were exactly this."""
        art = page_art(0)
        candidate = self._candidate(thumb_page=1)
        self._resolve(candidate, [(900, 600)] * 2, local_size=(450, 300),
                      page_thumbs=self._page_thumbs([art, art]), local_source=art)
        self.assertEqual(candidate.page_index, 1)

    def test_a_post_that_doesnt_contain_the_image_is_left_alone(self):
        """When nothing in the post resembles the local file the match
        itself is suspect, and a page number from anywhere is a guess.
        Measured: these refusals were almost all matches SauceNAO itself
        scored around 60% similar, and its page was often wrong too."""
        stranger = Image.new("RGB", (400, 600), (255, 255, 255))
        ImageDraw.Draw(stranger).polygon([(0, 0), (400, 0), (0, 600)], fill=(10, 10, 10))
        candidate = self._candidate(thumb_page=2)
        self._resolve(candidate, [(900, 600)] * 3, local_size=(450, 300),
                      page_thumbs=self._page_thumbs([page_art(i) for i in range(3)]),
                      local_source=stranger)
        self.assertEqual(candidate.page_index, 0)

    def test_a_huge_post_is_resolvable_when_saucenao_names_the_page(self):
        """Posts past the hashing cap used to be abandoned outright. A
        tenth of the real multi-page results measured sat beyond it."""
        sizes = [(3474, 6642)] * 40
        sizes[32] = (1986, 2938)
        candidate = self._candidate(thumb_page=32)
        _, download = self._resolve(candidate, sizes, local_size=(1986, 2938), page_count=40)
        self.assertEqual(candidate.page_index, 32)
        download.assert_not_called()

    def test_a_huge_post_with_nothing_naming_a_page_keeps_the_first(self):
        """No named page and no size that singles one out - nothing to
        check, and 40 pages is too many to compare. The page list is
        still fetched: it's one call, and it's what would have found a
        page to check."""
        candidate = self._candidate(thumb_page=None)
        _, download = self._resolve(
            candidate, [(3474, 6642)] * 40, local_size=(999, 888), page_count=40,
        )
        self.assertEqual(candidate.page_index, 0)
        download.assert_not_called()

    def test_a_huge_posts_named_page_is_checked_on_its_own(self):
        """REGRESSION GUARD, from a real 111-image post: every page the
        same size, so nothing could be confirmed from the page list, and
        all four matches were abandoned on page 1. Checking just the
        named page costs one download whatever the post's size."""
        art = page_art(3)
        candidate = self._candidate(thumb_page=54)
        _, download = self._resolve(
            candidate, [(800, 1122)] * 111, local_size=(800, 1122), page_count=111,
            page_thumbs=self._page_thumbs_at({54: art}), local_source=art,
        )
        self.assertEqual(candidate.page_index, 54)
        self.assertEqual(candidate.direct_file_url,
                         "https://i.pximg.net/img-original/89861006_p54.jpg")
        self.assertEqual(download.call_count, 1)

    def test_a_huge_posts_named_page_can_be_refuted(self):
        """The check has to be able to say no, or it's not a check. With
        no runner-up to be better than, the reading stands alone - so
        anything short of near-exact keeps page 1."""
        candidate = self._candidate(thumb_page=54)
        _, download = self._resolve(
            candidate, [(800, 1122)] * 111, local_size=(800, 1122), page_count=111,
            page_thumbs=self._page_thumbs_at({54: page_art(1)}), local_source=page_art(3),
        )
        self.assertEqual(candidate.page_index, 0)
        self.assertEqual(download.call_count, 1)

    def test_a_huge_post_falls_back_to_a_page_singled_out_by_size(self):
        """No SauceNAO index - an IQDB match, say - but one page is the
        local file's exact size, which is enough to name a page worth
        checking."""
        art = page_art(2)
        sizes = [(3474, 6642)] * 60
        sizes[41] = (1986, 2938)
        candidate = self._candidate(thumb_page=None)
        self._resolve(candidate, sizes, local_size=(1986, 2938), page_count=60,
                      page_thumbs=self._page_thumbs_at({41: art}), local_source=art)
        self.assertEqual(candidate.page_index, 41)

    def test_a_huge_post_checks_the_named_page_before_the_size_match(self):
        """Both signals available and disagreeing: SauceNAO's page is
        checked first, and confirming it ends the matter."""
        art = page_art(4)
        sizes = [(800, 1122)] * 111
        sizes[7] = (1986, 2938)
        candidate = self._candidate(thumb_page=54)
        _, download = self._resolve(
            candidate, sizes, local_size=(800, 1122), page_count=111,
            page_thumbs=self._page_thumbs_at({54: art, 7: page_art(1)}), local_source=art,
        )
        self.assertEqual(candidate.page_index, 54)
        self.assertEqual(download.call_count, 1)

    def test_a_featureless_local_image_confirms_nothing(self):
        """A near-flat image sits spuriously close to everything, so it
        would 'confirm' whatever page it was shown."""
        flat = Image.new("RGB", (400, 600), (128, 128, 128))
        candidate = self._candidate(thumb_page=54)
        self._resolve(candidate, [(800, 1122)] * 111, local_size=(800, 1122), page_count=111,
                      page_thumbs=self._page_thumbs_at({54: flat}), local_source=flat)
        self.assertEqual(candidate.page_index, 0)

    def test_an_iqdb_candidate_is_unaffected(self):
        """IQDB thumbnails carry no page, so this is the old path exactly:
        hash, and keep page 1 if that can't decide."""
        candidate = self._candidate(thumb_page=None)
        self._resolve(candidate, [(900, 600)] * 2, local_size=(450, 300))
        self.assertEqual(candidate.page_index, 0)


class TestResolvingAMultiPhotoTweet(unittest.TestCase):
    """REGRESSION (DAN-57): a tweet can hold four images and the app
    always showed the first.

    core/boorus/twitter.py exposed no page-list hooks, so every
    multi-photo tweet fell straight through the resolver and kept photo
    1 - shown, downloaded and compared as the match. The `/photo/N`
    suffix the parser reads was no help: SauceNAO's result URLs carry
    none, and the API's own media facets name /photo/1 for every photo in
    the tweet.

    Shapes below are the live api.fxtwitter.com response for the reported
    tweet, 2011168979038142698 - four photos, whose fourth is byte-for-
    byte the local file the board reported as the match.
    """

    TWEET = "https://x.com/DARKMETAKNIGH12/status/2011168979038142698"
    MEDIA = [
        ("G-kbgPfa0AAXsZh", 1216, 832),
        ("G-kbgizbQAAakdJ", 1365, 2048),
        ("G-kbhHJaoAAgMek", 2048, 2048),
        ("G-kbhl1bAAAxpzn", 1171, 2048),
    ]

    def _api_body(self, media=None):
        import json
        return json.dumps({"code": 200, "message": "OK", "tweet": {
            "id": "2011168979038142698",
            "author": {"screen_name": "DARKMETAKNIGH12"},
            "media": {"photos": [
                {"type": "photo", "width": w, "height": h,
                 "url": f"https://pbs.twimg.com/media/{name}.jpg?name=orig"}
                for name, w, h in (media if media is not None else self.MEDIA)]},
        }})

    def _candidate(self):
        """As it arrives from SauceNAO: pointed at photo 1, because that is
        all parse_file_url can offer without a page list."""
        from core.models import MatchCandidate
        first, width, height = self.MEDIA[0]
        return MatchCandidate(
            url=self.TWEET, source_name="Twitter", engine="SauceNAO",
            thumb_url="https://img1.saucenao.com/userdata/12345.jpg",
            direct_file_url=f"https://pbs.twimg.com/media/{first}.jpg?name=orig",
            preview_url=f"https://pbs.twimg.com/media/{first}.jpg?name=large",
            width=width, height=height,
        )

    def _photo_thumbs(self, images_by_index):
        return {f"https://pbs.twimg.com/media/{self.MEDIA[i][0]}.jpg?name=small":
                thumbnail_bytes(image) for i, image in images_by_index.items()}

    def _resolve(self, candidate, local_source, local_size, photo_thumbs, media=None,
                 page_info=None):
        from unittest import mock
        from core import search_engine
        from core.config import Settings

        with mock.patch.object(search_engine.remote, "fetch_text",
                               return_value=self._api_body(media)) as fetch, \
             mock.patch.object(search_engine.multipage_resolve, "image_dimensions",
                               return_value=local_size), \
             mock.patch.object(search_engine.remote, "download_bytes",
                               side_effect=lambda url, *a, **k: photo_thumbs.get(url)):
            search_engine._resolve_multipage_candidate(
                candidate, len(media if media is not None else self.MEDIA), Settings(),
                local_source, page_info,
            )
        return fetch

    def test_the_fourth_photo_is_used_when_it_is_the_one_that_matched(self):
        """The reported bug, end to end: photo 4 of 4 was shown as photo 1."""
        candidate = self._candidate()
        self._resolve(candidate, page_art(3), (1171, 2048),
                      self._photo_thumbs({i: page_art(i) for i in range(4)}))
        self.assertEqual(candidate.page_index, 3)
        self.assertEqual(candidate.direct_file_url,
                         "https://pbs.twimg.com/media/G-kbhl1bAAAxpzn.jpg?name=orig")
        self.assertEqual(candidate.preview_url,
                         "https://pbs.twimg.com/media/G-kbhl1bAAAxpzn.jpg?name=large")
        self.assertEqual((candidate.width, candidate.height), (1171, 2048))

    def test_each_photo_is_found_in_turn(self):
        """Acceptance 3: two matches on one tweet each point at their own
        image, because the photo is decided per candidate."""
        thumbs = self._photo_thumbs({i: page_art(i) for i in range(4)})
        for index, (name, width, height) in enumerate(self.MEDIA):
            with self.subTest(photo=index):
                candidate = self._candidate()
                self._resolve(candidate, page_art(index), (width, height), thumbs)
                self.assertEqual(candidate.page_index, index)
                self.assertEqual(candidate.direct_file_url,
                                 f"https://pbs.twimg.com/media/{name}.jpg?name=orig")

    def test_a_single_photo_tweet_is_left_alone(self):
        """Acceptance 2: a one-photo tweet keeps exactly what the parser
        gave it, and not one thumbnail is compared. (fetch_candidate_details
        never even calls the resolver for one - it gates on page_count > 1 -
        but the resolver must be harmless if it does.)"""
        candidate = self._candidate()
        before = (candidate.direct_file_url, candidate.preview_url,
                  candidate.width, candidate.height)
        self._resolve(candidate, page_art(0), (1216, 832), {}, media=self.MEDIA[:1])
        self.assertEqual(candidate.page_index, 0)
        self.assertEqual((candidate.direct_file_url, candidate.preview_url,
                          candidate.width, candidate.height), before)

    def test_a_tweet_none_of_whose_photos_match_keeps_the_first(self):
        """Being wrong about this is the bug; the first photo is at least
        a predictable default."""
        candidate = self._candidate()
        self._resolve(candidate, page_art(4), (1216, 832),
                      self._photo_thumbs({i: page_art(i) for i in range(4)}))
        self.assertEqual(candidate.page_index, 0)
        self.assertEqual(candidate.direct_file_url,
                         "https://pbs.twimg.com/media/G-kbgPfa0AAXsZh.jpg?name=orig")

    def test_a_url_naming_a_photo_is_overruled_by_the_images(self):
        """REGRESSION GUARD: the resolver returned early on photo 1, so a
        candidate the URL had already pointed at photo 3 stayed there even
        once the images said photo 1."""
        candidate = self._candidate()
        third, width, height = self.MEDIA[2]
        candidate.direct_file_url = f"https://pbs.twimg.com/media/{third}.jpg?name=orig"
        candidate.preview_url = f"https://pbs.twimg.com/media/{third}.jpg?name=large"
        candidate.width, candidate.height = width, height
        self._resolve(candidate, page_art(0), (1216, 832),
                      self._photo_thumbs({i: page_art(i) for i in range(4)}))
        self.assertEqual(candidate.page_index, 0)
        self.assertEqual(candidate.direct_file_url,
                         "https://pbs.twimg.com/media/G-kbgPfa0AAXsZh.jpg?name=orig")
        self.assertEqual((candidate.width, candidate.height), (1216, 832))

    def test_the_api_record_already_read_is_not_fetched_again(self):
        """The photo list IS the record the tags came from."""
        from core.boorus import BooruPageInfo
        candidate = self._candidate()
        page_info = BooruPageInfo(
            fetched_url="https://api.fxtwitter.com/status/2011168979038142698",
            body=self._api_body(), page_count=4,
        )
        fetch = self._resolve(candidate, page_art(3), (1171, 2048),
                              self._photo_thumbs({i: page_art(i) for i in range(4)}),
                              page_info=page_info)
        self.assertEqual(candidate.page_index, 3)
        fetch.assert_not_called()


class TestPageListRequestHeaders(unittest.TestCase):
    """REGRESSION GUARD: the page list is fetched by fetch_text, not by a
    parser, and it used to send the plain hatate User-Agent. Combined
    with a Pixiv session cookie that draws a Cloudflare challenge - HTTP
    403 - so every multi-page Pixiv post silently kept page 1, which is
    the exact bug the page list exists to fix.
    """

    def _capture(self, url):
        from unittest import mock
        from core import search_engine
        from core.config import Settings

        response = mock.Mock(status_code=200, text="{}")
        with mock.patch.object(__import__("requests").Session, "get", return_value=response) as get:
            search_engine.fetch_text(url, Settings())
        return get.call_args.kwargs["headers"]

    def test_pixiv_page_list_is_fetched_with_the_browser_user_agent(self):
        headers = self._capture("https://www.pixiv.net/ajax/illust/89861006/pages")
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertEqual(headers["Referer"], "https://www.pixiv.net/")

    def test_an_explicit_referer_still_wins(self):
        from unittest import mock
        from core import search_engine
        from core.config import Settings

        response = mock.Mock(status_code=200, text="{}")
        with mock.patch.object(__import__("requests").Session, "get", return_value=response) as get:
            search_engine.fetch_text(
                "https://www.pixiv.net/ajax/illust/1/pages", Settings(),
                referer="https://www.pixiv.net/artworks/1",
            )
        self.assertEqual(get.call_args.kwargs["headers"]["Referer"],
                         "https://www.pixiv.net/artworks/1")

    def test_other_sites_keep_the_honest_user_agent(self):
        headers = self._capture("https://danbooru.donmai.us/posts/1.json")
        from core import net
        self.assertEqual(headers["User-Agent"], net.USER_AGENT)


if __name__ == "__main__":
    unittest.main()
