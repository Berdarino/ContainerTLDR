# Security

## Reporting a problem

Please report security problems privately through GitHub: open the Security tab of this repository and choose
"Report a vulnerability". Don't open a public issue for them.

## What ContainerTLDR can and can't do

- It only reads from the Docker API: listing and inspecting containers and images. Run it behind a socket proxy
  with `POST=0`, as in [examples/docker-compose.yml](examples/docker-compose.yml), so it couldn't do more even if
  it tried.
- It only makes outbound requests: to container registries, the GitHub API, your LLM and your notification
  services. It doesn't listen on any port.
- It runs as a non-root user and only writes to `/data`, so a read-only root filesystem works.
- Secrets (notification URLs, `GITHUB_TOKEN`, the LLM API key, registry logins) are never logged. The settings
  printed at startup leave them out, and notification URLs in log messages are cut down to their scheme and host.

## Release notes are untrusted text

ContainerTLDR gives third-party release notes to an LLM, so a release note could attempt prompt injection
("ignore your instructions and..."). The design keeps that contained:

- The model has no tools and can't take any action. The only thing its reply becomes is the text of a
  notification.
- Release notes are wrapped in `<notes>` tags and the model is told to treat them as data. A release note can't
  close the tag early.
- The reply is capped at 600 tokens and cut to the notification service's limits.
- Notifications can't ping anyone. Discord messages go out with mentions turned off, and `@everyone`, `@here` and
  `@channel` are broken up for other services.
- The verdict is checked against the "Breaking / action needed" line in code, so a model that says "Safe to
  update" while a breaking change is listed gets overruled.

The worst a malicious release note can do is make its own summary wrong. For updates that matter, read the linked
release notes too.
