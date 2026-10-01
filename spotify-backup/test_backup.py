#!/usr/bin/env python3
"""Tests for spotify-backup mirror behavior."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
MODULE_PATH = HERE / "backup.py"


def load_backup_module(testcase: unittest.TestCase):
    if not MODULE_PATH.exists():
        testcase.fail(f"Missing backup module: {MODULE_PATH}")

    fake_spotipy = types.ModuleType("spotipy")
    fake_spotipy.Spotify = object

    fake_oauth = types.ModuleType("spotipy.oauth2")

    class FakeSpotifyOAuth:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    fake_oauth.SpotifyOAuth = FakeSpotifyOAuth

    spec = importlib.util.spec_from_file_location("spotify_backup", MODULE_PATH)
    if spec is None or spec.loader is None:
        testcase.fail(f"Could not load backup module from: {MODULE_PATH}")

    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {"spotipy": fake_spotipy, "spotipy.oauth2": fake_oauth},
    ):
        spec.loader.exec_module(module)
    return module


class SpotifyBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="spotify_backup_test_")
        self.base = Path(self.tmpdir.name)
        self.backup_dir = self.base / "backup"
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.base / "state"
        self.state_dir.mkdir()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_save_backup_keeps_one_current_pointer_and_history(self):
        module = load_backup_module(self)
        module.BACKUP_DIR = str(self.backup_dir)
        module.STATE_DIR = str(self.state_dir)

        first = module.save_backup({"timestamp": "2026-07-17T10:00:00"})
        second = module.save_backup({"timestamp": "2026-07-17T14:00:00"})

        self.assertEqual(first, str(self.backup_dir / "current" / "spotify_backup_current.json"))
        self.assertEqual(second, first)
        self.assertTrue((self.backup_dir / "current" / "spotify_backup_current.json").exists())
        self.assertEqual(
            len(list((self.backup_dir / "snapshots").glob("*/spotify_backup_current.json"))),
            2,
        )
        self.assertEqual(
            json.loads((self.backup_dir / "current" / "spotify_backup_current.json").read_text()),
            {"timestamp": "2026-07-17T14:00:00"},
        )

    def test_main_returns_failure_when_oauth_cache_is_missing(self):
        module = load_backup_module(self)
        module.CLIENT_ID = "client"
        module.CLIENT_SECRET = "secret"
        module.CACHE_PATH = str(self.base / ".cache")
        result = "raised"

        with patch.object(module, "get_spotify_client", side_effect=RuntimeError("client should not be built")):
            try:
                with self.assertLogs(module.logger, level="ERROR") as captured:
                    result = module.main()
            except RuntimeError:
                captured = None

        self.assertEqual(result, 1)
        self.assertIsNotNone(captured)
        self.assertIn("OAuth cache", "\n".join(captured.output))

    def test_get_spotify_client_disables_long_rate_limit_retries(self):
        module = load_backup_module(self)
        module.CLIENT_ID = "client"
        module.CLIENT_SECRET = "secret"
        captured = {}

        class FakeSpotifyClient:
            def __init__(self, *args, **kwargs):
                captured.update(kwargs)

        with patch.object(module, "Spotify", FakeSpotifyClient):
            module.get_spotify_client()

        self.assertEqual(captured["retries"], 0)
        self.assertEqual(captured["status_retries"], 0)
        self.assertEqual(captured["backoff_factor"], 0)

    def test_backup_playlists_skips_inaccessible_playlists_and_keeps_metadata(self):
        module = load_backup_module(self)
        module.BACKUP_DIR = str(self.backup_dir)
        module.STATE_DIR = str(self.state_dir)

        class FakeSpotify:
            def current_user_playlists(self):
                return {
                    "items": [
                        {
                            "id": "ok-playlist",
                            "name": "Owned playlist",
                            "description": "ok",
                            "owner": {"id": "test-owner"},
                            "collaborative": False,
                            "public": False,
                            "items": {"total": 1},
                        },
                        {
                            "id": "forbidden-playlist",
                            "name": "Blocked playlist",
                            "description": "forbidden",
                            "owner": {"id": "spotify-editor"},
                            "collaborative": False,
                            "public": True,
                            "items": {"total": 99},
                        },
                    ],
                    "next": None,
                }

            def playlist_items(self, playlist_id, limit=None):
                if playlist_id == "forbidden-playlist":
                    raise RuntimeError("http status: 403")
                return {
                    "items": [
                        {
                            "track": {
                                "id": "track-1",
                                "name": "Song",
                                "artists": [{"id": "artist-1", "name": "Artist"}],
                                "album": {"id": "album-1", "name": "Album", "release_date": "2026-01-01"},
                                "duration_ms": 1000,
                                "popularity": 50,
                                "uri": "spotify:track:track-1",
                            }
                        }
                    ],
                    "next": None,
                }

        playlists = module.backup_playlists(FakeSpotify())

        self.assertEqual(len(playlists), 2)
        self.assertEqual(playlists[0]["tracks"][0]["id"], "track-1")
        self.assertEqual(playlists[0]["tracks_count"], 1)
        self.assertEqual(playlists[1]["id"], "forbidden-playlist")
        self.assertEqual(playlists[1]["tracks"], [])
        self.assertEqual(playlists[1]["tracks_count"], 99)
        self.assertEqual(playlists[1]["tracks_error"], "http status: 403")

    def test_rate_limit_preserves_current_backup_and_playlist_cache(self):
        module = load_backup_module(self)
        module.BACKUP_DIR = str(self.backup_dir)
        module.STATE_DIR = str(self.state_dir)
        module.CLIENT_ID = "client"
        module.CLIENT_SECRET = "secret"
        module.CACHE_PATH = str(self.state_dir / ".cache")
        Path(module.CACHE_PATH).write_text("{}")
        module.save_backup({"previous": True})
        current = self.backup_dir / "current" / module.CURRENT_BACKUP_NAME
        previous = current.read_bytes()
        cache = self.state_dir / module.PLAYLIST_CACHE_NAME
        cache.write_text('{}')

        class RateLimited(Exception):
            http_status = 429

        class FakeSpotify:
            def current_user_playlists(self):
                return {"items": [{"id": "playlist", "name": "Playlist", "description": "",
                          "owner": {"id": "owner"}, "collaborative": False, "public": True,
                          "snapshot_id": "new", "tracks": {"total": 1}}], "next": None}

            def playlist_items(self, *args, **kwargs):
                raise RateLimited("Too many requests")

        with patch.object(module, "get_spotify_client", return_value=FakeSpotify()), \
             patch.object(module, "backup_profile", return_value={"id": "owner"}):
            self.assertEqual(module.main(), 1)
        self.assertEqual(current.read_bytes(), previous)
        self.assertEqual(cache.read_text(), '{}')
        self.assertEqual(len(list((self.backup_dir / "snapshots").iterdir())), 1)

    def test_access_limits_are_recorded_without_losing_other_backup_data(self):
        module = load_backup_module(self)
        module.BACKUP_DIR = str(self.backup_dir)
        module.STATE_DIR = str(self.state_dir)
        module.CLIENT_ID = "client"
        module.CLIENT_SECRET = "secret"
        module.CACHE_PATH = str(self.base / ".cache")
        Path(module.CACHE_PATH).write_text("{}", encoding="utf-8")
        with patch.object(module, "get_spotify_client", return_value=object()), \
             patch.object(module, "backup_profile", return_value={"display_name": "Tester"}), \
             patch.object(module, "backup_playlists", return_value=[{"tracks_error": "403"}]), \
             patch.object(module, "backup_liked_tracks", return_value=[]), \
             patch.object(module, "backup_saved_albums", return_value=[]), \
             patch.object(module, "backup_followed_artists", return_value=[]):
            self.assertEqual(module.main(), 0)

        current = json.loads((self.backup_dir / "current" / module.CURRENT_BACKUP_NAME).read_text(encoding="utf-8"))
        self.assertEqual(current["playlist_items_unavailable"], 1)
        self.assertEqual(current["playlists"][0]["tracks_error"], "403")
        self.assertEqual(current["liked_tracks"], [])

    def test_playlist_with_cached_error_is_retried(self):
        module = load_backup_module(self)
        module.BACKUP_DIR = str(self.backup_dir)
        module.STATE_DIR = str(self.state_dir)
        (self.state_dir / module.PLAYLIST_CACHE_NAME).write_text(json.dumps({
            "playlist": {"snapshot_id": "same", "tracks": [], "tracks_error": "403"}
        }), encoding="utf-8")

        class FakeSpotify:
            calls = 0

            def current_user_playlists(self):
                return {"items": [{"id": "playlist", "name": "Playlist", "description": "",
                          "owner": {"id": "owner"}, "collaborative": False, "public": True,
                          "snapshot_id": "same", "tracks": {"total": 0}}], "next": None}

            def playlist_items(self, playlist_id, limit=100):
                self.calls += 1
                return {"items": [], "next": None}

        spotify = FakeSpotify()
        result = module.backup_playlists(spotify)
        self.assertEqual(spotify.calls, 1)
        self.assertNotIn("tracks_error", result[0])

    def test_followed_playlist_items_are_not_requested_when_api_disallows_them(self):
        module = load_backup_module(self)
        module.BACKUP_DIR = str(self.backup_dir)
        module.STATE_DIR = str(self.state_dir)

        class FakeSpotify:
            def current_user_playlists(self):
                return {"items": [{"id": "followed", "name": "Followed", "description": "",
                          "owner": {"id": "another-user"}, "collaborative": False,
                          "public": True, "snapshot_id": "same", "tracks": {"total": 10}}],
                        "next": None}

            def playlist_items(self, *args, **kwargs):
                raise AssertionError("Spotify forbids item access for this playlist")

        result = module.backup_playlists(FakeSpotify(), user_id="my-user")
        self.assertEqual(result[0]["tracks_count"], 10)
        self.assertEqual(result[0]["tracks"], [])
        self.assertIn("Spotify API limits", result[0]["tracks_error"])

    def test_backup_playlists_skips_items_without_track_but_keeps_other_tracks(self):
        module = load_backup_module(self)
        module.BACKUP_DIR = str(self.backup_dir)
        module.STATE_DIR = str(self.state_dir)

        class FakeSpotify:
            def current_user_playlists(self):
                return {
                    "items": [
                        {
                            "id": "mixed-playlist",
                            "name": "Mixed playlist",
                            "description": "mixed",
                            "owner": {"id": "test-owner"},
                            "collaborative": False,
                            "public": False,
                            "items": {"total": 3},
                        }
                    ],
                    "next": None,
                }

            def playlist_items(self, playlist_id, limit=None):
                return {
                    "items": [
                        {"episode": {"id": "episode-1"}},
                        {"track": None},
                        {
                            "track": {
                                "id": "track-1",
                                "name": "Song",
                                "artists": [{"id": "artist-1", "name": "Artist"}],
                                "album": {"id": "album-1", "name": "Album", "release_date": "2026-01-01"},
                                "duration_ms": 1000,
                                "popularity": 50,
                                "uri": "spotify:track:track-1",
                            }
                        },
                    ],
                    "next": None,
                }

        playlists = module.backup_playlists(FakeSpotify())

        self.assertEqual(len(playlists), 1)
        self.assertEqual(playlists[0]["tracks_count"], 3)
        self.assertEqual(len(playlists[0]["tracks"]), 1)
        self.assertEqual(playlists[0]["tracks"][0]["id"], "track-1")
        self.assertNotIn("tracks_error", playlists[0])

    def test_backup_liked_tracks_tolerates_missing_optional_track_fields(self):
        module = load_backup_module(self)

        class FakeSpotify:
            def current_user_saved_tracks(self):
                return {
                    "items": [
                        {
                            "added_at": "2026-07-17T19:31:51Z",
                            "track": {
                                "id": "track-1",
                                "name": "Song",
                                "artists": [{"id": "artist-1", "name": "Artist"}],
                                "album": {"id": "album-1", "name": "Album"},
                                "duration_ms": 1000,
                                "uri": "spotify:track:track-1",
                            },
                        }
                    ],
                    "next": None,
                }

        tracks = module.backup_liked_tracks(FakeSpotify())

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["id"], "track-1")
        self.assertIsNone(tracks[0]["popularity"])
        self.assertIsNone(tracks[0]["album"]["release_date"])

    def test_backup_playlists_reuses_cached_tracks_when_snapshot_is_unchanged(self):
        module = load_backup_module(self)
        module.BACKUP_DIR = str(self.backup_dir)
        module.STATE_DIR = str(self.state_dir)

        cache_path = self.state_dir / "playlist_track_cache.json"
        cache_path.write_text(
            json.dumps(
                {
                    "cached-playlist": {
                        "snapshot_id": "snapshot-1",
                        "tracks": [
                            {
                                "id": "track-1",
                                "name": "Song",
                                "artists": [{"id": "artist-1", "name": "Artist"}],
                                "album": {
                                    "id": "album-1",
                                    "name": "Album",
                                    "release_date": "2026-01-01",
                                },
                                "duration_ms": 1000,
                                "popularity": 50,
                                "uri": "spotify:track:track-1",
                            }
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )

        class FakeSpotify:
            def current_user_playlists(self):
                return {
                    "items": [
                        {
                            "id": "cached-playlist",
                            "name": "Cached playlist",
                            "description": "cached",
                            "owner": {"id": "test-owner"},
                            "collaborative": False,
                            "public": False,
                            "snapshot_id": "snapshot-1",
                            "items": {"total": 1},
                        }
                    ],
                    "next": None,
                }

            def playlist_items(self, playlist_id):
                raise RuntimeError("playlist_items should not be called when snapshot is unchanged")

        playlists = module.backup_playlists(FakeSpotify())

        self.assertEqual(len(playlists), 1)
        self.assertEqual(playlists[0]["snapshot_id"], "snapshot-1")
        self.assertEqual(playlists[0]["tracks"][0]["id"], "track-1")


if __name__ == "__main__":
    unittest.main()
