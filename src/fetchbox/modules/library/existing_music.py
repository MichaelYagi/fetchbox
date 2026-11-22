"""Import an existing music folder: albums already on disk, recorded where they are.

The same flow as movies and shows (scan, review, apply; nothing changes before apply):

* **Scan** reads every audio file and groups them into albums by their tags (album artist
  and album), or by folder when untagged. MusicBrainz ids in the tags (Picard and Lidarr
  write them) match exactly; otherwise the artist and album names are looked up on
  MusicBrainz, the artist's albums fetched once per artist.
* **Apply** adds each artist with nothing else wanted ("none": only what's imported),
  picks the edition whose track count matches the files, matches files to tracks, and
  records them in place: the album is acquired, its quality what the files are.
"""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

import structlog
from sqlalchemy import select

from fetchbox.core.db import Database, utcnow
from fetchbox.core.errors import ProblemError
from fetchbox.core.events import Actor
from fetchbox.modules.library import music_files as mfiles
from fetchbox.modules.library.models import MediaFile, MediaFileItem
from fetchbox.modules.media.models import Album, Title, Track, WantedItem
from fetchbox.modules.media.music import MusicService
from fetchbox.modules.profiles.service import ProfilesService, to_spec
from fetchbox.modules.releases.music import norm
from fetchbox.modules.storage.models import Destination
from fetchbox.providers.metadata.musicbrainz import AlbumResult, MusicBrainzError

log = structlog.get_logger(__name__)


def _tag(path: Path, *keys: str) -> str | None:
    """A text tag by Vorbis / ID3 (TXXX) / MP4 name, for grouping and MusicBrainz ids."""
    import mutagen

    try:
        m = mutagen.File(path)
    except Exception:
        return None
    if m is None or m.tags is None:
        return None
    for k in keys:
        try:
            v = m.tags.get(k)
        except (KeyError, ValueError):
            v = None
        if v:
            v = v[0] if isinstance(v, list) else v
            text = str(getattr(v, "text", [v])[0] if hasattr(v, "text") else v)
            if text.strip():
                return text.strip()
    return None


_YEARED = re.compile(r"^(.*?)\s*[\(\[](\d{4})[\)\]]\s*$")


def scan_tree(root: Path) -> list[dict[str, Any]]:
    """Albums under ``root``: their files grouped by (album artist, album) tags, else
    by folder."""
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(filenames):
            if not mfiles.is_audio(name):
                continue
            p = Path(dirpath) / name
            rel = p.relative_to(root).as_posix()
            artist = _tag(p, "albumartist", "TPE2", "aART", "artist", "TPE1", "\xa9ART")
            album = _tag(p, "album", "TALB", "\xa9alb")
            folder = str(PurePosixPath(rel).parent)
            if folder.split("/")[-1].lower().startswith(("cd", "disc", "disk")) and "/" in folder:
                folder = str(PurePosixPath(folder).parent)  # CD1 / CD2 belong together
            year_tag = _tag(p, "date", "TDRC", "\xa9day")
            if not (artist and album):  # untagged: Artist/Album (Year)/... folders
                parts = PurePosixPath(folder).parts
                if m := _YEARED.match(parts[-1] if parts else ""):
                    album = album or m.group(1)
                    year_tag = year_tag or m.group(2)
                else:
                    album = album or (parts[-1] if parts else None)
                artist = artist or (parts[-2] if len(parts) >= 2 else None)
            key = (norm(artist or ""), norm(album or "")) if artist and album else ("", folder)
            g = groups.setdefault(
                key,
                {
                    "folder": folder,
                    "files": [],
                    "artist": artist,
                    "album": album or PurePosixPath(folder).name,
                    "artist_mbid": _tag(p, "musicbrainz_artistid", "TXXX:MusicBrainz Artist Id"),
                    "album_mbid": _tag(
                        p, "musicbrainz_releasegroupid", "TXXX:MusicBrainz Release Group Id"
                    ),
                    "year": year_tag,
                },
            )
            g["files"].append({"path": rel, "size": p.stat().st_size})
    return list(groups.values())


