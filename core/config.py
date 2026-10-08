"""Persistent settings, stored as JSON under ~/.config/hatate-linux/config.json.

Mirrors the various settings windows from the original Hatate program
(General, SauceNAO, Hydrus, delay, "better match" conditions, tag sources).
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field
from typing import Dict, List

from .sites import ALL_SITE_OPTIONS, OTHER_SITE
from .applog import get_logger
from .paths import CONFIG_DIR, CONFIG_FILE, DEFAULT_LOG_FILE

# The previous config, kept on every save. Restoring is a file copy.
CONFIG_BACKUP_FILE = CONFIG_FILE.with_name(CONFIG_FILE.name + ".bak")
# Where an unreadable config is moved so a later save cannot replace it.
CONFIG_BROKEN_FILE = CONFIG_FILE.with_name(CONFIG_FILE.name + ".unreadable")


def _preserve_unreadable_config() -> None:
    """Keeps a config that could not be parsed, out of the way.

    Without this the next save writes over it, and whatever keys and
    cookies it held are gone for good. Renaming costs nothing and leaves
    the user something to open and copy out of.
    """
    if not CONFIG_FILE.exists():
        return
    try:
        os.replace(CONFIG_FILE, CONFIG_BROKEN_FILE)
        log.warning("Kept the unreadable config as %s - your keys and cookies are "
                    "still in it", CONFIG_BROKEN_FILE)
    except OSError as exc:
        log.warning("Could not set aside the unreadable config %s: %s", CONFIG_FILE, exc)

log = get_logger("config")

# This file holds the Hydrus access key, the SauceNAO API key, and whole
# logged-in session cookies for Pixiv, Sankaku, DeviantArt and
# Anime-Pictures. Those cookies are passwords in every sense that
# matters - anyone who can read them is logged in as the user - so the
# file is kept readable by its owner alone, and the directory with it
# (the session database records every file path the user has tagged).
_FILE_MODE = 0o600
_DIR_MODE = 0o700


def _restrict(path, mode: int) -> None:
    """Tightens permissions, tolerating filesystems that cannot.

    A config on a mount without POSIX permissions (a FAT-formatted drive,
    some network shares) raises here. That is worth a warning, since the
    secrets really are exposed, but never worth refusing to save and
    losing the user's settings over.
    """
    try:
        os.chmod(path, mode)
    except OSError as exc:
        log.warning(
            "Could not restrict permissions on %s (%s) - it may be readable by other "
            "users on this machine", path, exc,
        )

# Bump this whenever a new default needs a one-time migration for existing
# saved configs (see Settings.load()). 2 = added the Hydrus tag source.
# 3 = added e621 and Safebooru to the site list. 4 = replaced
# use_saucenao_fallback with primary_engine + secondary_engine_mode.
# 5 = replaced use_filename_hashes with hash_source.
# 6 = extra search engines (ascii2d / trace.moe / IQDB 3D) plus
# Rule34, Xbooru, Twitter, AniList in the site list.
# 7 = Paheal in the site list, recognised now that Google Lens surfaces
# its posts.
# 8 = MangaUpdates in the site list, so its series pages can be filtered
# out deliberately rather than as part of "Other".
# 9 = MyAnimeList, same reasoning.
# 10 = Rule34.us in the site list, now that it has a parser - until then
# its matches counted as "Other".
# 11 = Reddit, the same way.
# 12 = Toon34, the same way again: its gallery pages counted as "Other"
# until canonicalize_url/classify_site learned to name them.
# (engine_delays, added later, needs no entry here: an absent key keeps
# the dataclass default of {}, which means "pace exactly as before".
# enable_google_images, enable_google_lens, enable_yandex and enable_pawchive are the
# same case - absent means False, which is the default a fresh config gets
# too, so nothing needs migrating. `mcp` (DAN-707) is the same case again -
# an old config has no "mcp" key at all, which loads as McpSettings()'s
# defaults, i.e. disabled - exactly the state every config was already in.)
CURRENT_SCHEMA_VERSION = 13

# Starter blacklist, pre-filled so that ticking the feature on does
# something useful immediately rather than presenting an empty box. These
# are booru housekeeping/metadata tags that describe the FILE or its
# tagging state rather than what's in the picture, which is what makes
# them noise in a personal library. Deliberately conservative - nothing
# here describes image content, so no entry can quietly cost real tags.
# Users can edit the list freely in Settings > Tag Namespaces.
DEFAULT_TAG_BLACKLIST = [
    "highres",
    "absurdres",
    "lowres",
    "incredibly_absurdres",
    "tagme",
    "commentary",
    "commentary_request",
    "commentary_typo",
    "translated",
    "translation_request",
    "check_translation",
    "artist_request",
    "character_request",
    "source_request",
    "bad_id",
    "bad_pixiv_id",
    "bad_twitter_id",
    "md5_mismatch",
    "resolution_mismatch",
    "revision",
    "third-party_edit",
    "duplicate",
]


@dataclass
class MatchConditions:
    """Conditions under which a booru match is considered 'better' than the
    local image (used to decide GOOD vs POOR / whether to suggest replacing
    the local file)."""
    min_width_gain_percent: float = 10.0   # booru image must be this % wider
    min_height_gain_percent: float = 10.0
    min_tags_for_good: int = 5             # fewer than this -> POOR (yellow)
    require_larger_than_local: bool = True


@dataclass
class SauceNaoSettings:
    api_key: str = ""
    use_json_api: bool = True
    min_similarity: float = 60.0
    tag_types: List[str] = field(default_factory=lambda: ["character", "material"])
    namespace: str = ""  # optional namespace prefix applied to retrieved tags
    dbmask: int = 999    # search all indexes by default
    pause_search_on_quota_exhausted: bool = True  # stop the batch when SauceNAO's DAILY allowance
                                                   # runs out, rather than searching on and recording
                                                   # degraded results for the rest of the list


@dataclass
class HydrusSettings:
    api_url: str = "http://127.0.0.1:45869"
    access_key: str = ""
    verify_ssl: bool = True
    tag_service_key: str = ""   # empty -> use "my tags" default local service
    timeout: float = 30.0


@dataclass
class McpSettings:
    """The embedded MCP server - lets a local AI drive a review pass while
    you're away. See core/mcp_server.py and core/mcp_tools.py.

    `host` is deliberately NOT a field here: it is always 127.0.0.1 (a
    module constant in core/mcp_server.py), never configurable, because
    this is a loopback-only connection by design and a setting that could
    be set to "0.0.0.0" is a setting that eventually will be.
    """
    enabled: bool = False       # off by default - this exposes control of a live Hydrus to
                                 # whatever can reach the port, even though that's loopback-only
    port: int = 8787
    token: str = ""              # bearer token every request must present. Blank means the
                                 # server refuses to start rather than listen with no auth at all
    dry_run: bool = False        # every write tier validates and logs the call it WOULD have
                                 # made, changes nothing, and says so in the result
    allow_research: bool = False        # the `research` tool - spends third-party search engine
                                         # quota (SauceNAO's daily allowance) the same as the
                                         # Shortcuts tab's Re-search action does
    allow_hydrus_writes: bool = True     # send_upload / send_url / download_send - defaults ON
                                         # by board direction (DAN-702, 2026-10-07): the whole
                                         # point of this server is unattended Hydrus writes, so
                                         # shipping them refusing by default is a fail. See
                                         # tests/test_mcp_config.py's
                                         # test_hydrus_writes_default_on_is_a_board_decision.
    allow_destructive: bool = False     # remove_row / reset_result - local-list-only, but still
                                         # not something to hand over by default
    audit_log_max_bytes: int = 5_000_000  # rotates mcp_audit.jsonl once it passes this size


@dataclass
class Settings:
    schema_version: int = CURRENT_SCHEMA_VERSION
    delay_min_seconds: float = 45.0
    search_timeout: float = 30.0  # for IQDB/SauceNAO/booru-page network calls specifically -
                                   # deliberately separate from hydrus.timeout (Hydrus API calls
                                   # are a different kind of operation with very different normal latency)
    delay_max_seconds: float = 75.0
    engine_delays: Dict[str, List[float]] = field(default_factory=dict)
    # engine id -> [min_seconds, max_seconds], overriding the global delay
    # for requests to THAT engine only. Absent (the default for every
    # engine) means use delay_min_seconds/delay_max_seconds, so an
    # untouched config paces exactly as it did before this existed.
    #
    # The point is that the five engines are five different hosts. One
    # global gap has to be set for the strictest of them, so a run that
    # queries only IQDB - which is what the README recommends once
    # SauceNAO's daily quota is spent - waits at SauceNAO's pace for a
    # service it never calls. Deliberately empty by default: what each
    # host tolerates is not something to guess on the user's behalf, and
    # guessing too low gets them banned.
    hash_source: str = "hydrus"  # where a file's SHA256 comes from when adding a batch:
                                  #   "hydrus" - take it from the filename when the file is named
                                  #              after its own hash, as everything in Hydrus's file
                                  #              store is, and only read files that aren't. A sample
                                  #              is verified against real contents first, and any
                                  #              mismatch falls back to reading everything.
                                  #   "local"  - always read and hash every file's bytes.
                                  # Both produce identical hashes for a genuine Hydrus store; the
                                  # difference is only whether 85 GB has to cross the wire to find
                                  # that out.
    thumbnail_source: str = "hydrus"  # where row thumbnails come from:
                                       #   "hydrus" - use Hydrus's own stored thumbnail for files it
                                       #              already has (a few KB instead of reading the
                                       #              whole file), falling back to decoding locally
                                       #              for anything Hydrus doesn't know
                                       #   "local"  - always decode from the file itself
                                       #   "off"    - no row thumbnails at all, read nothing
                                       # Matters most on a network share, where decoding thumbnails
                                       # for a large batch means pulling every byte across the wire.
    lazy_thumbnails: bool = True  # only generate thumbnails for rows actually on screen (plus a
                                   # small buffer), refreshing as you scroll, instead of generating
                                   # one for every entry the moment it's added. A table shows ~20
                                   # rows at a time, so for a batch of tens of thousands this is the
                                   # difference between reading a handful of files and reading all
                                   # of them. Turn off to go back to generating everything up front.
    hash_workers: int = 1  # parallel file-hashing threads when adding a batch. 1 = sequential
                            # (the safe default). Higher can help substantially on a network
                            # share, where per-read latency dominates; usually pointless or
                            # slightly worse on a local disk. Worth measuring - the app logs
                            # its own MB/s each run so different values can be compared.
    retrieve_tags_from_booru: bool = True
    drop_restricted_matches: bool = True  # drop matches the site won't actually show without a paid
                                           # account tier - Danbooru keeps banned and censored posts
                                           # listed but Gold-only, so they turn up as matches whose
                                           # image can never be fetched, tagged or compared
    rank_matches_by_quality: bool = True  # order the match dropdown by how USEFUL each match is,
                                           # not just how similar. Similarity still dominates - the
                                           # quality bonus is capped so it can only reorder matches
                                           # that are already about equally likely to be the same
                                           # image. Off = pure similarity order
    drop_dead_matches: bool = True  # when a match's source page returns a definite 404/410 during
                                     # search, drop it rather than presenting a dead link as a result
    borrow_tags_from_other_matches: bool = True  # when the chosen match yields no tags, take them
                                                  # from another match of the SAME image instead of
                                                  # leaving it untagged. MangaDex and Sankaku are why
                                                  # this earns its keep: neither can supply tags at
                                                  # all - MangaDex by design, Sankaku because a
                                                  # re-encoded local file cannot be found by hash -
                                                  # and between them they are 94% of the tagless
                                                  # matches measured on a real library
    borrow_tags_similarity_slack: float = 5.0  # how far BELOW the chosen match's similarity a
                                                # candidate may be and still lend its tags. The point
                                                # is not to tag an image with some other picture's
                                                # tags: a candidate this close is the same picture on
                                                # another site, which is exactly what makes its tags
                                                # worth having. 0 means "only an equal or better
                                                # match may lend"
    continue_without_saucenao_on_quota: bool = False  # when SauceNAO's DAILY allowance runs out,
                                                       # keep going with the other engines instead of
                                                       # stopping. IQDB has no daily cap, so the
                                                       # alternative to this is an idle machine: a
                                                       # 24,000-image library against a 5,000/day
                                                       # allowance is five days, most of it spent
                                                       # waiting for midnight.
                                                       #
                                                       # Results found this way are marked PROVISIONAL
                                                       # and deliberately NOT cached, because they are
                                                       # weaker than a full search would have given -
                                                       # which is precisely why stopping was the
                                                       # default. Re-running them once the allowance
                                                       # resets replaces them with the real thing.
                                                       #
                                                       # Takes precedence over the pause below.
    retry_failed_searches: bool = True  # after the first pass, give each image that ended in
                                         # ERROR one more attempt. Those are network faults -
                                         # timeouts, 502s, a rate limit - not verdicts, which is why
                                         # the result cache deliberately never remembers them. Until
                                         # this existed nothing acted on that: the run ended and the
                                         # rows waited to be spotted and re-searched by hand.
                                         # Exactly one extra attempt, so a persistently failing image
                                         # cannot loop
    remove_after_import: bool = False  # drop successfully-sent-to-Hydrus images from the list
    restore_session_on_start: bool = True  # reload the working list on launch, so a restart
                                            # doesn't re-hash files the app has already seen
    autosave_session: bool = True          # also save the working list periodically while running,
                                            # not only on a clean exit. A search over a large batch
                                            # runs for hours or days, and a crash or power cut in
                                            # the middle would otherwise lose which files had
                                            # already been sent to Hydrus
    autosave_interval_seconds: int = 120   # how often that periodic save happens
    search_cache_ttl_days: float = 30.0  # re-search a cached "not found" once it is this old;
                                          # 0 disables expiry. Found matches are never aged out -
                                          # see EXPIRING_STATUSES in core/search_cache.py for why
    use_search_cache: bool = True  # reuse a previous search result instead of re-hitting IQDB/SauceNAO
                                    # for an image (by content hash) that's already been searched before
    sankaku_cookies: str = ""       # raw cookie string copied from a logged-in browser, e.g.
                                     # "login=you; pass_hash=abc123". Sankaku hides adult and
                                     # account-only posts from anonymous requests, redirecting them
                                     # to a blank page - with these the real post is seen instead.
                                     # Stored as a whole cookie string rather than named fields
                                     # because Sankaku has changed its auth more than once, and a
                                     # free-form string keeps working when the names change.
    animepictures_cookies: str = ""  # raw cookie string from a logged-in browser. Anime-Pictures
                                      # gates some posts behind an account and answers its API with
                                      # HTTP 403 {"errormsg":"Forbidden"} for them - no tags, no
                                      # dimensions, no preview, nothing. Free-form like Sankaku's
                                      # rather than a named field: the site sets no cookie at all
                                      # for anonymous visitors, so the auth cookie's name can't be
                                      # discovered without an account, and pasting the whole string
                                      # keeps working whatever it turns out to be called.
    deviantart_cookies: str = ""     # raw cookie string from a logged-in browser. DeviantArt bakes
                                      # the blur for adult deviations into the signed image token
                                      # itself when you're logged out, so no URL rewriting undoes
                                      # it - a session is the only way to see the real picture.
    e621_username: str = ""   # e621 gates some posts behind an account: the API returns the post
    e621_api_key: str = ""     # but withholds file.url for anything under its global blacklist, so
                                # the match arrives with no picture to preview, download or compare.
                                # Unlike the cookie-based sites below, e621 documents a proper API
                                # key for third-party tools - found under Account > Manage API
                                # Access on the site - so that is what this uses. It is sent as HTTP
                                # Basic auth and is as good as a password: treat it accordingly. A
                                # WRONG one is worse than none, since e621 answers 401 to everything;
                                # the parser recognises that and says so rather than reporting a
                                # generic failed fetch.
    pixiv_session_cookie: str = ""  # PHPSESSID from a logged-in browser session. Pixiv dropped
                                     # username/password auth for third-party tools, so this is the
                                     # practical way in. Treat it like a password - it grants access
                                     # to the account it came from. Expires periodically; re-copy it
                                     # from the browser when Pixiv results start coming back empty.
    enable_tag_namespace_remap: bool = False  # e.g. rewrite booru's "artist:" to Hydrus PTR's "creator:"
    tag_namespace_remap: Dict[str, str] = field(default_factory=lambda: {"artist": "creator"})
    enable_tag_blacklist: bool = False  # drop booru housekeeping tags (highres, tagme, bad_id, ...) as
                                         # they come in, so they never reach the tag list or Hydrus.
                                         # Off by default: silently discarding tags is not something to
                                         # start doing to an existing setup without being asked
    tag_blacklist: List[str] = field(default_factory=lambda: list(DEFAULT_TAG_BLACKLIST))
    add_rating_tag: bool = False  # turn the matched post's age rating into a tag ("rating:explicit").
                                   # Off by default: it adds a tag the library did not have before,
                                   # and one that ends up in Hydrus. Only the sites that STATE a
                                   # rating contribute one - it is never inferred
    rating_tag_namespace: str = "rating"  # the namespace that tag is filed under. Configurable
                                           # because Hydrus users' conventions differ; a blank value
                                           # falls back to "rating" rather than producing an
                                           # unnamespaced "explicit" floating among the general tags
    auto_import_enabled: bool = False  # automatically send confident matches to Hydrus during a search,
                                        # before moving on to the next image in the batch
    auto_import_min_similarity: float = 90.0  # only auto-imports if the top match meets/exceeds this
    auto_import_method: str = "upload"  # "upload" | "url_importer" | "download_send"
    url_import_confirm_timeout: float = 60.0  # how long to wait for Hydrus to confirm a URL-importer
                                               # download actually finished, before giving up
    url_import_confirm_interval: float = 2.0  # how often to check while waiting
    set_hydrus_duplicate_relationships: bool = False  # when Download + send brings in a better copy of
                                                       # a file Hydrus already holds, tell Hydrus the two
                                                       # are related instead of leaving it with two files
                                                       # it thinks are strangers: the new copy as the king
                                                       # of the duplicate group when it is the same picture,
                                                       # or the pair marked alternates when it is a
                                                       # different edit or crop. Off by default: it writes
                                                       # a relationship into the user's library, and a
                                                       # wrong pairing is tedious to pick apart again.
                                                       # Needs the "Edit File Relationships" permission on
                                                       # the Hydrus access key
    write_hydrus_provenance_note: bool = False  # on a send, write a short note onto the file in
                                                 # Hydrus recording where the match came from: engine,
                                                 # site, similarity (with the marker that says whether
                                                 # that number was measured), the matched URL and the
                                                 # date. All of it is already shown and exported; the
                                                 # library itself keeps none of it. Off by default: it
                                                 # writes into the user's library, though a note is
                                                 # trivial to delete from Hydrus's own notes panel.
                                                 # Needs the "Edit File Notes" permission on the Hydrus
                                                 # access key
    hydrus_provenance_note_name: str = ""  # which note that goes in, blank = core.provenance_note's
                                            # DEFAULT_NOTE_NAME. Configurable because it is the one
                                            # thing about this that lands in the user's own namespace,
                                            # and the app REPLACES the note it names on every send -
                                            # so anyone who keeps notes of their own needs to be able
                                            # to keep this one out of their way
    enable_hydrus_tag_colors: bool = False  # color tags in the tag list to match Hydrus's namespace colors
    tag_namespace_colors: Dict[str, str] = field(default_factory=dict)  # user overrides for the built-in defaults;
                                                                          # key "" overrides the unnamespaced color
    log_matched_urls: bool = True
    log_file_path: str = str(DEFAULT_LOG_FILE)
    enabled_tag_sources: List[str] = field(
        default_factory=lambda: ["User", "Hydrus", "Booru", "Hatate-linux"]
    )
    filter_sent_tags_by_source: bool = False  # whether enabled_tag_sources also decides which tags are
                                              # sent to Hydrus and written to exports, rather than only
                                              # which ones the tag list widget shows. Off by default and
                                              # left off by the v12 -> v13 migration: this switch is new,
                                              # and turning it on for an existing user would change what
                                              # their next send puts in their library without them asking
                                              # (DAN-72)
    # Files > Write Tag Files (DAN-76). Both absent from an older config
    # means the default, and both defaults are the cautious choice, so
    # there is nothing to migrate.
    sidecar_filename_style: str = "append"  # "append" = cat.jpg.txt, what Hydrus's own sidecar
                                             # importer looks for and a name that cannot collide with
                                             # anything else's; "replace" = cat.txt, what the
                                             # stable-diffusion training tooling reads but which can
                                             # land on an unrelated cat.txt of the user's
    sidecar_overwrite: str = "ask"  # "ask" | "skip" | "overwrite", for a sidecar that is already
                                     # there. This is the app's only write into the user's own picture
                                     # folders, so the default asks; "overwrite" is for someone
                                     # re-running over a folder they know this app wrote

    # Tags the app applies itself, chosen by how a search turned out
    # (DAN-77). Written under the "Hatate-linux" source, so the filter
    # above can keep them out of Hydrus. All three are empty by default:
    # these are tags no site put on the image and nobody typed, so the
    # feature does nothing at all until somebody configures it. No
    # migration needed for the same reason - an older config loads them
    # empty, which is off. See core/tag_rules.py for the rule.
    tags_for_found: List[str] = field(default_factory=list)
    tags_for_not_found: List[str] = field(default_factory=list)
    # Added on top of tags_for_found when the match came back with fewer
    # than match_conditions.min_tags_for_good tags - the same number that
    # already decides GOOD vs POOR, reused rather than configured twice.
    tags_for_low_tag_count: List[str] = field(default_factory=list)

    enabled_sites: List[str] = field(default_factory=lambda: list(ALL_SITE_OPTIONS))
    primary_engine: str = "iqdb"          # "iqdb" or "saucenao" - which one is searched first
    secondary_engine_mode: str = "always"  # "always" (search both, merge every site) |
                                            # "fallback" (only search the second engine if
                                            # the primary finds nothing) | "disabled" (never
                                            # use the second engine at all)
    fallback_below_similarity: float = 0.0  # 0 = off. Above 0, the fallback wave runs
                                            # unless the best match so far is at least
                                            # this similar - so a weak result brings in
                                            # the remaining engines instead of standing.
    extras_only_as_fallback: bool = False   # hold ascii2d/trace.moe/IQDB 3D/Google back
                                            # for that fallback wave rather than running
                                            # them on every image. Pairs with the setting
                                            # above; the slow and metered engines are the
                                            # ones worth spending only when needed.
    enable_ascii2d: bool = True            # colour/feature search; often finds Pixiv/Twitter
                                            # when IQDB does not. No API key. Runs in parallel
                                            # with IQDB/SauceNAO.
    enable_tracemoe: bool = False          # anime-screenshot search. Off by default: the
                                            # anonymous monthly quota would be burned by a
                                            # booru-tagging batch that is not screenshots.
    enable_iqdb3d: bool = False            # 3d.iqdb.org, for 3D/CG. Off by default so a 2D
                                            # library does not spend a request per image on
                                            # an index that will not match it.
    enable_google_images: bool = False     # general web reverse search. Finds the page an
                                            # image was posted on when the booru engines find
                                            # nothing, but carries no tags, so it is opt-in
                                            # rather than a default.
    enable_google_lens: bool = False       # the reverse image search from images.google.com,
                                            # read out of a real browser engine. Strongest of
                                            # the extra engines on this kind of art, but needs
                                            # the optional PyQt6-WebEngine package and can ask
                                            # the user to answer Google's robot check.
    enable_yandex: bool = False            # yandex.com's reverse image search, over plain
                                            # HTTP. Keeps only post pages on sites with a
                                            # parser. Off by default: it uploads each picture
                                            # to Yandex, and can be met with a captcha.
    enable_pawchive: bool = False          # looks each file up on pawchive.pw by its SHA-256.
                                            # Finds exact copies of Patreon/Fanbox posts that
                                            # SauceNAO and IQDB do not index. One small request
                                            # per image and no upload, so it always runs in
                                            # the first wave, even with the extras held back.
                                            # Off by default: it sends every file's hash to
                                            # pawchive.pw.
    enable_pawchive_index: bool = True     # searches the local index of pawchive artists
                                            # built under Files > Pawchive Index, for resized
                                            # or re-saved copies the exact lookup can't find.
                                            # On by default: it is local, sends nothing, and
                                            # does nothing at all until an artist is indexed.
    lens_search_whole_image: bool = True    # Google Lens picks a "search area" out of the
                                            # picture and searches only that. It sometimes
                                            # settles on one small detail and answers about
                                            # that instead of the image. On = widen it back
                                            # to the whole picture; off = take Lens's crop.
    google_images_api_key: str = ""        # Google Cloud Vision key, for its web-detection
                                            # API. Effectively required: the public endpoint
                                            # still accepts the upload but now answers with a
                                            # page that has no results in it until Google's
                                            # own JavaScript has run. See core/google_images.py.
    tracemoe_min_similarity: float = 85.0  # trace.moe false-positives a lot below this
    saucenao: SauceNaoSettings = field(default_factory=SauceNaoSettings)
    hydrus: HydrusSettings = field(default_factory=HydrusSettings)
    match_conditions: MatchConditions = field(default_factory=MatchConditions)
    mcp: McpSettings = field(default_factory=McpSettings)
    review_shortcuts: Dict[str, str] = field(default_factory=dict)
    # Keyboard bindings for the review pass, as action id -> key sequence.
    # Only the ones the user actually changed are stored: an absent id
    # means "use the default", so a later build can improve a default and
    # have it reach everyone who never touched that particular binding.
    # An id mapped to "" is different - that is a binding deliberately
    # cleared, and it stays cleared. See core/shortcuts.py.
    # "system" follows the desktop, "dark"/"light" pin it. Absent from an
    # older config, which simply leaves the default - no migration needed.
    theme: str = "system"
    window_geometry: str = ""
    table_header_state: str = ""
    table_header_columns: int = 0  # how many columns table_header_state was saved with, so a
                                    # build that adds or removes one can discard a layout that
                                    # no longer describes the table (0 = saved before this existed)  # column widths/order for the image table, base64-encoded Qt state

    @staticmethod
    def load() -> "Settings":
        if not CONFIG_FILE.exists():
            log.info("No config file at %s, creating one with defaults", CONFIG_FILE)
            s = Settings()
            s.save()
            return s
        # An unreadable config used to mean defaults, and the next save
        # then wrote those defaults straight over it - so one bad read
        # cost every key and cookie in the file, permanently. The backup
        # is tried next, and if that fails too the unreadable file is
        # moved aside rather than left to be overwritten: it is the only
        # copy of those credentials there is.
        raw = None
        for source in (CONFIG_FILE, CONFIG_BACKUP_FILE):
            if not source.exists():
                continue
            try:
                raw = json.loads(source.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                log.error("Could not read/parse %s (%s)", source, exc)
                continue
            if source is CONFIG_BACKUP_FILE:
                log.warning("Recovered settings from the backup at %s", CONFIG_BACKUP_FILE)
                _preserve_unreadable_config()
            break

        if raw is None:
            log.error("No readable config or backup - starting from defaults")
            _preserve_unreadable_config()
            return Settings()

        # Tighten an existing config immediately rather than waiting for
        # the next save: one written before this app restricted them is
        # sitting there world-readable right now, and a user who never
        # changes a setting would never trigger a save to fix it.
        _restrict(CONFIG_FILE, _FILE_MODE)
        _restrict(CONFIG_DIR, _DIR_MODE)

        stored_version = raw.get("schema_version", 1)  # configs saved before this field existed are version 1
        log.debug("Loading settings from %s (schema_version=%d)", CONFIG_FILE, stored_version)

        s = Settings()
        unknown_keys = []
        for k, v in raw.items():
            if k == "saucenao" and isinstance(v, dict):
                s.saucenao = SauceNaoSettings(**{**asdict(SauceNaoSettings()), **v})
            elif k == "hydrus" and isinstance(v, dict):
                s.hydrus = HydrusSettings(**{**asdict(HydrusSettings()), **v})
            elif k == "match_conditions" and isinstance(v, dict):
                s.match_conditions = MatchConditions(**{**asdict(MatchConditions()), **v})
            elif k == "mcp" and isinstance(v, dict):
                s.mcp = McpSettings(**{**asdict(McpSettings()), **v})
            elif hasattr(s, k):
                setattr(s, k, v)
            else:
                unknown_keys.append(k)
        if unknown_keys:
            log.debug("Ignored %d unrecognized key(s) in config file: %s", len(unknown_keys), unknown_keys)

        if stored_version < 2:
            # Configs saved before the Hydrus tag source existed still have
            # the old enabled_tag_sources list on disk, which would silently
            # hide auto-imported Hydrus tags in the tag panel even though
            # they're genuinely on the entry - add it in once, here, rather
            # than every load overriding it back out from the saved file.
            if "Hydrus" not in s.enabled_tag_sources:
                log.info("Migrating config: adding 'Hydrus' to enabled_tag_sources (schema v1 -> v2)")
                s.enabled_tag_sources.append("Hydrus")

        if stored_version < 3:
            # Same situation for e621 and Safebooru: existing configs have
            # an enabled_sites list saved from before these existed, which
            # would silently filter their matches out as if the user had
            # deliberately unchecked them.
            added = [site for site in ("e621", "Safebooru") if site not in s.enabled_sites]
            if added:
                log.info("Migrating config: adding %s to enabled_sites (schema v2 -> v3)", added)
                s.enabled_sites.extend(added)

        if stored_version < 4:
            # use_saucenao_fallback (bool) is replaced by primary_engine +
            # secondary_engine_mode. Map the old value to its closest
            # equivalent so existing users see no behavior change until
            # they explicitly pick something new in Settings: IQDB stays
            # primary (that was always the hardcoded order before), and
            # the old True/False becomes "always"/"disabled".
            old_value = raw.get("use_saucenao_fallback")
            if old_value is not None:
                s.secondary_engine_mode = "always" if old_value else "disabled"
                log.info(
                    "Migrating config: use_saucenao_fallback=%s -> secondary_engine_mode=%r (schema v3 -> v4)",
                    old_value, s.secondary_engine_mode,
                )

        if stored_version < 5:
            # use_filename_hashes (bool) is replaced by hash_source, a
            # named mode, so the setting reads as a deliberate choice
            # between two sources rather than an obscure opt-out. Same
            # shape as the v3 -> v4 change above: map the old value to
            # its exact equivalent so nobody's behaviour shifts.
            old_value = raw.get("use_filename_hashes")
            if old_value is not None:
                s.hash_source = "hydrus" if old_value else "local"
                log.info(
                    "Migrating config: use_filename_hashes=%s -> hash_source=%r (schema v4 -> v5)",
                    old_value, s.hash_source,
                )

        if stored_version < 6:
            # New sites must be added to existing enabled_sites lists the
            # same way e621/Safebooru were in v3 - otherwise they look
            # like the user unchecked them.
            added = [
                site for site in ("Rule34", "Xbooru", "Twitter", "AniList")
                if site not in s.enabled_sites
            ]
            if added:
                log.info("Migrating config: adding %s to enabled_sites (schema v5 -> v6)", added)
                s.enabled_sites.extend(added)

        if stored_version < 7:
            # Paheal, recognised now that Google Lens surfaces its posts.
            # Same reason as every site added before it: without this it
            # looks like the user unchecked it.
            if "Paheal" not in s.enabled_sites:
                log.info("Migrating config: adding Paheal to enabled_sites (schema v6 -> v7)")
                s.enabled_sites.append("Paheal")

        if stored_version < 8:
            # MangaUpdates, named now that it turns up often enough to be
            # worth filtering deliberately. Same reason as every site
            # added before it: without this it looks like the user
            # unchecked it.
            if "MangaUpdates" not in s.enabled_sites:
                log.info("Migrating config: adding MangaUpdates to enabled_sites (schema v7 -> v8)")
                s.enabled_sites.append("MangaUpdates")

        if stored_version < 9:
            # MyAnimeList, for the same reason as MangaUpdates before it.
            if "MyAnimeList" not in s.enabled_sites:
                log.info("Migrating config: adding MyAnimeList to enabled_sites (schema v8 -> v9)")
                s.enabled_sites.append("MyAnimeList")

        if stored_version < 10:
            # Rule34.us. Unlike the sites before it, its matches were
            # already being kept or dropped - as "Other" - so the new
            # switch starts where that one was: a user who unticked
            # "Other" had rule34.us hidden, and still does.
            if "Rule34.us" not in s.enabled_sites and OTHER_SITE in s.enabled_sites:
                log.info("Migrating config: adding Rule34.us to enabled_sites (schema v9 -> v10)")
                s.enabled_sites.append("Rule34.us")

        if stored_version < 11:
            # Reddit, for the same reason as Rule34.us: it was "Other" until
            # now, so it starts wherever that switch was.
            if "Reddit" not in s.enabled_sites and OTHER_SITE in s.enabled_sites:
                log.info("Migrating config: adding Reddit to enabled_sites (schema v10 -> v11)")
                s.enabled_sites.append("Reddit")

        if stored_version < 12:
            # Toon34, for the same reason as Reddit: it was "Other" until
            # now, so it starts wherever that switch was.
            if "Toon34" not in s.enabled_sites and OTHER_SITE in s.enabled_sites:
                log.info("Migrating config: adding Toon34 to enabled_sites (schema v11 -> v12)")
                s.enabled_sites.append("Toon34")

        if stored_version < 13:
            # filter_sent_tags_by_source. Every build up to v12 sent and
            # exported every tag regardless of the tag-source checkboxes,
            # so an existing config's ticks say nothing about what that
            # user wants sent - only about what they wanted to look at.
            # Pinning the new switch off keeps their next send byte-for-byte
            # what the last one was; they opt in from Settings > General
            # when they actually mean it. Written out explicitly rather
            # than left to the dataclass default so the intent is on disk
            # and a later default flip can't reach back and change it.
            s.filter_sent_tags_by_source = False
            log.info("Migrating config: filter_sent_tags_by_source pinned off "
                     "so sends keep their current contents (schema v12 -> v13)")

        if stored_version < CURRENT_SCHEMA_VERSION:
            log.info("Migrating config schema_version %d -> %d", stored_version, CURRENT_SCHEMA_VERSION)
            s.schema_version = CURRENT_SCHEMA_VERSION
            s.save()  # persist the migration once so it doesn't reapply

        return s

    def save(self):
        """Writes the config, keeping the previous one as a backup.

        This file holds the Hydrus access key, the SauceNAO and Cloud
        Vision keys, and the site cookies - none of which the user can
        get back by clicking around. Two things protect them:

        The write is ATOMIC. The old version wrote in place with
        O_TRUNC, which empties the file before a single byte of the new
        content lands; anything that interrupts that window - a crash, a
        kill, the power going - leaves an empty or half-written file
        where the credentials were. A temporary file in the same
        directory, followed by os.replace, is either the old content or
        the new one and never something in between.

        And the previous version is kept as config.json.bak. That is
        what turns "my settings are gone" into a file to copy back, and
        it is worth the few kilobytes: this was written after a config
        was lost, and there was nothing to restore from.
        """
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _restrict(CONFIG_DIR, _DIR_MODE)
        payload = json.dumps(asdict(self), indent=2)

        if CONFIG_FILE.exists():
            try:
                shutil.copy2(CONFIG_FILE, CONFIG_BACKUP_FILE)
                _restrict(CONFIG_BACKUP_FILE, _FILE_MODE)
            except OSError as exc:
                # Worth a warning, never worth refusing to save over.
                log.warning("Could not back up %s to %s: %s",
                            CONFIG_FILE, CONFIG_BACKUP_FILE, exc)

        # Written through a descriptor with the mode set at creation
        # rather than written and then chmod-ed: the latter leaves a
        # moment, however brief, where a file full of credentials is
        # world-readable.
        temp_path = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
        descriptor = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())   # so the rename cannot outrun the content
        _restrict(temp_path, _FILE_MODE)
        os.replace(temp_path, CONFIG_FILE)
        log.debug("Settings saved to %s", CONFIG_FILE)
