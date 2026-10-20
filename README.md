# ContainerTLDR

Tells you when a container you run has a newer image, and what actually changed upstream: breaking changes,
security fixes and highlights, summarised by an LLM from the release notes.

Diun, What's Up Docker and Watchtower tell you *that* an image changed. You still have to open a few GitHub
release pages to decide whether it's safe to update. ContainerTLDR reads them for you and sends one notification
per update:

```
⬆️ radarr: 6.1.1.10360 → 6.4.4.10685
Breaking / action needed: None mentioned
Security: None mentioned
Highlights:
- Improved handling of qBittorrent authentication and compatibility with newer versions.
- Added support for RQBit download client.
- Enhanced logging and error handling for various operations.
- Fixed issues with parsing and importing items from download clients.
- Improved reliability and performance in managing media files and database operations.
Verdict: Safe to update - No breaking changes or security issues mentioned, with several performance and
compatibility improvements.
Stack: arr · Image: lscr.io/linuxserver/radarr:latest
Summarised by qwen3:8b from 3 release notes
```

That summary is real qwen3:8b output for Radarr's 6.2.1, 6.3.0 and 6.4.4 releases. On Discord it arrives as an
embed whose title links to the release notes; other services get the same text.

Images rebuilt with the same app version, like LinuxServer's weekly base image updates, aren't summarised. They're
listed together in one short message.

ContainerTLDR never updates or restarts anything. It only tells you.

## How it works

1. On a schedule (04:30 by default) it lists your running containers through the Docker API.
2. For each image it asks the registry for the tag's current digest. That's a HEAD request, which doesn't count
   against Docker Hub's pull limit.
3. When the digest has changed, it reads the app version from the running image and the new one (the
   `org.opencontainers.image.version` label or an environment variable), finds the upstream GitHub repo, and
   collects every release note in between.
