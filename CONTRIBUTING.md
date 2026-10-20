# Contributing

Bug reports and pull requests are welcome. For anything bigger than a small fix, please open an issue first so we
can agree on the approach before you spend time on it.

## Setting up

You need [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run pytest
uv run ruff check && uv run ruff format --check
```

To try it against your own Docker without sending or saving anything:

```sh
CTLDR_NOTIFY_URLS=discord://x/y CTLDR_LLM_PROVIDER=none uv run containertldr --once --dry-run
```

Set `CTLDR_DOCKER_HOST` if your socket isn't at `/var/run/docker.sock` (Docker Desktop on macOS keeps it at
`unix://$HOME/.docker/run/docker.sock`).

## Tests

The tests never touch the network. Responses from GitHub and the registries are recorded under `tests/fixtures/`
and served through httpx's mock transport. If you change how a response is read, record a fresh one rather than
writing it by hand.

## Image defaults

`src/containertldr/mappings.yaml` only holds defaults for widely used images whose own labels point at the wrong
place. For everything else, the `containertldr.*` labels on the container are the way to configure it. If you think
an image belongs in the defaults, say why in the pull request: what the image's labels say, and what goes wrong
without the entry.
