import logging
import queue
import threading
from collections.abc import Callable

log = logging.getLogger(__name__)


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


# How each sender's body names the images to check
SENDERS: dict[str, Callable[[object], list[str]]] = {"generic": generic}
