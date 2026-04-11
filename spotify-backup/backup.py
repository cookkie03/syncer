#!/usr/bin/env python3
"""
Spotify Backup - Export user data to JSON
Run via Docker: docker run -v $(pwd)/data:/data spotify-backup
"""

import os
import json
import logging
from datetime import datetime
from spotipy import Spotify
from spotipy.oauth2 import SpotifyOAuth

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# Configuration
CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
REDIRECT_URI = os.getenv('SPOTIFY_REDIRECT_URI', 'https://localhost:8888/callback')
BACKUP_DIR = os.getenv('BACKUP_DIR', '/data/backup')
CACHE_PATH = os.getenv('CACHE_PATH', '/data/.cache')

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
    )
    return Spotify(auth_manager=auth_manager)


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


def backup_playlists(sp):
    """Backup all playlists with tracks."""
    logger.info("Backing up playlists...")
    playlists = []
    results = sp.current_user_playlists()
    
    while results:
        for playlist in results['items']:
            # Get tracks for each playlist
            tracks = []
            track_results = sp.playlist_items(playlist['id'])
            while track_results:
                for item in track_results['items']:
                    if item['track']:
                        track = item['track']
                        tracks.append({
                            'id': track['id'],
                            'name': track['name'],
                            'artists': [{'id': a['id'], 'name': a['name']} for a in track['artists']],
                            'album': {
                                'id': track['album']['id'],
                                'name': track['album']['name'],
                                'release_date': track['album'].get('release_date'),
                            },
                            'duration_ms': track['duration_ms'],
                            'popularity': track['popularity'],
                            'uri': track['uri'],
                        })
                if track_results['next']:
                    track_results = sp.next(track_results)
                else:
                    track_results = None
            
            playlists.append({
                'id': playlist['id'],
                'name': playlist['name'],
                'description': playlist['description'],
                'owner': playlist['owner']['id'],
                'collaborative': playlist['collaborative'],
                'public': playlist['public'],
                'tracks_count': playlist['tracks']['total'],
                'tracks': tracks,
            })
        
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
            tracks.append({
                'id': track['id'],
                'name': track['name'],
                'artists': [{'id': a['id'], 'name': a['name']} for a in track['artists']],
                'album': {
                    'id': track['album']['id'],
                    'name': track['album']['name'],
                    'release_date': track['album'].get('release_date'),
                },
                'duration_ms': track['duration_ms'],
                'popularity': track['popularity'],
                'added_at': item['added_at'],
                'uri': track['uri'],
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
    """Save backup to JSON file."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    
    timestamp = datetime.now().strftime('%Y-%m-%d_%H%M%S')
    filename = os.path.join(BACKUP_DIR, f'spotify_backup_{timestamp}.json')
    
    with open(filename, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    
    logger.info(f"Backup saved to {filename}")
    
    # Remove old backups, keep only latest
    backups = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith('spotify_backup_') and f.endswith('.json')])
    for old in backups[:-1]:
        old_path = os.path.join(BACKUP_DIR, old)
        os.remove(old_path)
        logger.info(f"Removed old backup: {old}")
    
    return filename


def main():
    logger.info("Starting Spotify backup...")
    
    # Check credentials
    if not CLIENT_ID or not CLIENT_SECRET:
        logger.error("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are required")
        return
    
    try:
        sp = get_spotify_client()
        
        # Run all backups
        backup_data = {
            'timestamp': datetime.now().isoformat(),
            'profile': backup_profile(sp),
            'playlists': backup_playlists(sp),
            'liked_tracks': backup_liked_tracks(sp),
            'saved_albums': backup_saved_albums(sp),
            'followed_artists': backup_followed_artists(sp),
        }
        
        # Save to file
        save_backup(backup_data)
        
        logger.info("Backup complete!")
        
        # Log summary
        logger.info(f"  - Profile: {backup_data['profile']['display_name']}")
        logger.info(f"  - Playlists: {len(backup_data['playlists'])}")
        logger.info(f"  - Liked tracks: {len(backup_data['liked_tracks'])}")
        logger.info(f"  - Saved albums: {len(backup_data['saved_albums'])}")
        logger.info(f"  - Followed artists: {len(backup_data['followed_artists'])}")
        
    except Exception as e:
        logger.error(f"Backup failed: {e}")
        raise


if __name__ == '__main__':
    main()