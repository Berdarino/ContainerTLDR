# Security

## Reporting a problem

Please report security problems privately through GitHub: open the Security tab of this repository and choose
"Report a vulnerability". Don't open a public issue for them.

## What ContainerTLDR can and can't do

- It only reads from the Docker API: listing and inspecting containers and images. Run it behind a socket proxy
  with `POST=0`, as in [examples/docker-compose.yml](examples/docker-compose.yml), so it couldn't do more even if
  it tried.
- It only makes outbound requests: to container registries, the GitHub API, your LLM and your notification
  services. It doesn't listen on any port unless `CTLDR_WEB_PORT` is set.
- Without `CTLDR_WEB_PASSWORD`, the web UI that `CTLDR_WEB_PORT` turns on is read-only and has no login. It shows
  container names, image references and update summaries, but no settings or secrets. Keep it on your LAN, or put
  it behind your reverse proxy's authentication if it's reachable from further away.
- It runs as a non-root user and only writes to `/data`, so a read-only root filesystem works.
- Secrets (notification URLs, `GITHUB_TOKEN`, the LLM API key, the web UI password, registry logins) are never
  logged. The settings printed at startup leave them out, and notification URLs in log messages are cut down to
  their scheme and host.

## The web UI's login

With `CTLDR_WEB_PASSWORD` set, every page needs a login and settings can be changed there.

- The password only comes from the environment. ContainerTLDR doesn't store it, and the settings page can't
  change it, nor the port, the address or `/data`.
- The session is a signed cookie, `HttpOnly` and `SameSite=Strict`. The key it's signed with is kept in
  `/data/session.key` and includes the password, so changing the password logs everyone out.
- Failed logins are slowed down, one second more for each one in a row up to ten, one attempt at a time. They're
  logged with the client's address.
- Every form carries a token tied to the session, and a form posted from another site is refused.
- Secrets saved on the settings page are write-only: no page, log or error shows them. They're kept in
  `/data/settings.json`, which only its owner can read.
- The cookie isn't marked `Secure`, so logins work over plain http on your LAN. From further away, use https
  through your reverse proxy, or the password crosses the network in the clear.

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
- The web UI escapes a summary before it formats its bold text and lists, so HTML in a release note or in the
  model's reply is never rendered.

The worst a malicious release note can do is make its own summary wrong. For updates that matter, read the linked
release notes too.
