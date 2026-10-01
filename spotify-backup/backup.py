#!/usr/bin/env python3
"""
Spotify Backup - Export user data to JSON
Run via Docker Compose from the repository root.
"""

import os
import json
import logging
import sys
import shutil
from pathlib import Path
from datetime import datetime
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backup_storage import new_staging, publish_snapshot

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
REDIRECT_URI = os.getenv('SPOTIFY_REDIRECT_URI', 'http://127.0.0.1:9000/callback')
BACKUP_DIR = os.getenv('BACKUP_DIR', '/backup')
STATE_DIR = os.getenv('STATE_DIR', '/state')
CACHE_PATH = os.getenv('CACHE_PATH', '/state/.cache')
CURRENT_BACKUP_NAME = 'spotify_backup_current.json'
PLAYLIST_CACHE_NAME = 'playlist_track_cache.json'
SNAPSHOT_RETENTION = max(1, int(os.getenv('SPOTIFY_SNAPSHOT_RETENTION', '14')))

# Spotify scopes needed
SCOPES = [
    'user-read-private',
    'user-read-email',
    'playlist-read-private',
    'playlist-read-collaborative',
    'user-library-read',
    'user-follow-read',
    'user-top-read',
]


def get_spotify_client():
    """Initialize Spotify client with OAuth."""
    scope = ' '.join(SCOPES)
    auth_manager = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=scope,
        cache_path=CACHE_PATH,
        open_browser=False,
    )
    return Spotify(
        auth_manager=auth_manager,
        retries=0,
        status_retries=0,
        backoff_factor=0,
    )


