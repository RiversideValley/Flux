"""Support for tasks, coroutines and the scheduler."""

__all__ = (
    'Task', 'create_task',
    'FIRST_COMPLETED', 'FIRST_EXCEPTION', 'ALL_COMPLETED',
    'wait', 'wait_for', 'as_completed', 'sleep',
    'gather', 'shield', 'ensure_future', 'run_coroutine_threadsafe',
    'current_task', 'all_tasks',
    '_register_task', '_unregister_task', '_enter_task', '_leave_task',
)

import concurrent.futures
import functools
import inspect
import types
import warnings
import weakref

from . import base_tasks
from . import coroutines
from . import events
from . import futures
from .coroutines import coroutine


def current_task(loop=None):
    """Return a currently executed task."""
    if loop is None:
        loop = events.get_running_loop()
    return _current_tasks.get(loop)


def all_tasks(loop=None):
    """Return a set of all tasks for the loop."""
    if loop is None:
        loop = events.get_event_loop()
    return {t for t in _all_tasks if futures._get_loop(t) is loop}


class Task(futures._PyFuture):  # Inherit Python Task implementation
                                # from a Python Future implementation.

    """A coroutine wrapped in a Future."""

    # An important invariant maintained while a Task not done:
    #
    # - Either _fut_waiter is None, and _step() is scheduled;
    # - or _fut_waiter is some Future, and _step() is *not* scheduled.
    #
    # The only transition from the latter to the former is through
    # _wakeup().  When _fut_waiter is not None, one of its callbacks
    # must be _wakeup().

    # If False, don't log a message if the task is destroyed whereas its
    # status is still pending
    _log_destroy_pending = True

    @classmethod
    def current_task(cls, loop=None):
        """Return the currently running task in an event loop or None.

        By default the current task for the current event loop is returned.

        None is returned when called not in the context of a Task.
        """
        warnings.warn("Task.current_task() is deprecated, "
                      "use asyncio.current_task() instead",
                      PendingDeprecationWarning,
                      stacklevel=2)
        if loop is None:
            loop = events.get_event_loop()
        return current_task(loop)

    @classmethod
    def all_tasks(cls, loop=None):
        """Return a set of all tasks for an event loop.

        By default all tasks for the current event loop are returned.
        """
        warnings.warn("Task.all_tasks() is deprecated, "
                      "use asyncio.all_tasks() instead",
                      PendingDeprecationWarning,
                      stacklevel=2)
        return all_tasks(loop)

    def __init__(self, coro, *, loop=None):
        super().__init__(loop=loop)
        if self._source_traceback:
            del self._source_traceback[-1]
        if not coroutines.iscoroutine(coro):
            # raise after Future.__init__(), attrs are required for __del__
            # prevent logging for pending task in __del__
            self._log_destroy_pending = False
            raise TypeError(f"a coroutine was expected, got {coro!r}")

        self._must_cancel = False
        self._fut_waiter = None
        self._coro = coro

        self._loop.call_soon(self._step)
        _register_task(self)

    def __del__(self):
        if self._state == futures._PENDING and self._log_destroy_pending:
            context = {
                'task': self,
                'message': 'Task was destroyed but it is pending!',
            }
            if self._source_traceback:
                context['source_traceback'] = self._source_traceback
            self._loop.call_exception_handler(context)
        super().__del__()

    def _repr_info(self):
        return base_tasks._task_repr_info(self)

    def set_result(self, result):
        raise RuntimeError('Task does not support set_result operation')

    def set_exception(self, exception):
        raise RuntimeError('Task does not support set_exception operation')

    def get_stack(self, *, limit=None):
        """Return the list of stack frames for this task's coroutine.

        If the coroutine is not done, this returns the stack where it is
        suspended.  If the coroutine has completed successfully or was
        cancelled, this returns an empty list.  If the coroutine was
        terminated by an exception, this returns the list of traceback
        frames.

        The frames are always ordered from oldest to newest.

        The optional limit gives the maximum number of frames to
        return; by default all available frames are returned.  Its
        meaning differs depending on whether a stack or a traceback is
        returned: the newest frames of a stack are returned, but the
        oldest frames of a traceback are returned.  (This matches the
        behavior of the traceback module.)

        For reasons beyond our control, only one stack frame is
        returned for a suspended coroutine.
        """
        return base_tasks._task_get_stack(self, limit)

    def print_stack(self, *, limit=None, file=None):
        """Print the stack or traceback for this task's coroutine.

        This produces output similar to that of the traceback module,
        for the frames retrieved by get_stack().  The limit argument
        is passed to get_stack().  The file argument is an I/O stream
        to which the output is written; by default output is written
        to sys.stderr.
        """
        return base_tasks._task_print_stack(self, limit, file)

    def cancel(self):
        """Request that this task cancel itself.

        This arranges for a CancelledError to be thrown into the
        wrapped coroutine on the next cycle through the event loop.
        The coroutine then has a chance to clean up or even deny
        the request using try/except/finally.

        Unlike Future.cancel, this does not guarantee that the
        task will be cancelled: the exception might be caught and
        acted upon, delaying cancellation of the task or preventing
        cancellation completely.  The task may also return a value or
        raise a different exception.

        Immediately after this method is called, Task.cancelled() will
        not return True (unless the task was already cancelled).  A
        task will be marked as cancelled when the wrapped coroutine
        terminates with a CancelledError exception (even if cancel()
        was not called).
        """
        self._log_traceback = False
        if self.done():
            return False
        if self._fut_waiter is not None:
            if self._fut_waiter.cancel():
                # Leave self._fut_waiter; it may be a Task that
                # catches and ignores the cancellation so we may have
                # to cancel it again later.
                return True
        # It must be the case that self._step is already scheduled.
        self._must_cancel = True
        return True

    def _step(self, exc=None):
        if self.done():
            raise futures.InvalidStateError(
                f'_step(): already done: {self!r}, {exc!r}')
        if self._must_cancel:
            if not isinstance(exc, futures.CancelledError):
                exc = futures.CancelledError()
            self._must_cancel = False
        coro = self._coro
        self._fut_waiter = None

        _enter_task(self._loop, self)
        # Call either coro.throw(exc) or coro.send(None).
        try:
            if exc is None:
                # We use the `send` method directly, because coroutines
                # don't have `__iter__` and `__next__` methods.
                result = coro.send(None)
            else:
                result = coro.throw(exc)
        except StopIteration as exc:
            if self._must_cancel:
                # Task is cancelled right before coro stops.
                self._must_cancel = False
                super().set_exception(futures.CancelledError())
            else:
                super().set_result(exc.value)
        except futures.CancelledError:
            super().cancel()  # I.e., Future.cancel(self).
        except Exception as exc:
            super().set_exception(exc)
        except BaseException as exc:
            super().set_exception(exc)
            raise
        else:
            blocking = getattr(result, '_asyncio_future_blocking', None)
            if blocking is not None:
                # Yielded Future must come from Future.__iter__().
                if futures._get_loop(result) is not self._loop:
                    new_exc = RuntimeError(
                        f'Task {self!r} got Future '
                        f'{result!r} attached to a different loop')
                    self._loop.call_soon(self._step, new_exc)
                elif blocking:
                    if result is self:
                        new_exc = RuntimeError(
                            f'Task cannot await on itself: {self!r}')
                        self._loop.call_soon(self._step, new_exc)
                    else:
                        result._asyncio_future_blocking = False
                        result.add_done_callback(self._wakeup)
                        self._fut_waiter = result
                        if self._must_cancel:
                            if self._fut_waiter.cancel():
                                self._must_cancel = False
                else:
                    new_exc = RuntimeError(
                        f'yield was used instead of yield from '
                        f'in task {self!r} with {result!r}')
                    self._loop.call_soon(self._step, new_exc)

            elif result is None:
                # Bare yield relinquishes control for one event loop iteration.
                self._loop.call_soon(self._step)
            elif inspect.isgenerator(result):
                # Yielding a generator is just wrong.
                new_exc = RuntimeError(
                    f'yield was used instead of yield from for '
                    f'generator in task {self!r} with {result}')
                self._loop.call_soon(self._step, new_exc)
            else:
                # Yielding something else is an error.
                new_exc = RuntimeError(f'Task got bad yield: {result!r}')
                self._loop.call_soon(self._step, new_exc)
        finally:
            _leave_task(self._loop, self)
            self = None  # Needed to break cycles when an exception occurs.

    def _wakeup(self, future):
        try:
            future.result()
        except Exception as exc:
            # This may also be a cancellation.
            self._step(exc)
        else:
            # Don't pass the value of `future.result()` explicitly,
            # as `Future.__iter__` and `Future.__await__` don't need it.
            # If we call `_step(value, None)` instead of `_step()`,
            # Python eval loop would use `.send(value)` method call,
            # instead of `__next__()`, which is slower for futures
            # that return non-generator iterators from their `__iter__`.
            self._step()
        self = None  # Needed to break cycles when an exception occurs.


