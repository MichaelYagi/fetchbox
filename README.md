# Fetchbox

Self-hosted media manager for movies and TV: discover a title, pick a quality profile, and
Fetchbox downloads and organizes it. Works out of the box with a built-in torrent engine.

Status: early development — see [PLAN.md](PLAN.md) and the engineering specification.

## Development

```sh
uv sync                     # Python 3.12 or 3.13
uv run fetchbox serve --data ./data
# API docs: http://127.0.0.1:8787/api/docs
uv run pytest
uv run ruff check . && uv run mypy && uv run lint-imports
```

License: MIT.