class ExistingMusic:
    def __init__(self, db: Database, *, music: MusicService, profiles: ProfilesService) -> None:
        self.db = db
        self.music = music
        self.profiles = profiles

    # ---- scan ---------------------------------------------------------------------------------

    async def scan(self, dest: Destination, progress: Any = None) -> dict[str, Any]:
        root = Path(dest.path)
        items = await asyncio.to_thread(scan_tree, root)
        async with self.db.sessionmaker() as s:
            have = {
                a.mbid: (a, t)
                for a, t in (
                    await s.execute(select(Album, Title).join(Title, Title.id == Album.title_id))
                ).all()
            }
            recorded = set(
                (
                    await s.execute(
                        select(MediaFile.relative_path).where(MediaFile.destination_id == dest.id)
                    )
                ).scalars()
            )
        artists: dict[str, tuple[str | None, str | None, list[AlbumResult]]] = {}
        mb = self.music.client()
        try:
            for n, item in enumerate(items, 1):
                await self._match(mb, item, artists)
                m = item["match"]
                known = have.get(m["album_mbid"]) if m else None
                item["title_id"] = known[1].id if known else None
                paths = [f["path"] for f in item["files"]]
                item["status"] = (
                    "imported"
                    if all(p in recorded for p in paths)
                    else "in_library"
                    if known
                    else "new"
                )
                item["size"] = sum(f["size"] for f in item["files"])
                if progress and (n % 5 == 0 or n == len(items)):
                    await progress(n / max(len(items), 1), f"{n} of {len(items)} albums matched")
        finally:
            await mb.aclose()
        return {
            "destination_id": dest.id,
            "kind": "music",
            "path": dest.path,
            "items": items,
            "counts": {
                s: sum(i["status"] == s for i in items) for s in ("new", "in_library", "imported")
            },
        }

    async def _match(self, mb: Any, item: dict[str, Any], artists: dict[str, Any]) -> None:
        """item["match"] = {artist_mbid, artist, album_mbid, album, year} or None."""
        item["match"], item["confidence"] = None, "none"
        try:
            artist_mbid, artist_name = item.get("artist_mbid"), item.get("artist")
            if not artist_mbid and artist_name:
                key = norm(artist_name)
                if key not in artists:
                    hits = await mb.search_artists(artist_name, limit=5)
                    best = next((h for h in hits if norm(h.name) == key), None)
                    artists[key] = (best.id if best else None, best.name if best else None, [])
                artist_mbid, artist_name = artists[key][0], artists[key][1] or artist_name
            if not artist_mbid:
                return
            if artist_mbid not in artists or not artists[artist_mbid][2]:
                albums = await mb.albums(artist_mbid)
                artists[artist_mbid] = (artist_mbid, artist_name, albums)
            albums = artists[artist_mbid][2]
            exact = next((a for a in albums if a.id == item.get("album_mbid")), None)
            want = norm(item.get("album") or "")
            named = [a for a in albums if norm(a.title) == want]
            year = str(item.get("year") or "")[:4]
            dated = [a for a in named if a.release_date and str(a.release_date.year) == year]
            pick = exact or (dated or named or [None])[0]
            if pick is None:
                return
            item["match"] = {
                "artist_mbid": artist_mbid,
                "artist": artist_name,
                "album_mbid": pick.id,
                "album": pick.title,
                "year": pick.release_date.year if pick.release_date else None,
                "kind": pick.kind,
            }
            item["confidence"] = (
                "exact" if exact else ("high" if dated or len(named) == 1 else "low")
            )
        except MusicBrainzError as exc:
            item["error"] = exc.message

    # ---- apply --------------------------------------------------------------------------------

    async def apply_one(
        self, dest: Destination, item: dict[str, Any], actor: Actor
    ) -> tuple[Title, bool, int]:
        """(artist, newly added, files recorded) for one album item:
        {folder, files, artist_mbid, album_mbid, profile_id}."""
        new = False
        async with self.db.sessionmaker() as s:
            title = (
                await s.execute(
                    select(Title).where(Title.kind == "music", Title.mbid == item["artist_mbid"])
                )
            ).scalar_one_or_none()
        if title is None:
            title = await self.music.add_artist(
                item["artist_mbid"],
                profile_id=item["profile_id"],
                monitor_mode="none",
                album_kinds=["album", "ep", "single", "live", "compilation", "other"],
                actor=actor,
                search_after=utcnow(),
            )
            new = True
        async with self.db.sessionmaker() as s:
            album = (
                await s.execute(
                    select(Album).where(
                        Album.title_id == title.id, Album.mbid == item["album_mbid"]
                    )
                )
            ).scalar_one_or_none()
        if album is None:
            raise ProblemError(
                422,
                "Validation Failed",
                "MusicBrainz doesn't list this album for the artist",
                slug="validation",
            )
        root = Path(dest.path)
        audio = [
            await asyncio.to_thread(mfiles.read, root / p, p)
            for p in item["files"]
            if mfiles.is_audio(p)
        ]
        # the edition with as many tracks as there are files (else the standard one)
        eds = await self.music.editions(album.id)
        fit = next((e for e in eds if e.track_count == len(audio)), None)
        album = await self.music.choose_edition(album.id, fit.id if fit else None)
        async with self.db.sessionmaker() as s:
            rows = list(
                (
                    await s.execute(
                        select(Track)
                        .where(Track.album_id == album.id)
                        .order_by(Track.disc, Track.number)
                    )
                ).scalars()
            )
            w = (
                await s.execute(select(WantedItem).where(WantedItem.album_id == album.id))
            ).scalar_one()
        tracks = [mfiles.WantedTrack(t.id, t.disc, t.number, t.name, t.length_ms) for t in rows]
        m = mfiles.match(audio, tracks)
        if not m.pairs:
            raise ProblemError(
                422, "Validation Failed", "no file matches a track of the album", slug="validation"
            )
        quality = mfiles.album_quality(list(m.pairs.values()))
        profile = to_spec(await self.profiles.get_profile(title.profile_id))
        tier = profile.tier_index(quality)
        complete = not m.missing
        async with self.db.sessionmaker() as s:
            for track_id, af in m.pairs.items():
                if (
                    await s.execute(
                        select(MediaFile.id).where(
                            MediaFile.storage_node_id == dest.node_id,
                            MediaFile.relative_path == af.rel,
                        )
                    )
                ).scalar():
                    continue  # recorded before
                row = MediaFile(
                    storage_node_id=dest.node_id,
                    destination_id=dest.id,
                    relative_path=af.rel,
                    size=af.size,
                    mtime=utcnow(),
                    quality={"name": af.quality, "codec": af.codec},
                    original_name=af.rel,
                    link_mode="existing",
                    track_id=track_id,
                    mediainfo={"length_s": af.length_s, "tags_written": True},
                )
                s.add(row)
                await s.flush()
                s.add(MediaFileItem(media_file_id=row.id, wanted_item_id=w.id))
            row_w = await s.get(WantedItem, w.id)
            assert row_w is not None
            row_w.monitored = True
            if complete:  # every track has its file: the album is had
                row_w.state, row_w.lock = "acquired", "acquired"
                row_w.cutoff_met = tier is not None and tier >= profile.cutoff_index
            row_w.version += 1
            await s.commit()
        return title, new, len(m.pairs)
