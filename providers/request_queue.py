"""Request queue with priority lanes and worker-aware concurrency control.

This module provides a FIFO queue that:
- Respects NVIDIA NIM worker limit (32 concurrent requests per worker)
- Supports priority lanes (HIGH for Discord, NORMAL for API, LOW for background)
- Provides observability metrics (queue depth, wait times, rejections)
- Handles timeouts and graceful degradation
- Supports both regular requests and streaming requests
"""

import asyncio
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

from loguru import logger


class RequestPriority(IntEnum):
    """Priority levels for request queue.

    Higher values = higher priority (processed first).
    """
    LOW = 0       # Background tasks
    NORMAL = 1    # API proxy requests
    HIGH = 2      # Discord bot (interactive)


@dataclass
class QueueStats:
    """Queue statistics for observability."""
    current_depth: int = 0
    max_depth: int = 0
    total_enqueued: int = 0
    total_processed: int = 0
    total_rejected: int = 0
    total_timeouts: int = 0
    current_workers_busy: int = 0
    max_workers_busy: int = 0
    avg_wait_time_ms: float = 0.0
    avg_process_time_ms: float = 0.0
    _wait_times: deque = field(default_factory=lambda: deque(maxlen=1000))
    _process_times: deque = field(default_factory=lambda: deque(maxlen=1000))

    def record_wait(self, wait_time_ms: float) -> None:
        self._wait_times.append(wait_time_ms)
        if self._wait_times:
            self.avg_wait_time_ms = sum(self._wait_times) / len(self._wait_times)

    def record_process(self, process_time_ms: float) -> None:
        self._process_times.append(process_time_ms)
        if self._process_times:
            self.avg_process_time_ms = sum(self._process_times) / len(self._process_times)

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_depth": self.current_depth,
            "max_depth": self.max_depth,
            "total_enqueued": self.total_enqueued,
            "total_processed": self.total_processed,
            "total_rejected": self.total_rejected,
            "total_timeouts": self.total_timeouts,
            "current_workers_busy": self.current_workers_busy,
            "max_workers_busy": self.max_workers_busy,
            "avg_wait_time_ms": round(self.avg_wait_time_ms, 2),
            "avg_process_time_ms": round(self.avg_process_time_ms, 2),
        }


@dataclass
class QueuedRequest:
    """A request waiting in the queue."""
    priority: int
    timestamp: float
    coro_factory: Callable[[], Awaitable[Any]]
    future: asyncio.Future
    processing_started: asyncio.Event = field(default_factory=asyncio.Event)
    priority_name: str = "NORMAL"

    def __lt__(self, other: "QueuedRequest") -> bool:
        # Higher priority first, then earlier timestamp (FIFO within priority)
        if self.priority != other.priority:
            return self.priority > other.priority
        return self.timestamp < other.timestamp