_PyTask = Task


try:
    import _asyncio
except ImportError:
    pass
else:
    # _CTask is needed for tests.
    Task = _CTask = _asyncio.Task


def create_task(coro):
    """Schedule the execution of a coroutine object in a spawn task.

    Return a Task object.
    """
    loop = events.get_running_loop()
    return loop.create_task(coro)


# wait() and as_completed() similar to those in PEP 3148.

FIRST_COMPLETED = concurrent.futures.FIRST_COMPLETED
FIRST_EXCEPTION = concurrent.futures.FIRST_EXCEPTION
ALL_COMPLETED = concurrent.futures.ALL_COMPLETED


async def wait(fs, *, loop=None, timeout=None, return_when=ALL_COMPLETED):
    """Wait for the Futures and coroutines given by fs to complete.

    The sequence futures must not be empty.

    Coroutines will be wrapped in Tasks.

    Returns two sets of Future: (done, pending).

    Usage:

        done, pending = await asyncio.wait(fs)

    Note: This does not raise TimeoutError! Futures that aren't done
    when the timeout occurs are returned in the second set.
    """
    if futures.isfuture(fs) or coroutines.iscoroutine(fs):
        raise TypeError(f"expect a list of futures, not {type(fs).__name__}")
    if not fs:
        raise ValueError('Set of coroutines/Futures is empty.')
    if return_when not in (FIRST_COMPLETED, FIRST_EXCEPTION, ALL_COMPLETED):
        raise ValueError(f'Invalid return_when value: {return_when}')

    if loop is None:
        loop = events.get_event_loop()

    fs = {ensure_future(f, loop=loop) for f in set(fs)}

    return await _wait(fs, timeout, return_when, loop)


