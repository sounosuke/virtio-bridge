"""
Filesystem watcher abstraction.

Provides event-driven file change detection with multiple backends:
- inotify (Linux, preferred for VM side)
- polling (universal fallback)

macOS fsevents could be added later but polling works fine for the host side
since the server is doing I/O-bound HTTP forwarding anyway.
"""

import os
import time
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger("virtio-bridge.watcher")


class FileWatcher(ABC):
    """Base class for filesystem watchers."""

    def __init__(self, watch_dir: str | Path, pattern: str = "*.json"):
        self.watch_dir = Path(watch_dir)
        self.pattern = pattern
        self._running = False

    @abstractmethod
    def watch(self, callback: Callable[[Path], None]) -> None:
        """
        Watch for new files matching pattern.
        Calls callback(filepath) for each new file detected.
        Blocks until stop() is called.
        """
        ...

    def stop(self) -> None:
        """Stop the watcher."""
        self._running = False

    @classmethod
    def create(cls, watch_dir: str | Path, pattern: str = "*.json") -> "FileWatcher":
        """Factory: create the best available watcher for the current platform."""
        # Try inotify first (Linux)
        try:
            return InotifyWatcher(watch_dir, pattern)
        except ImportError:
            pass

        # Fallback to polling
        logger.info("Using polling watcher (inotify not available)")
        return PollingWatcher(watch_dir, pattern)


class InotifyWatcher(FileWatcher):
    """Linux inotify-based watcher. Low latency, event-driven."""

    def __init__(self, watch_dir: str | Path, pattern: str = "*.json"):
        super().__init__(watch_dir, pattern)
        # Import here to fail fast if not available
        import inotify.adapters  # type: ignore
        self._inotify_mod = inotify.adapters

    def watch(self, callback: Callable[[Path], None]) -> None:
        self._running = True
        i = self._inotify_mod.Inotify()
        i.add_watch(str(self.watch_dir))
        logger.info(f"inotify watching: {self.watch_dir}")

        try:
            for event in i.event_gen(yield_nones=True):
                if not self._running:
                    break

                if event is None:
                    continue

                (_, type_names, path, filename) = event

                # We care about MOVED_TO (atomic rename) and CLOSE_WRITE
                if not any(t in type_names for t in ("IN_MOVED_TO", "IN_CLOSE_WRITE")):
                    continue

                if not filename:
                    continue

                filepath = Path(path) / filename

                # Check pattern match
                if not filepath.match(self.pattern):
                    continue

                # Skip tmp files
                if filepath.suffix == ".tmp":
                    continue

                logger.debug(f"inotify event: {filepath}")
                try:
                    callback(filepath)
                except Exception as e:
                    logger.error(f"Callback error for {filepath}: {e}")
        finally:
            i.remove_watch(str(self.watch_dir))


class PollingWatcher(FileWatcher):
    """Polling-based watcher. Universal fallback."""

    def __init__(self, watch_dir: str | Path, pattern: str = "*.json",
                 interval: float = 0.1):
        super().__init__(watch_dir, pattern)
        self.interval = interval

    def watch(self, callback: Callable[[Path], None]) -> None:
        self._running = True
        seen: set[str] = set()

        # Initialize with existing files
        for f in self.watch_dir.glob(self.pattern):
            seen.add(str(f))

        logger.info(f"Polling watching: {self.watch_dir} (interval={self.interval}s)")

        while self._running:
            try:
                current = set()
                for f in self.watch_dir.glob(self.pattern):
                    fname = str(f)
                    current.add(fname)
                    if fname not in seen:
                        # Skip tmp files
                        if f.suffix == ".tmp":
                            continue
                        logger.debug(f"Poll detected: {f}")
                        try:
                            callback(f)
                        except Exception as e:
                            logger.error(f"Callback error for {f}: {e}")

                seen = current
            except OSError as e:
                logger.warning(f"Polling error: {e}")

            time.sleep(self.interval)