def save_json_file(filename, data):
    """Atomically save the incremental playlist cache to STATE_DIR."""
    os.makedirs(STATE_DIR, exist_ok=True)

    path = os.path.join(STATE_DIR, filename)
    temp_path = f'{path}.tmp'

    with open(temp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    os.replace(temp_path, path)
    return path


def load_json_file(filename, default):
    """Load incremental cache data from STATE_DIR if present."""
    path = os.path.join(STATE_DIR, filename)
    if not os.path.exists(path):
        return default

    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def backup_profile(sp):
    """Backup user profile."""
    logger.info("Backing up profile...")
    profile = sp.current_user()
    return {
        'id': profile.get('id'),
        'email': profile.get('email'),
        'display_name': profile.get('display_name'),
        'product': profile.get('product'),
        'country': profile.get('country'),
        'followers': profile.get('followers', {}).get('total'),
        'images': profile.get('images'),
    }


def backup_playlists(sp, user_id=None):
    """Backup all playlists with tracks."""
    logger.info("Backing up playlists...")
    playlists = []
    playlist_cache = load_json_file(PLAYLIST_CACHE_NAME, {})
    updated_cache = {}
    results = sp.current_user_playlists()

    while results:
        for playlist in results['items']:
            tracks = []
            tracks_payload = playlist.get('tracks') or playlist.get('items') or {}
            snapshot_id = playlist.get('snapshot_id')
            playlist_record = {
                'id': playlist['id'],
                'name': playlist['name'],
                'description': playlist['description'],
                'owner': playlist['owner']['id'],
                'collaborative': playlist['collaborative'],
                'public': playlist['public'],
                'snapshot_id': snapshot_id,
                'tracks_count': tracks_payload.get('total', 0),
                'tracks': tracks,
            }

            cached = playlist_cache.get(playlist['id'])
            if user_id and playlist_record['owner'] != user_id and not playlist_record['collaborative']:
                if cached and cached.get('snapshot_id') == snapshot_id:
                    tracks.extend(cached.get('tracks', []))
                playlist_record['tracks_error'] = 'Spotify API limits playlist items to owners and collaborators'
            elif cached and cached.get('snapshot_id') == snapshot_id and not cached.get('tracks_error'):
                tracks.extend(cached.get('tracks', []))
            else:
                try:
                    track_results = sp.playlist_items(playlist['id'], limit=100)
                    while track_results:
                        for item in track_results['items']:
                            track = item.get('track')
                            if track:
                                track = item['track']
                                album = track.get('album') or {}
                                tracks.append({
                                    'id': track.get('id'),
                                    'name': track.get('name'),
                                    'artists': [
                                        {'id': artist.get('id'), 'name': artist.get('name')}
                                        for artist in track.get('artists', [])
                                    ],
                                    'album': {
                                        'id': album.get('id'),
                                        'name': album.get('name'),
                                        'release_date': album.get('release_date'),
                                    },
                                    'duration_ms': track.get('duration_ms'),
                                    'popularity': track.get('popularity'),
                                    'uri': track.get('uri'),
                                })
                        if track_results['next']:
                            track_results = sp.next(track_results)
                        else:
                            track_results = None
                except Exception as exc:
                    if cached and cached.get('snapshot_id') == snapshot_id and cached.get('tracks'):
                        tracks[:] = cached['tracks']
                    logger.warning(
                        "Skipping playlist items for %s (%s): %s",
                        playlist['name'],
                        playlist['id'],
                        exc,
                    )
                    playlist_record['tracks_error'] = str(exc)

            playlists.append(playlist_record)
            updated_cache[playlist['id']] = {
                'snapshot_id': snapshot_id,
                'tracks': tracks,
            }
            if 'tracks_error' in playlist_record:
                updated_cache[playlist['id']]['tracks_error'] = playlist_record['tracks_error']
            save_json_file(PLAYLIST_CACHE_NAME, updated_cache)

        if results['next']:
            results = sp.next(results)
        else:
            results = None

    return playlists


def backup_liked_tracks(sp):
    """Backup liked tracks."""
    logger.info("Backing up liked tracks...")
    tracks = []
    results = sp.current_user_saved_tracks()

    while results:
        for item in results['items']:
            track = item['track']
            album = track.get('album') or {}
            tracks.append({
                'id': track.get('id'),
                'name': track.get('name'),
                'artists': [
                    {'id': artist.get('id'), 'name': artist.get('name')}
                    for artist in track.get('artists', [])
                ],
                'album': {
                    'id': album.get('id'),
                    'name': album.get('name'),
                    'release_date': album.get('release_date'),
                },
                'duration_ms': track.get('duration_ms'),
                'popularity': track.get('popularity'),
                'added_at': item.get('added_at'),
                'uri': track.get('uri'),
            })

        if results['next']:
            results = sp.next(results)
        else:
            results = None

    return tracks


def backup_saved_albums(sp):
    """Backup saved albums."""
    logger.info("Backing up saved albums...")
    albums = []
    results = sp.current_user_saved_albums()

    while results:
        for item in results['items']:
            album = item['album']
            albums.append({
                'id': album['id'],
                'name': album['name'],
                'artists': [{'id': a['id'], 'name': a['name']} for a in album['artists']],
                'release_date': album.get('release_date'),
                'album_type': album['album_type'],
                'total_tracks': album['total_tracks'],
                'images': album.get('images'),
                'added_at': item['added_at'],
            })

        if results['next']:
            results = sp.next(results)
        else:
            results = None

    return albums


def backup_followed_artists(sp):
    """Backup followed artists."""
    logger.info("Backing up followed artists...")
    artists = []
    results = sp.current_user_followed_artists()

    while results:
        for artist in results['artists']['items']:
            artists.append({
                'id': artist['id'],
                'name': artist['name'],
                'popularity': artist.get('popularity'),
                'genres': artist.get('genres', []),
                'images': artist.get('images'),
                'uri': artist['uri'],
            })

        if results['artists']['next']:
            results = sp.current_user_followed_artists(after=results['artists']['cursors']['after'])
        else:
            results = None

    return artists


def save_backup(data):
    """Publish one full snapshot and atomically update the current pointer."""
    root = Path(BACKUP_DIR)
    staging = new_staging(root)
    try:
        (staging / CURRENT_BACKUP_NAME).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        current = publish_snapshot(staging, root, SNAPSHOT_RETENTION)
        filename = str(current / CURRENT_BACKUP_NAME)
        logger.info("Backup saved to %s", filename)
        return filename
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    logger.info("Starting Spotify backup...")

    if not CLIENT_ID or not CLIENT_SECRET:
        logger.error("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are required")
        return 1

    if not os.path.exists(CACHE_PATH):
        logger.error(
            "OAuth cache not found at %s. Run spotify-backup/auth_helper.py externally first.",
            CACHE_PATH,
        )
        return 1

    try:
        sp = get_spotify_client()
        profile = backup_profile(sp)

        backup_data = {
            'timestamp': datetime.now().isoformat(),
            'profile': profile,
            'playlists': backup_playlists(sp, profile.get('id')),
            'liked_tracks': backup_liked_tracks(sp),
            'saved_albums': backup_saved_albums(sp),
            'followed_artists': backup_followed_artists(sp),
        }

        incomplete = [playlist for playlist in backup_data['playlists'] if playlist.get('tracks_error')]
        backup_data['playlist_items_unavailable'] = len(incomplete)
        save_backup(backup_data)

        if incomplete:
            logger.warning(
                'Backup saved with access limits: items unavailable for %d playlist(s)',
                len(incomplete),
            )
        else:
            logger.info("Backup complete!")
        logger.info(f"  - Profile: {backup_data['profile']['display_name']}")
        logger.info(f"  - Playlists: {len(backup_data['playlists'])}")
        logger.info(f"  - Liked tracks: {len(backup_data['liked_tracks'])}")
        logger.info(f"  - Saved albums: {len(backup_data['saved_albums'])}")
        logger.info(f"  - Followed artists: {len(backup_data['followed_artists'])}")
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        return 1

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