def _release_waiter(waiter, *args):
    if not waiter.done():
        waiter.set_result(None)


async def wait_for(fut, timeout, *, loop=None):
    """Wait for the single Future or coroutine to complete, with timeout.

    Coroutine will be wrapped in Task.

    Returns result of the Future or coroutine.  When a timeout occurs,
    it cancels the task and raises TimeoutError.  To avoid the task
    cancellation, wrap it in shield().

    If the wait is cancelled, the task is also cancelled.

    This function is a coroutine.
    """
    if loop is None:
        loop = events.get_event_loop()

    if timeout is None:
        return await fut

    if timeout <= 0:
        fut = ensure_future(fut, loop=loop)

        if fut.done():
            return fut.result()

        fut.cancel()
        raise futures.TimeoutError()

    waiter = loop.create_future()
    timeout_handle = loop.call_later(timeout, _release_waiter, waiter)
    cb = functools.partial(_release_waiter, waiter)

    fut = ensure_future(fut, loop=loop)
    fut.add_done_callback(cb)

    try:
        # wait until the future completes or the timeout
        try:
            await waiter
        except futures.CancelledError:
            fut.remove_done_callback(cb)
            fut.cancel()
            raise

        if fut.done():
            return fut.result()
        else:
            fut.remove_done_callback(cb)
            fut.cancel()
            raise futures.TimeoutError()
    finally:
        timeout_handle.cancel()


