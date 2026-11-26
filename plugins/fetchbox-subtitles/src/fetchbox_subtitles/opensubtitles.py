"""The OpenSubtitles REST API (api.opensubtitles.com, v1), as much of it as this plugin uses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

API = "https://api.opensubtitles.com/api/v1"
HOST = "api.opensubtitles.com"
TOKEN_SECONDS = 23 * 3600  # a login token lasts a day


class OpenSubtitlesError(Exception):
    pass


class QuotaReached(OpenSubtitlesError):
    """No downloads left today; ``reset`` says when (as OpenSubtitles writes it)."""

    def __init__(self, reset: str) -> None:
        super().__init__(f"no downloads left until {reset}")
        self.reset = reset


@dataclass(frozen=True)
class Found:
    file_id: int
    language: str
    release: str
    downloads: int
    hash_match: bool
    hearing_impaired: bool
    machine_made: bool


class OpenSubtitles:
    def __init__(self, ctx: Any, api_key: str, username: str = "", password: str = "") -> None:
        self.ctx = ctx
        self.api_key = api_key
        self.username = username
        self.password = password
        ctx.http.limit(HOST, per_second=1)  # what OpenSubtitles asks of apps

    def _headers(self, token: str | None = None) -> dict[str, str]:
        h = {"Api-Key": self.api_key, "Accept": "application/json"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    async def _token(self) -> str | None:
        """Signed in, downloads count against the account (more a day); optional."""
        if not (self.username and self.password):
            return None

        async def login() -> str | None:
            r = await self.ctx.http.post(
                f"{API}/login",
                headers=self._headers(),
                json={"username": self.username, "password": self.password},
            )
            if r.status_code != 200:
                raise OpenSubtitlesError(
                    f"signing in failed ({r.status_code}): check the username and password"
                )
            return str(r.json().get("token") or "") or None

        token: str | None = await self.ctx.cache.remember(
            f"token:{self.username}", TOKEN_SECONDS, login
        )
        return token

    async def check(self) -> str:
        r = await self.ctx.http.get(f"{API}/infos/languages", headers=self._headers())
        if r.status_code in (401, 403):
            raise OpenSubtitlesError("OpenSubtitles refused the API key")
        r.raise_for_status()
        if self.username:
            await self._token()
            return "The API key and the account work"
        return "The API key works"

    async def search(self, params: dict[str, Any]) -> list[Found]:
        r = await self.ctx.http.get(f"{API}/subtitles", headers=self._headers(), params=params)
        if r.status_code in (401, 403):
            raise OpenSubtitlesError("OpenSubtitles refused the API key")
        r.raise_for_status()
        out = []
        for d in r.json().get("data") or []:
            a = d.get("attributes") or {}
            for f in a.get("files") or []:
                if isinstance(f.get("file_id"), int):
                    out.append(
                        Found(
                            file_id=f["file_id"],
                            language=str(a.get("language") or ""),
                            release=str(a.get("release") or f.get("file_name") or ""),
                            downloads=int(a.get("download_count") or 0),
                            hash_match=bool(a.get("moviehash_match")),
                            hearing_impaired=bool(a.get("hearing_impaired")),
                            machine_made=bool(
                                a.get("ai_translated") or a.get("machine_translated")
                            ),
                        )
                    )
        return out

    async def download(self, file_id: int) -> bytes:
        r = await self.ctx.http.post(
            f"{API}/download",
            headers=self._headers(await self._token()),
            json={"file_id": file_id, "sub_format": "srt"},
        )
        if r.status_code == 406 or (r.status_code == 200 and r.json().get("remaining") == -1):
            raise QuotaReached(str(r.json().get("reset_time") or "tomorrow"))
        r.raise_for_status()
        link = r.json().get("link")
        if not link:
            raise OpenSubtitlesError("OpenSubtitles gave no download link")
        got = await self.ctx.http.get(link)
        got.raise_for_status()
        return bytes(got.content)
