# Tests

Run them with:

```bash
bash run_tests.sh              # everything
python3 -m unittest discover -s tests -t .        # same, directly
python3 -m unittest tests.test_upscale_detect     # one module
```

## A note on what compiles vs what runs

`python3 -m compileall` passing means the syntax is valid — nothing
more. Python resolves global names when a line actually executes, so a
missing import compiles perfectly and crashes the first time someone
presses the button. `test_no_undefined_names` walks every module in
`core/`, `gui/` and `workers/` for names used but never imported or
defined, which catches that class without needing PyQt6 installed.

## What's here, and why

These aren't speculative tests. Nearly every case corresponds to a bug
that actually occurred while building this app — several of which
produced *confidently wrong results* rather than visible failures, which
is exactly the kind that survives manual testing.

Cases marked `REGRESSION:` in their docstring name the specific bug:

| Bug | Test module |
|---|---|
| `LRUCache` had `__setitem__` but no `__getitem__`, crashing on thumbnail load | `test_core_utils` |
| `ThreadPoolExecutor` shutdown hook made a hung request freeze app exit | `test_core_utils` |
| SauceNAO returned a quota field as `str` while another was `int`, crashing on subtraction | `test_core_utils` |
| Hydrus silently failed on Pixiv's legacy `member_illust.php` URLs | `test_urls_and_availability` |
| Pixiv's CDN needs the bare origin as Referer, not the artwork page | `test_urls_and_availability` |
| Pixiv preload extraction used a regex that broke on any markup variation | `test_parsers_and_tags` |
| `fetch_page_info` returned empty-without-raising for unparsed sites, read as "confirmed alive" | `test_parsers_and_tags` |
| Namespace-strip produced `""` instead of `None`, silently breaking dedup | `test_parsers_and_tags` |
| Upscale check summed the RGB histogram wrongly, producing values above 255 | `test_upscale_detect` |
| Upscale check averaged over flat regions, false-positiving on anime art | `test_upscale_detect` |
| Settings field added to a tab whose layout had a different name | `test_gui_smoke` |
| Thumbnail handler crashed on a cache that lacked `__getitem__` | `test_gui_smoke` |
| SauceNAO mixed str/int quota values crashed a signal handler | `test_gui_smoke` |
| Pixiv stopped embedding page data; moved to the AJAX API | `test_parsers_and_tags` |
| Blacklist `meta:*` must not swallow unnamespaced tags (no colon to match) | `test_parsers_and_tags` |
| Blacklist patterns copied from Hydrus (`bad id`) must match booru spelling (`bad_id`) | `test_parsers_and_tags` |
| e-shuushuu's per-tag `/tags/{id}` jump link must not be scraped as a second tag | `test_parsers_and_tags` |
| Gallery/auth-gated sites must stay parser-less (deliberate, not forgotten) | `test_parsers_and_tags` |
| e-shuushuu artist named `Fagi of Note` must not be truncated at the `of` | `test_parsers_and_tags` |
| `by Fagimoto` must not make a separate `Fagi` tag a creator | `test_parsers_and_tags` |
| `hydrus_hash` is every file's SHA256, so "sent" must not be derived from it | `test_session` |
| URL-importer "accepted" must not be reported as a confirmed import | `test_session` |
| SauceNAO's ~30s burst limit must not be mistaken for the daily quota | `test_core_utils` |
| Quota exhaustion must not pause a batch when SauceNAO isn't in the engine order | `test_core_utils` |
| Files merely *named* like hashes must not be trusted without verification | `test_core_utils` |
| A hash that doesn't match the local file must not import another file's tags | `test_core_utils` |
| `use_filename_hashes=False` must migrate to `hash_source="local"`, not silently re-enable | `test_core_utils` |
| `/get_files/thumbnail` never 404s, so unknown hashes must be filtered first | `test_core_utils` |
| Qt's `rowAt()` returns -1 past the last row; treating it as an error blanks those rows | `test_core_utils` |
| The autosave timer must never write over a session the user saved by name | `test_session` |
| `file_missing` must be re-derived on load, not persisted (files come back from Hydrus's trash) | `test_session` |
| Perceptual match must survive a 2x resolution change and JPEG re-encoding | `test_image_compare` |
| A bigger-but-reshaped match must not be presented as a clean upgrade | `test_image_compare` |
| Danbooru's downscaled sample must never be returned as the full-resolution file | `test_parsers_and_tags` |
| Cache entries predating a field must re-fetch it, not restore as "already fetched" | `test_parsers_and_tags` |
| A SauceNAO result mirrored across sites must yield one candidate per site | `test_parsers_and_tags` |
| The site label must follow the match URL, never SauceNAO's index name | `test_parsers_and_tags` |
| Sankaku redirects dead posts to `/posts/show_empty` with HTTP 200, not a 404 | `test_urls_and_availability` |
| The manual availability check must send login cookies, or it deletes posts a search kept | `test_urls_and_availability` |
| Gelbooru redirects deleted posts to the post list (HTTP 200, valid page) | `test_urls_and_availability` |
| A Gelbooru list URL contains `s=list` already, so it must not flag itself as gone | `test_urls_and_availability` |
| Sankaku redirects gone posts to `/errors/not_found` on a *different subdomain* | `test_urls_and_availability` |
| Gelbooru's `Size:` separator may be `×` not `x`; matching only `x` yields no dimensions | `test_parsers_and_tags` |
| Moebooru's page carries `sample_width` too; reporting it inverts the size comparison | `test_parsers_and_tags` |
| A restored session has no thumbnail bytes, so selecting a row must fetch them | `test_session` |
| Sankaku's index `/en/posts` is a prefix of a real post `/en/posts/{id}` — substring matching would delete every good match | `test_urls_and_availability` |
| "Opposite engine" refused unsearched rows outright instead of trying the untried engine | `test_core_utils` |
| Size Difference must sort by ratio — as text, `+ 10x` precedes `+ 2x` | `test_image_compare` |
| A saved column layout with an *unrecorded* count is the stale case, not an exemption | `test_core_utils` |
| Quality ranking must never overturn a clearly better similarity | `test_ranking` |
| A candidate must not be demoted for dimensions its engine simply doesn't report | `test_ranking` |
| A multi-image Pixiv post must resolve to the matched page, not page 1 | `test_multipage` |
| Page matching must refuse unrelated images — measured: genuine 0-5, unrelated 20+ | `test_multipage` |
| Yande.re keeps a deleted post's whole page at HTTP 200 — only one sentence differs | `test_urls_and_availability` |
| Every Moebooru page carries hidden login template text; a marker from it would condemn all matches | `test_urls_and_availability` |
| A duplicate dict key silently discards the earlier entry | `test_no_undefined_names` |
| Cookie rows copied from a browser panel are newline-separated, not `;`-separated | `test_urls_and_availability` |
| A truncated session cookie fails silently, so the paste is checked for an ellipsis | `test_urls_and_availability` |
| `QApplication` used without being imported — compiles fine, crashes on click | `test_no_undefined_names` |
| SauceNAO returns Danbooru's legacy `/post/show/{id}`; matching only `/posts/{id}` disabled the JSON API, and with it Gold detection | `test_parsers_and_tags` |
| Danbooru Gold-only posts return a full record with no `file_url`, not an error | `test_parsers_and_tags` |
| Hydrus was handed an AJAX endpoint instead of an artwork page | `test_urls_and_availability` |
| `t.co` as a substring classified `anilist.co` as Twitter | `test_search_quality` |
| ascii2d's first item-box is the query image and has no source links | `test_search_quality` |
| trace.moe similarity is 0–1 and must not be treated as already-percent | `test_search_quality` |
| Anime-Pictures file URLs must not be a guessed CDN host | `test_search_quality` |
| rule34.xxx Data API returns a bare list, not Gelbooru's `{post: [...]}` wrap | `test_search_quality` |
| IQDB 3D thumbnails must join against 3d.iqdb.org, not iqdb.org | `test_search_quality` |

## GUI smoke tests

`test_gui_smoke.py` constructs the real `MainWindow` and `SettingsDialog`
and fires the real signal handlers. It exists because the core suite
cannot catch a whole class of bug: code that compiles fine and only fails
when a dialog opens or a signal fires. Two such crashes shipped during
development (`form` vs `layout` NameError; `LRUCache` not subscriptable),
and a single construct-and-fire pass would have caught both.

These require PyQt6 and are **skipped automatically** when it isn't
installed, so the rest of the suite still runs anywhere. They use Qt's
`offscreen` platform, so no display is needed - `run_tests.sh` sets that
for you.

They also redirect `XDG_CONFIG_HOME` to a temporary directory, so running
them never reads or overwrites your real settings file (which holds your
Hydrus access key and Pixiv session cookie).

## Design notes

- **No PyQt6 required.** Tests cover `core/` only, so they run in a plain
  Python environment. GUI wiring is verified separately by static checks.
- **No network.** Every HTTP interaction is mocked. Tests must never
  depend on a live site's current behaviour.
- **Availability rules matter most.** The tests around 404/410 vs
  403/5xx/timeout encode a deliberate policy: only a *definite* gone
  response may drop a match. Loosening that would discard good matches on
  a network hiccup, so those tests should be treated as a specification,
  not an implementation detail.