async def _wait(fs, timeout, return_when, loop):
    """Internal helper for wait() and wait_for().

    The fs argument must be a collection of Futures.
    """
    assert fs, 'Set of Futures is empty.'
    waiter = loop.create_future()
    timeout_handle = None
    if timeout is not None:
        timeout_handle = loop.call_later(timeout, _release_waiter, waiter)
    counter = len(fs)

    def _on_completion(f):
        nonlocal counter
        counter -= 1
        if (counter <= 0 or
            return_when == FIRST_COMPLETED or
            return_when == FIRST_EXCEPTION and (not f.cancelled() and
                                                f.exception() is not None)):
            if timeout_handle is not None:
                timeout_handle.cancel()
            if not waiter.done():
                waiter.set_result(None)

    for f in fs:
        f.add_done_callback(_on_completion)

    try:
        await waiter
    finally:
        if timeout_handle is not None:
            timeout_handle.cancel()

    done, pending = set(), set()
    for f in fs:
        f.remove_done_callback(_on_completion)
        if f.done():
            done.add(f)
        else:
            pending.add(f)
    return done, pending


# This is *not* a @coroutine!  It is just an iterator (yielding Futures).
def as_completed(fs, *, loop=None, timeout=None):
    """Return an iterator whose values are coroutines.

    When waiting for the yielded coroutines you'll get the results (or
    exceptions!) of the original Futures (or coroutines), in the order
    in which and as soon as they complete.

    This differs from PEP 3148; the proper way to use this is:

        for f in as_completed(fs):
            result = await f  # The 'await' may raise.
            # Use result.

    If a timeout is specified, the 'await' will raise
    TimeoutError when the timeout occurs before all Futures are done.

    Note: The futures 'f' are not necessarily members of fs.
    """
    if futures.isfuture(fs) or coroutines.iscoroutine(fs):
        raise TypeError(f"expect a list of futures, not {type(fs).__name__}")
    loop = loop if loop is not None else events.get_event_loop()
    todo = {ensure_future(f, loop=loop) for f in set(fs)}
    from .queues import Queue  # Import here to avoid circular import problem.
    done = Queue(loop=loop)
    timeout_handle = None

    def _on_timeout():
        for f in todo:
            f.remove_done_callback(_on_completion)
            done.put_nowait(None)  # Queue a dummy value for _wait_for_one().
        todo.clear()  # Can't do todo.remove(f) in the loop.

    def _on_completion(f):
        if not todo:
            return  # _on_timeout() was here first.
        todo.remove(f)
        done.put_nowait(f)
        if not todo and timeout_handle is not None:
            timeout_handle.cancel()

    async def _wait_for_one():
        f = await done.get()
        if f is None:
            # Dummy value from _on_timeout().
            raise futures.TimeoutError
        return f.result()  # May raise f.exception().

    for f in todo:
        f.add_done_callback(_on_completion)
    if todo and timeout is not None:
        timeout_handle = loop.call_later(timeout, _on_timeout)
    for _ in range(len(todo)):
        yield _wait_for_one()


@types.coroutine
def __sleep0():
    """Skip one event loop run cycle.

    This is a private helper for 'asyncio.sleep()', used
    when the 'delay' is set to 0.  It uses a bare 'yield'
    expression (which Task._step knows how to handle)
    instead of creating a Future object.
    """
    yield


