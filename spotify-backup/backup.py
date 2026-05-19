#!/usr/bin/env python3
"""
Spotify Backup - Export user data to JSON
Run via Docker: docker run -v $(pwd)/data:/data spotify-backup
"""

import os
import json
import logging
from datetime import datetime
from spotipy import Spotify, SpotifyException
from spotipy.oauth2 import SpotifyOAuth

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
REDIRECT_URI = os.getenv('SPOTIFY_REDIRECT_URI', 'https://localhost:8888/callback')
BACKUP_DIR = os.getenv('BACKUP_DIR', '/data/backup')
CACHE_PATH = os.getenv('CACHE_PATH', '/data/.cache')

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
    if not os.path.exists(CACHE_PATH):
        raise RuntimeError(
            f"No Spotify token cache found at {CACHE_PATH}. "
            "Run auth_helper.py on the host machine first, then restart this container."
        )
    auth_manager = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=' '.join(SCOPES),
        cache_path=CACHE_PATH,
        open_browser=False,
    )
    return Spotify(auth_manager=auth_manager, retries=0)


def _next_page(sp, results):
    """Advance to next page, returning None on rate-limit or error."""
    if not results.get('next'):
        return None
    try:
        return sp.next(results)
    except SpotifyException as e:
        if e.http_status == 429:
            raise
        logger.warning(f"Pagination error (skipping): {e}")
        return None


def backup_profile(sp):
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


def _fetch_playlist_tracks(sp, playlist_id, playlist_name):
    """Fetch tracks for a playlist we own. Returns list of track dicts."""
    tracks = []
    try:
        results = sp.playlist_items(playlist_id)
        while results:
            for item in (results.get('items') or []):
                track = item.get('track') if item else None
                if not track or not track.get('id'):
                    continue
                tracks.append({
                    'id': track.get('id'),
                    'name': track.get('name'),
                    'artists': [{'id': a.get('id'), 'name': a.get('name')} for a in track.get('artists', [])],
                    'album': {
                        'id': track.get('album', {}).get('id'),
                        'name': track.get('album', {}).get('name'),
                        'release_date': track.get('album', {}).get('release_date'),
                    },
                    'duration_ms': track.get('duration_ms'),
                    'popularity': track.get('popularity'),
                    'uri': track.get('uri'),
                })
            results = _next_page(sp, results)
    except SpotifyException as e:
        if e.http_status == 429:
            raise
        logger.warning(f"Could not fetch tracks for '{playlist_name}' (skipped): {e}")
    return tracks


def backup_playlists(sp, user_id):
    """
    Backup playlists.
    For playlists owned by the user: fetch full track list.
    For followed playlists: save metadata only (avoids 403s and rate-limit abuse).
    """
    logger.info("Backing up playlists...")
    playlists = []
    results = sp.current_user_playlists()

    while results:
        for playlist in (results.get('items') or []):
            if not playlist:
                continue
            owner_id = playlist.get('owner', {}).get('id')
            is_mine = owner_id == user_id
            tracks = _fetch_playlist_tracks(sp, playlist['id'], playlist.get('name')) if is_mine else []
            playlists.append({
                'id': playlist.get('id'),
                'name': playlist.get('name'),
                'description': playlist.get('description'),
                'owner': owner_id,
                'mine': is_mine,
                'collaborative': playlist.get('collaborative'),
                'public': playlist.get('public'),
                'tracks_count': playlist.get('tracks', {}).get('total', len(tracks)),
                'tracks': tracks,
            })

        results = _next_page(sp, results)

    return playlists


def backup_liked_tracks(sp):
    logger.info("Backing up liked tracks...")
    tracks = []
    results = sp.current_user_saved_tracks()

    while results:
        for item in (results.get('items') or []):
            track = item.get('track') if item else None
            if not track or not track.get('id'):
                continue
            tracks.append({
                'id': track.get('id'),
                'name': track.get('name'),
                'artists': [{'id': a.get('id'), 'name': a.get('name')} for a in track.get('artists', [])],
                'album': {
                    'id': track.get('album', {}).get('id'),
                    'name': track.get('album', {}).get('name'),
                    'release_date': track.get('album', {}).get('release_date'),
                },
                'duration_ms': track.get('duration_ms'),
                'popularity': track.get('popularity'),
                'added_at': item.get('added_at'),
                'uri': track.get('uri'),
            })
        results = _next_page(sp, results)

    return tracks


def backup_saved_albums(sp):
    logger.info("Backing up saved albums...")
    albums = []
    results = sp.current_user_saved_albums()

    while results:
        for item in (results.get('items') or []):
            album = item.get('album') if item else None
            if not album:
                continue
            albums.append({
                'id': album.get('id'),
                'name': album.get('name'),
                'artists': [{'id': a.get('id'), 'name': a.get('name')} for a in album.get('artists', [])],
                'release_date': album.get('release_date'),
                'album_type': album.get('album_type'),
                'total_tracks': album.get('total_tracks'),
                'images': album.get('images'),
                'added_at': item.get('added_at'),
            })
        results = _next_page(sp, results)

    return albums


def backup_followed_artists(sp):
    logger.info("Backing up followed artists...")
    artists = []
    results = sp.current_user_followed_artists()

    while results:
        for artist in (results.get('artists', {}).get('items') or []):
            if not artist:
                continue
            artists.append({
                'id': artist.get('id'),
                'name': artist.get('name'),
                'popularity': artist.get('popularity'),
                'genres': artist.get('genres', []),
                'images': artist.get('images'),
                'uri': artist.get('uri'),
            })
        cursor = results.get('artists', {})
        if cursor.get('next'):
            after = cursor.get('cursors', {}).get('after')
            results = sp.current_user_followed_artists(after=after)
        else:
            results = None

    return artists


def save_backup(data):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    filename = os.path.join(BACKUP_DIR, f'spotify_backup_{timestamp}.json')
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"Backup saved to {filename}")
    backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith('spotify_backup_') and f.endswith('.json')])
    for old in backups[:-1]:
        os.remove(os.path.join(BACKUP_DIR, old))
        logger.info(f"Removed old backup: {old}")
    return filename


def main():
    logger.info("Starting Spotify backup...")
    if not CLIENT_ID or not CLIENT_SECRET:
        logger.error("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are required")
        return

    sp = get_spotify_client()
    profile = backup_profile(sp)
    user_id = profile['id']
    logger.info(f"Authenticated as: {profile.get('display_name')} ({user_id})")

    backup_data = {
        'timestamp': datetime.now().isoformat(),
        'profile': profile,
        'playlists': backup_playlists(sp, user_id),
        'liked_tracks': backup_liked_tracks(sp),
        'saved_albums': backup_saved_albums(sp),
        'followed_artists': backup_followed_artists(sp),
    }

    save_backup(backup_data)

    own = sum(1 for p in backup_data['playlists'] if p.get('mine'))
    logger.info("Backup complete!")
    logger.info(f"  - Profile: {profile.get('display_name')}")
    logger.info(f"  - Playlists: {len(backup_data['playlists'])} ({own} own, {len(backup_data['playlists']) - own} followed — tracks only for own)")
    logger.info(f"  - Liked tracks: {len(backup_data['liked_tracks'])}")
    logger.info(f"  - Saved albums: {len(backup_data['saved_albums'])}")
    logger.info(f"  - Followed artists: {len(backup_data['followed_artists'])}")


if __name__ == '__main__':
    main()
