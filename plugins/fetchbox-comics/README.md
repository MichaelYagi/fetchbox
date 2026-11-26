# fetchbox-comics

Comics for Fetchbox: comic series and their issues, with metadata from [ComicVine](https://comicvine.gamespot.com). A reference plugin for the [Fetchbox plugin API](../../docs/plugins.md): a media kind.

- A series is found by name on the Add page; its issues, with their numbers and release dates, are what's wanted and downloaded.
- Release names like `Saga 061 (2023) (Digital) (Zone-Empire).cbz`, `The Walking Dead #193 (2019).cbr` and runs like `Saga v01 001-006 (2012).cbz` are read; collected editions (TPB, omnibus) and annuals aren't issues.
- Files are named `Saga (2012)/Saga #061 (2023).cbz` by default; the tokens are `series`, `start_year`, `issue`, `year`, `title` and `publisher`.
- Indexers are asked for "Saga 061" in Books > Comics (7030).

Comics in the library, on the Add page and through the download pipeline come with installing plugins in Fetchbox; until then the kind is complete and tested on its own.

## Settings

| Setting | |
|---|---|
| ComicVine API key | Free: sign up at comicvine.gamespot.com/api |

## Develop

```sh
fetchbox plugin test .     # a fake ComicVine, a table of real release names (tests/cases.txt)
fetchbox plugin check .
```