async def sleep(delay, result=None, *, loop=None):
    """Coroutine that completes after a given time (in seconds)."""
    if delay <= 0:
        await __sleep0()
        return result

    if loop is None:
        loop = events.get_event_loop()
    future = loop.create_future()
    h = loop.call_later(delay,
                        futures._set_result_unless_cancelled,
                        future, result)
    try:
        return await future
    finally:
        h.cancel()


def ensure_future(coro_or_future, *, loop=None):
    """Wrap a coroutine or an awaitable in a future.

    If the argument is a Future, it is returned directly.
    """
    if futures.isfuture(coro_or_future):
        if loop is not None and loop is not futures._get_loop(coro_or_future):
            raise ValueError('loop argument must agree with Future')
        return coro_or_future
    elif coroutines.iscoroutine(coro_or_future):
        if loop is None:
            loop = events.get_event_loop()
        task = loop.create_task(coro_or_future)
        if task._source_traceback:
            del task._source_traceback[-1]
        return task
    elif inspect.isawaitable(coro_or_future):
        return ensure_future(_wrap_awaitable(coro_or_future), loop=loop)
    else:
        raise TypeError('An asyncio.Future, a coroutine or an awaitable is '
                        'required')


@coroutine
def _wrap_awaitable(awaitable):
    """Helper for asyncio.ensure_future().

    Wraps awaitable (an object with __await__) into a coroutine
    that will later be wrapped in a Task by ensure_future().
    """
    return (yield from awaitable.__await__())


class _GatheringFuture(futures.Future):
    """Helper for gather().

    This overrides cancel() to cancel all the children and act more
    like Task.cancel(), which doesn't immediately mark itself as
    cancelled.
    """

    def __init__(self, children, *, loop=None):
        super().__init__(loop=loop)
        self._children = children

    def cancel(self):
        if self.done():
            return False
        ret = False
        for child in self._children:
            if child.cancel():
                ret = True
        return ret


def gather(*coros_or_futures, loop=None, return_exceptions=False):
    """Return a future aggregating results from the given coroutines/futures.

    Coroutines will be wrapped in a future and scheduled in the event
    loop. They will not necessarily be scheduled in the same order as
    passed in.

    All futures must share the same event loop.  If all the tasks are
    done successfully, the returned future's result is the list of
    results (in the order of the original sequence, not necessarily
    the order of results arrival).  If *return_exceptions* is True,
    exceptions in the tasks are treated the same as successful
    results, and gathered in the result list; otherwise, the first
    raised exception will be immediately propagated to the returned
    future.

    Cancellation: if the outer Future is cancelled, all children (that
    have not completed yet) are also cancelled.  If any child is
    cancelled, this is treated as if it raised CancelledError --
    the outer Future is *not* cancelled in this case.  (This is to
    prevent the cancellation of one child to cause other children to
    be cancelled.)
    """
    if not coros_or_futures:
        if loop is None:
            loop = events.get_event_loop()
        outer = loop.create_future()
        outer.set_result([])
        return outer

    def _done_callback(fut):
        nonlocal nfinished
        nfinished += 1

        if outer.done():
            if not fut.cancelled():
                # Mark exception retrieved.
                fut.exception()
            return

        if not return_exceptions:
            if fut.cancelled():
                # Check if 'fut' is cancelled first, as
                # 'fut.exception()' will *raise* a CancelledError
                # instead of returning it.
                exc = futures.CancelledError()
                outer.set_exception(exc)
                return
            else:
                exc = fut.exception()
                if exc is not None:
                    outer.set_exception(exc)
                    return

        if nfinished == nfuts:
            # All futures are done; create a list of results
            # and set it to the 'outer' future.
            results = []

            for fut in children:
                if fut.cancelled():
                    # Check if 'fut' is cancelled first, as
                    # 'fut.exception()' will *raise* a CancelledError
                    # instead of returning it.
                    res = futures.CancelledError()
                else:
                    res = fut.exception()
                    if res is None:
                        res = fut.result()
                results.append(res)

            outer.set_result(results)

    arg_to_fut = {}
    children = []
    nfuts = 0
    nfinished = 0
    for arg in coros_or_futures:
        if arg not in arg_to_fut:
            fut = ensure_future(arg, loop=loop)
            if loop is None:
                loop = futures._get_loop(fut)
            if fut is not arg:
                # 'arg' was not a Future, therefore, 'fut' is a new
                # Future created specifically for 'arg'.  Since the caller
                # can't control it, disable the "destroy pending task"
                # warning.
                fut._log_destroy_pending = False

            nfuts += 1
            arg_to_fut[arg] = fut
            fut.add_done_callback(_done_callback)

        else:
            # There's a duplicate Future object in coros_or_futures.
            fut = arg_to_fut[arg]

        children.append(fut)

    outer = _GatheringFuture(children, loop=loop)
    return outer