4. An LLM summarises them, and the summary goes to Discord or any of the services
   [Apprise](https://github.com/caronc/apprise) supports (ntfy, Telegram, Gotify, Slack, email and many more).
5. It remembers what it reported, so each update is reported once. A weekly rebuild of an update you've already
   been told about stays quiet.

## Quick start

Copy [examples/docker-compose.yml](examples/docker-compose.yml), fill in your notification URL and where your
Ollama runs, then:

```sh
docker compose up -d
```

The example runs ContainerTLDR behind a read-only Docker socket proxy, with a read-only root filesystem.

The first run records what's already pending without sending anything, so installing it doesn't set off a burst of
messages. To see a notification straight away, post a test summary for one of your containers:

```sh
docker compose exec containertldr containertldr --test radarr
```

That summarises the last few releases of the container's app and posts the result with a `[TEST]` prefix. To see
what a normal check would send, without sending or saving anything:

```sh
docker compose run --rm containertldr --once --dry-run
```

## Configuration

Everything is set with environment variables.

| Variable | Default | |
|---|---|---|
| `CTLDR_NOTIFY_URLS` | required | Apprise URLs, separated by commas or newlines. Discord webhooks (`discord://id/token` or `https://discord.com/api/webhooks/...`) get rich embeds |
| `CTLDR_SCHEDULE` | `30 4 * * *` | When to check, as a cron expression |
| `CTLDR_TZ` or `TZ` | `UTC` | Timezone for the schedule |
| `CTLDR_DOCKER_HOST` | `unix:///var/run/docker.sock` | Or `tcp://socket-proxy:2375` |
| `CTLDR_LLM_PROVIDER` | `ollama` | `ollama`, `openai_compat` (LM Studio, vLLM, llama.cpp, LocalAI and others) or `none`, which sends the first lines of each release instead of a summary |
| `CTLDR_LLM_URL` | `http://ollama:11434` | For `openai_compat`, the base URL including `/v1` |
| `CTLDR_LLM_MODEL` | `qwen3:8b` | |
| `CTLDR_LLM_API_KEY` | | For hosted OpenAI-compatible APIs |
| `CTLDR_LLM_NUM_CTX` | `8192` | Ollama's context size |
| `CTLDR_NOTES_MAX_CHARS` | `18000` | How much release-note text goes to the model at most |
| `GITHUB_TOKEN` | | Raises GitHub's limit from 60 to 5000 requests an hour. It needs no scopes |
| `CTLDR_INITIAL_RUN` | `baseline` | What the first run does with updates that are already pending: `baseline` (record them quietly) or `notify` |
| `CTLDR_NOTIFY_REBUILDS` | `true` | Send the grouped "rebuilt images" message |
| `CTLDR_INCLUDE_LABEL` | | Only check containers with this label (`key` or `key=value`) |
| `CTLDR_EXCLUDE_LABEL` | | Never check containers with this label |
| `CTLDR_INSECURE_REGISTRIES` | | Registries to reach over plain http, comma-separated |
| `CTLDR_DATA_DIR` | `/data` | Where it keeps what it has reported |
| `CTLDR_LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error` |

`CTLDR_NOTIFY_URLS`, `CTLDR_LLM_API_KEY` and `GITHUB_TOKEN` also work with a `_FILE` suffix pointing at a file,
for Docker secrets.

## Container labels

Most images need nothing. When ContainerTLDR picks the wrong repo or can't find an image's version, label the
container:

| Label | |
|---|---|
| `containertldr.enable=false` | Skip this container |
| `containertldr.repo` | The GitHub repo with the release notes, as `owner/name` |
| `containertldr.version` | Where the image keeps its app version: `label` (`org.opencontainers.image.version`, the default), `env:NAME`, or `none` to go by the images' build dates |
| `containertldr.tag-prefix` | Only count release tags starting with this, for repos that release several things |
| `containertldr.notes-url` | Link to send when there are no release notes on GitHub |
| `containertldr.commits=true` | The image is built from a branch: summarise the commits between the two builds |

For example, an app from a repo that also releases other packages, with its version in an environment variable:

```yaml
services:
  app:
    image: ghcr.io/example/app:latest
    labels:
      containertldr.version: env:APP_VERSION
      containertldr.tag-prefix: "@example/app@"
```

Without a label, the repo comes from a short list of built-in defaults for popular images whose own labels point
at the wrong place (LinuxServer's, for example), then from the image's `org.opencontainers.image.source` label,
then from its `ghcr.io/owner/name` path. When the repo was guessed, the notification says so.

## Models

Summaries run one at a time. On a GTX 1080, qwen3:8b with an 8k context takes 20 to 50 seconds per summary.
Checks run at night, so slower hardware is fine too; it just takes longer.

Any OpenAI-compatible endpoint works with `CTLDR_LLM_PROVIDER=openai_compat`. With `none`, no model is needed.
If the model can't be reached, the update is still reported, marked "summary unavailable".

## Private registries

Mount your `~/.docker/config.json` at `/config/config.json`. Logins kept by a credential helper (`credsStore` in
that file) aren't supported yet.

## Without the socket proxy

You can mount `/var/run/docker.sock` directly and leave `CTLDR_DOCKER_HOST` at its default. The container runs
as uid 1000, so it also needs the socket's group: add `group_add: ["<gid>"]` to the service, where `<gid>` comes
from `stat -c %g /var/run/docker.sock`. The proxy is the safer choice, because ContainerTLDR then can't do anything
but read.

## Security

ContainerTLDR only reads from Docker, only makes outbound requests and never logs secrets. Release notes are
treated as untrusted text: the model has no tools, can't ping anyone, and its verdict is checked against the
breaking changes it lists. See [SECURITY.md](SECURITY.md) for the details and how to report a problem.

## Limitations

- Release notes come from GitHub only, for now.
- Only the tag you run is checked. Running `postgres:18`, you won't hear about `postgres:19`.
- Registry logins from credential helpers aren't supported.
- A summary is only as good as the release notes and the model. For updates that matter, read the notes too.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[MIT](LICENSE)
