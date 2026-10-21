import logging
import queue
import re
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)

_DOCKER_HUB = re.compile(r"(.+\.)?docker\.io")  # registry-1.docker.io, as What's Up Docker names it
_DOCKHAND_LINE = re.compile(r"^- \S+ \((\S+)\): ", re.MULTILINE)  # "- radarr (lscr.io/linuxserver/radarr:latest): "


class Hooks:
    """Checks that webhooks ask for, run one after another in a thread, after the request has been answered.

    Senders give up waiting after a few seconds, and a check with a summary can take a minute and more.
    """

    def __init__(self, check: Callable[[str], None]):
        self.check = check
        self.waiting: set[str] = set()
        self._queue: queue.Queue[str] = queue.Queue()
        self._lock = threading.Lock()
        threading.Thread(target=self._work, name="hooks", daemon=True).start()

    def add(self, image: str) -> bool:
        """False if the image is waiting already, as when a second hook comes in for the same update."""
        with self._lock:
            if image in self.waiting:
                return False
            self.waiting.add(image)
        self._queue.put(image)
        return True

    def _work(self) -> None:
        while True:
            image = self._queue.get()
            with self._lock:
                self.waiting.discard(image)
            try:
                self.check(image)
            except Exception:  # the thread has to stay up for the next hook
                log.exception("webhook check of %s failed", image)


def generic(body: object) -> list[str]:
    """{"image": "lscr.io/linuxserver/radarr:latest"}"""
    image = body.get("image") if isinstance(body, dict) else None
    if not isinstance(image, str) or not image.strip():
        raise ValueError('expected a JSON object like {"image": "lscr.io/linuxserver/radarr:latest"}')
    return [image.strip()]


def wud(body: object) -> list[str]:
    """What's Up Docker's HTTP trigger: a container, or a list of them in batch mode."""
    images = []
    for container in body if isinstance(body, list) else [body]:
        try:
            image = container["image"]
            # Put together the way WUD does it, e.g. "https://ghcr.io/v2" and "linuxserver/radarr"
            host = re.sub(r"^https?://|/v2/?$", "", image["registry"]["url"])
            name = image["name"] if _DOCKER_HUB.fullmatch(host) else f"{host}/{image['name']}"
            images.append(f"{name}:{image['tag']['value']}")
        except (KeyError, TypeError) as e:
            raise ValueError("expected What's Up Docker's container JSON") from e
    return images


def dockhand(body: object) -> list[str]:
    """Dockhand's json:// notification. Only its message names the images, a line per container for updates;
    other events (a test, a container that stopped) have no such lines, so they check nothing."""
    message = body.get("message") if isinstance(body, dict) else None
    if not isinstance(message, str):
        raise ValueError("expected Dockhand's JSON, with a message")
    return list(dict.fromkeys(_DOCKHAND_LINE.findall(message)))  # once per image, however many containers run it


# How each sender's body names the images to check
SENDERS: dict[str, Callable[[object], list[str]]] = {
    "generic": generic,
    "diun": generic,  # Diun's notifications name the image the same way, next to fields of its own
    "wud": wud,
    "dockhand": dockhand,
}