def shield(arg, *, loop=None):
    """Wait for a future, shielding it from cancellation.

    The statement

        res = await shield(something())

    is exactly equivalent to the statement

        res = await something()

    *except* that if the coroutine containing it is cancelled, the
    task running in something() is not cancelled.  From the POV of
    something(), the cancellation did not happen.  But its caller is
    still cancelled, so the yield-from expression still raises
    CancelledError.  Note: If something() is cancelled by other means
    this will still cancel shield().

    If you want to completely ignore cancellation (not recommended)
    you can combine shield() with a try/except clause, as follows:

        try:
            res = await shield(something())
        except CancelledError:
            res = None
    """
    inner = ensure_future(arg, loop=loop)
    if inner.done():
        # Shortcut.
        return inner
    loop = futures._get_loop(inner)
    outer = loop.create_future()

    def _done_callback(inner):
        if outer.cancelled():
            if not inner.cancelled():
                # Mark inner's result as retrieved.
                inner.exception()
            return

        if inner.cancelled():
            outer.cancel()
        else:
            exc = inner.exception()
            if exc is not None:
                outer.set_exception(exc)
            else:
                outer.set_result(inner.result())

    inner.add_done_callback(_done_callback)
    return outer


def run_coroutine_threadsafe(coro, loop):
    """Submit a coroutine object to a given event loop.

    Return a concurrent.futures.Future to access the result.
    """
    if not coroutines.iscoroutine(coro):
        raise TypeError('A coroutine object is required')
    future = concurrent.futures.Future()

    def callback():
        try:
            futures._chain_future(ensure_future(coro, loop=loop), future)
        except Exception as exc:
            if future.set_running_or_notify_cancel():
                future.set_exception(exc)
            raise

    loop.call_soon_threadsafe(callback)
    return future


# WeakSet containing all alive tasks.
_all_tasks = weakref.WeakSet()

# Dictionary containing tasks that are currently active in
# all running event loops.  {EventLoop: Task}
_current_tasks = {}


def _register_task(task):
    """Register a new task in asyncio as executed by loop."""
    _all_tasks.add(task)


def _enter_task(loop, task):
    current_task = _current_tasks.get(loop)
    if current_task is not None:
        raise RuntimeError(f"Cannot enter into task {task!r} while another "
                           f"task {current_task!r} is being executed.")
    _current_tasks[loop] = task


def _leave_task(loop, task):
    current_task = _current_tasks.get(loop)
    if current_task is not task:
        raise RuntimeError(f"Leaving task {task!r} does not match "
                           f"the current task {current_task!r}.")
    del _current_tasks[loop]


def _unregister_task(task):
    """Unregister a task."""
    _all_tasks.discard(task)


_py_register_task = _register_task
_py_unregister_task = _unregister_task
_py_enter_task = _enter_task
_py_leave_task = _leave_task


try:
    from _asyncio import (_register_task, _unregister_task,
                          _enter_task, _leave_task,
                          _all_tasks, _current_tasks)
except ImportError:
    pass
else:
    _c_register_task = _register_task
    _c_unregister_task = _unregister_task
    _c_enter_task = _enter_task
    _c_leave_task = _leave_task