class RequestQueue:
    """FIFO queue with priority lanes and worker-aware concurrency control.

    Architecture:
    - PriorityQueue holds pending requests (priority, timestamp, coro_factory, future)
    - Worker pool (semaphore) limits concurrent in-flight requests to max_concurrent
    - Background worker tasks pull from queue and execute requests
    - Metrics track queue depth, wait times, processing times, rejections
    """

    def __init__(
        self,
        max_concurrent: int = 32,          # NVIDIA worker limit
        max_queue_size: int = 600,         # Max queued requests (support multiple sessions)
        num_workers: int = 4,              # Number of background worker tasks
        queue_timeout: float = 300.0,      # Max wait in queue before timeout (seconds)
        enabled: bool = True,              # Can be disabled via config
    ):
        if max_concurrent <= 0:
            raise ValueError("max_concurrent must be > 0")
        if max_queue_size < 0:
            raise ValueError("max_queue_size must be >= 0")
        if num_workers <= 0:
            raise ValueError("num_workers must be > 0")
        if queue_timeout <= 0:
            raise ValueError("queue_timeout must be > 0")

        self._max_concurrent = max_concurrent
        self._max_queue_size = max_queue_size
        self._num_workers = num_workers
        self._queue_timeout = queue_timeout
        self._enabled = enabled

        # Priority queue for pending requests
        self._queue: asyncio.PriorityQueue[QueuedRequest] = asyncio.PriorityQueue(maxsize=max_queue_size)

        # Worker semaphore - limits concurrent in-flight requests
        self._worker_semaphore = asyncio.Semaphore(max_concurrent)

        # Worker tasks
        self._workers: list[asyncio.Task] = []
        self._shutdown_event = asyncio.Event()

        # Statistics
        self._stats = QueueStats()
        self._stats_lock = asyncio.Lock()

        # Priority name mapping for logging
        self._priority_names = {
            RequestPriority.LOW: "LOW",
            RequestPriority.NORMAL: "NORMAL",
            RequestPriority.HIGH: "HIGH",
        }

        logger.info(
            f"RequestQueue initialized: max_concurrent={max_concurrent}, "
            f"max_queue_size={max_queue_size}, num_workers={num_workers}, "
            f"queue_timeout={queue_timeout}s, enabled={enabled}"
        )

    async def start(self) -> None:
        """Start background worker tasks."""
        if not self._enabled:
            logger.info("RequestQueue disabled, workers not started")
            return

        for i in range(self._num_workers):
            worker = asyncio.create_task(self._worker_loop(i), name=f"queue-worker-{i}")
            self._workers.append(worker)

        logger.info(f"RequestQueue started with {self._num_workers} workers")

    async def shutdown(self, drain: bool = True, timeout: float = 30.0) -> None:
        """Shutdown the queue.

        Args:
            drain: If True, wait for queue to empty before stopping workers.
            timeout: Maximum time to wait for drain/shutdown.
        """
        logger.info(f"RequestQueue shutdown requested (drain={drain})")
        self._shutdown_event.set()

        if drain:
            # Wait for queue to empty
            try:
                await asyncio.wait_for(self._wait_until_empty(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(f"Queue drain timed out after {timeout}s, forcing shutdown")

        # Cancel workers
        for worker in self._workers:
            worker.cancel()

        # Wait for workers to finish
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)

        self._workers.clear()
        logger.info("RequestQueue shutdown complete")

    async def _wait_until_empty(self) -> None:
        """Wait until queue is empty and all workers are idle."""
        while True:
            async with self._stats_lock:
                if self._stats.current_depth == 0 and self._stats.current_workers_busy == 0:
                    return
            await asyncio.sleep(0.1)

    async def enqueue(
        self,
        coro_factory: Callable[[], Awaitable[Any]],
        priority: int = RequestPriority.NORMAL,
    ) -> Any:
        """Add a request to the queue and wait for its result.

        Args:
            coro_factory: Callable that returns an awaitable (the actual request).
            priority: Request priority (HIGH, NORMAL, LOW).

        Returns:
            The result of the awaitable.

        Raises:
            asyncio.TimeoutError: If request waits in queue longer than queue_timeout.
            RuntimeError: If queue is disabled or shut down.
            asyncio.QueueFull: If queue is at max capacity (non-blocking put).
        """
        if not self._enabled:
            # Bypass queue entirely - execute directly
            return await coro_factory()

        if self._shutdown_event.is_set():
            raise RuntimeError("RequestQueue is shut down")

        # Clamp priority to valid range
        priority = max(RequestPriority.LOW, min(RequestPriority.HIGH, priority))
        priority_name = self._priority_names.get(RequestPriority(priority), "NORMAL")

        # Create future for result
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        request = QueuedRequest(
            priority=priority,
            timestamp=time.monotonic(),
            coro_factory=coro_factory,
            future=future,
            priority_name=priority_name,
        )

        # Try to enqueue (non-blocking - we handle full queue ourselves)
        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            async with self._stats_lock:
                self._stats.total_rejected += 1
            logger.warning(
                f"RequestQueue full (depth={self._queue.qsize()}/{self._max_queue_size}), "
                f"rejecting {priority_name} priority request"
            )
            raise RuntimeError(
                f"Request queue full ({self._max_queue_size} max). "
                "Try again later or increase REQUEST_QUEUE_MAX_SIZE."
            )
            raise

        # Update stats
        async with self._stats_lock:
            self._stats.current_depth = self._queue.qsize()
            self._stats.total_enqueued += 1
            if self._stats.current_depth > self._stats.max_depth:
                self._stats.max_depth = self._stats.current_depth

        logger.debug(
            f"Enqueued {priority_name} request "
            f"(queue depth: {self._queue.qsize()}/{self._max_queue_size})"
        )

        # Two-phase timeout:
        #   Phase 1 – wait for the request to be picked up by a worker
        #              (capped by queue_timeout). If a worker picks it up
        #              before the timeout, the processing_started event fires.
        #   Phase 2 – once processing has started, wait indefinitely for it
        #              to finish (no timeout, since streaming + retries can
        #              take much longer than queue_timeout).
        #
        # NOTE: We do NOT wrap the future in asyncio.ensure_future() during
        # Phase 1 — doing so would create a Task that, when cancelled below,
        # would prevent the future from ever receiving set_result() from the
        # worker. We only await processing_started with a timeout, then
        # handle both the fast-path (future already done) and normal path.
        try:
            # Phase 1: Queue-wait phase (with timeout)
            # Only watch processing_started — the future is never wrapped in
            # a task, so it can still receive set_result() from the worker.
            await asyncio.wait_for(
                request.processing_started.wait(),
                timeout=self._queue_timeout,
            )

            # Fast-path: if the future completed before processing_started
            # fired (extremely fast non-streaming request), return directly.
            if future.done():
                return future.result()

            # Phase 2: Processing phase (no timeout – let it take as long as needed)
            logger.debug(
                f"Request dequeued after {request.priority_name} priority wait, "
                f"waiting for processing to complete..."
            )
            return await future

        except asyncio.TimeoutError:
            # Check if the future somehow became done during the timeout wait
            if future.done():
                return future.result()

            async with self._stats_lock:
                self._stats.total_timeouts += 1
            logger.warning(
                f"Request timed out after {self._queue_timeout}s in queue "
                f"(depth: {self._queue.qsize()})"
            )
            raise

    async def enqueue_stream(
        self,
        stream_factory: Callable[[], AsyncIterator[Any]],
        priority: int = RequestPriority.NORMAL,
    ) -> AsyncIterator[Any]:
        """Add a streaming request to the queue and yield events as they arrive.

        Unlike the buffered approach (collect all events, then return), this
        yields each event to the client immediately as it's produced. This
        keeps the SSE connection alive during long generations — the client
        sees message_start and content deltas in real time instead of waiting
        minutes for the first byte.

        Args:
            stream_factory: Callable that returns an async iterator (streaming events).
            priority: Request priority (HIGH, NORMAL, LOW).

        Yields:
            Each event from the stream as it's produced.
        """
        if not self._enabled:
            # Bypass queue entirely - execute directly
            async for event in stream_factory():
                yield event
            return

        if self._shutdown_event.is_set():
            raise RuntimeError("RequestQueue is shut down")

        # Clamp priority to valid range
        priority = max(RequestPriority.LOW, min(RequestPriority.HIGH, priority))
        priority_name = self._priority_names.get(RequestPriority(priority), "NORMAL")

        # Queue for passing events from producer (worker) to consumer (this generator)
        queue: asyncio.Queue = asyncio.Queue()
        _SENTINEL = object()

        async def produce():
            """Producer: runs in the worker, puts events into the queue."""
            try:
                async for event in stream_factory():
                    await queue.put(event)
            except BaseException as e:
                # Forward exceptions to the consumer
                await queue.put(e)
            finally:
                await queue.put(_SENTINEL)

        # Submit the producer to the queue
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()

        request = QueuedRequest(
            priority=priority,
            timestamp=time.monotonic(),
            coro_factory=produce,
            future=future,
            priority_name=priority_name,
        )

        # Try to enqueue (non-blocking - we handle full queue ourselves)
        try:
            self._queue.put_nowait(request)
        except asyncio.QueueFull:
            async with self._stats_lock:
                self._stats.total_rejected += 1
            logger.warning(
                f"RequestQueue full (depth={self._queue.qsize()}/{self._max_queue_size}), "
                f"rejecting {priority_name} priority request"
            )
            raise RuntimeError(
                f"Request queue full ({self._max_queue_size} max). "
                "Try again later or increase REQUEST_QUEUE_MAX_SIZE."
            )
            raise

        # Update stats
        async with self._stats_lock:
            self._stats.current_depth = self._queue.qsize()
            self._stats.total_enqueued += 1
            if self._stats.current_depth > self._stats.max_depth:
                self._stats.max_depth = self._stats.max_depth

        logger.debug(
            f"Enqueued {priority_name} request "
            f"(queue depth: {self._queue.qsize()}/{self._max_queue_size})"
        )

        # Consumer: yield events from the queue as they arrive
        try:
            while True:
                event = await queue.get()
                if event is _SENTINEL:
                    break
                if isinstance(event, BaseException):
                    raise event
                yield event
        finally:
            # Ensure the producer future is cleaned up
            if not future.done():
                future.cancel()

    def get_stats(self) -> dict[str, Any]:
        """Get current queue statistics."""
        return self._stats.to_dict()

    @property
    def is_enabled(self) -> bool:
        return self._enabled

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    # ------------------------------------------------------------------
    # Worker loop
    # ------------------------------------------------------------------

    async def _worker_loop(self, worker_id: int) -> None:
        """Background worker that processes requests from the queue."""
        logger.debug(f"Queue worker {worker_id} started")

        while not self._shutdown_event.is_set():
            try:
                # Get next request with timeout to allow shutdown check
                try:
                    request = await asyncio.wait_for(
                        self._queue.get(),
                        timeout=1.0
                    )
                except asyncio.TimeoutError:
                    continue  # Check shutdown_event and loop

                # Acquire worker slot (limits concurrent in-flight requests)
                async with self._worker_semaphore:
                    await self._process_request(request, worker_id)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Queue worker {worker_id} error: {e}")

        logger.debug(f"Queue worker {worker_id} stopped")

    async def _process_request(self, request: QueuedRequest, worker_id: int) -> None:
        """Process a single queued request."""
        # Signal that processing has started so enqueue's two-phase timeout
        # can stop the queue-wait timer and proceed to indefinite processing wait.
        request.processing_started.set()
        wait_time_ms = (time.monotonic() - request.timestamp) * 1000
        start_time = time.monotonic()

        # Update stats - waiting done, now processing
        async with self._stats_lock:
            self._stats.current_depth = self._queue.qsize()
            self._stats.current_workers_busy += 1
            if self._stats.current_workers_busy > self._stats.max_workers_busy:
                self._stats.max_workers_busy = self._stats.current_workers_busy
            self._stats.record_wait(wait_time_ms)

        logger.debug(
            f"Worker {worker_id} processing {request.priority_name} request "
            f"(waited {wait_time_ms:.1f}ms, queue depth: {self._queue.qsize()})"
        )

        try:
            # Execute the actual request
            result = await request.coro_factory()

            # Success - set result
            if not request.future.done():
                request.future.set_result(result)

        except Exception as e:
            # Failure - set exception
            if not request.future.done():
                request.future.set_exception(e)

        finally:
            # Update stats
            process_time_ms = (time.monotonic() - start_time) * 1000
            async with self._stats_lock:
                self._stats.current_workers_busy -= 1
                self._stats.total_processed += 1
                self._stats.record_process(process_time_ms)

            logger.debug(
                f"Worker {worker_id} completed {request.priority_name} request "
                f"in {process_time_ms:.1f}ms"
            )

    # ------------------------------------------------------------------
    # Context manager for direct worker slot access (bypass queue)
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def worker_slot(self):
        """Acquire a worker slot directly (bypasses queue).

        Use for operations that need concurrency control but shouldn't be queued.
        """
        async with self._worker_semaphore:
            yield