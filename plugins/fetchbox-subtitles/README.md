# fetchbox-subtitles

Subtitles from [OpenSubtitles](https://www.opensubtitles.com) for movies and episodes, filed beside them. A reference plugin for the [Fetchbox plugin API](../../docs/plugins.md).

- When a movie or an episode is imported or upgraded, subtitles in your languages are written beside it as `<its name>.<language>.srt`. Fetchbox renames, moves and deletes them together with the file.
- The best match is a subtitle timed to that exact file (OpenSubtitles' file hash), then one made by people rather than machine translated, then the most downloaded.
- A language not found yet is tried again once a day; **Search subtitles** on a movie or episode tries at once.
- When the day's downloads run out, it waits until OpenSubtitles resets them.

**Install:** in Fetchbox, Settings > Plugins, with its address from a Fetchbox release; see [Plugins](../../docs/plugin-list.md#subtitles).

## Settings

| Setting | |
|---|---|
| OpenSubtitles API key | Free: sign up at opensubtitles.com, then *API Consumers* in your profile |
| Languages | Two-letter codes, best first: `en`, `fr` |
| Username, password | Optional: signed in, you get more downloads a day |
| Prefer hearing impaired | Off by default |

## Develop

```sh
fetchbox plugin test .     # its tests: a fake OpenSubtitles, a real Fetchbox (offline)
fetchbox plugin check .
fetchbox plugin dev .
```
