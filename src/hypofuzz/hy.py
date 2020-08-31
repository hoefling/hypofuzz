"""Adaptive fuzzing for property-based tests using Hypothesis."""

import contextlib
import itertools
import sys
import traceback
from inspect import getfullargspec
from random import Random
from typing import (
    Any,
    Callable,
    Counter,
    Dict,
    FrozenSet,
    Generator,
    List,
    NoReturn,
    Tuple,
    Union,
)

from hypothesis import settings, strategies as st
from hypothesis.core import (
    BuildContext,
    deterministic_PRNG,
    failure_exceptions_to_catch,
    get_trimmed_traceback,
    process_arguments_to_given,
    skip_exceptions_to_reraise,
)
from hypothesis.database import ExampleDatabase
from hypothesis.errors import StopTest, UnsatisfiedAssumption
from hypothesis.internal.conjecture.data import ConjectureData, ConjectureResult, Status
from hypothesis.internal.conjecture.engine import BUFFER_SIZE
from hypothesis.internal.conjecture.junkdrawer import stack_depth_of_caller
from hypothesis.internal.reflection import function_digest
from sortedcontainers import SortedKeyList

from .cov import Arc, CollectionContext


@contextlib.contextmanager
def constant_stack_depth() -> Generator[None, None, None]:
    # TODO: consider extracting this upstream so we can just import it.
    recursion_limit = sys.getrecursionlimit()
    depth = stack_depth_of_caller()
    # Because we add to the recursion limit, to be good citizens we also add
    # a check for unbounded recursion.  The default limit is 1000, so this can
    # only ever trigger if something really strange is happening and it's hard
    # to imagine an intentionally-deeply-recursive use of this code.
    assert depth <= 1000, (
        f"Hypothesis would usually add {recursion_limit} to the stack depth of "
        "{depth} here, but we are already much deeper than expected.  Aborting "
        "now, to avoid extending the stack limit in an infinite loop..."
    )
    try:
        sys.setrecursionlimit(depth + recursion_limit)
        yield
    finally:
        sys.setrecursionlimit(recursion_limit)


def sort_key(buffer: Union[bytes, ConjectureResult]) -> Tuple[int, bytes]:
    """Sort our buffers in shortlex order.

    See `hypothesis.internal.conjecture.shrinker.sort_key` for details on why we
    use shortlex order in particular.  This tweaked version is identical except
    for handling ConjectureResult objects too.
    """
    if isinstance(buffer, ConjectureResult):
        buffer = buffer.buffer
    return (len(buffer), buffer)


def fuzz_in_generator(
    test: Callable[..., None],
    strategy: st.SearchStrategy,
    collector: CollectionContext = None,
    random: Random = None,
) -> Generator[ConjectureResult, bytes, NoReturn]:
    """Wrap the user's test function into a minimal Conjecture fuzz target.

    This is our main integration point with Hypothesis internals - and it's designed
    so that we can get a ConjectureResult out, push a bytestring in, and be done.
    This is very useful in that it provides a great baseline for evaluations.

    It's a combination of the logic in StateForAGivenExecution in
    hypothesis.core, and hypothesis.internal.conjecture.engine.ConjectureRunner
    with as much as possible taken out - for this fuzzing mode we prioritize
    performance over health-checks (just run Hypothesis for the latter!).
    """
    random = random or Random(0)
    collector = collector or contextlib.nullcontext()  # type: ignore
    buf = b"\0" * BUFFER_SIZE
    while True:
        data = ConjectureData(max_length=BUFFER_SIZE, prefix=buf, random=random)
        try:
            with deterministic_PRNG(), BuildContext(data), constant_stack_depth():
                # Note that the data generation and test execution happen in the same
                # coverage context.  We may later split this, or tag each separately.
                with collector:
                    args, kwargs = data.draw(strategy)
                    test(*args, **kwargs)
        except StopTest:
            data.status = Status.OVERRUN
        except (UnsatisfiedAssumption,) + skip_exceptions_to_reraise():
            data.status = Status.INVALID
        except failure_exceptions_to_catch() as e:
            data.status = Status.INTERESTING
            tb = get_trimmed_traceback()
            filename, lineno, *_ = traceback.extract_tb(tb)[-1]
            data.interesting_origin = (type(e), filename, lineno)
            data.note(e)
        data.extra_information.arcs = frozenset(getattr(collector, "arcs", ()))
        data.freeze()
        buf = (yield data.as_result()) or b""
    raise NotImplementedError("Loop not expected to exit")


def engine(*targets: "FuzzProcess") -> NoReturn:
    """Run a quick-and-dirty engine on top of FuzzProcess instances."""
    for p in targets:
        p.startup()
    r = Random(0)
    while True:
        weights = [p.estimated_value_of_next_run for p in targets]
        r.choices(targets, weights=weights)[0].run_one()


class FuzzProcess:
    """Maintain all the state associated with fuzzing a single target.

    This includes:

        - the coverage map and associated inputs
        - references to Hypothesis' database for this test (for failure replay)
        - a "run one" method, and an estimate of the value of running one input
        - checkpointing tools so we can crash and restart without losing progess

    etc.  The fuzz controller will then operate on a collection of these objects.
    """

    @classmethod
    def from_hypothesis_test(
        cls,
        wrapped_test: Any,
        *,
        nodeid: str = None,
        extra_kw: Dict[str, object] = None,
    ) -> "FuzzProcess":
        """Return a FuzzProcess for an @given-decorated test function."""
        _, _, _, search_strategy = process_arguments_to_given(
            wrapped_test,
            arguments=(),
            kwargs=extra_kw,
            given_kwargs=wrapped_test.hypothesis._given_kwargs,
            argspec=getfullargspec(wrapped_test),
        )
        return cls(
            test_fn=wrapped_test.hypothesis.inner_test,
            strategy=search_strategy,
            nodeid=nodeid,
            database_key=function_digest(wrapped_test),
            hypothesis_database=wrapped_test._hypothesis_internal_use_settings.database,
        )

    def __init__(
        self,
        test_fn: Callable,
        strategy: st.SearchStrategy,
        *,
        random_seed: int = 0,
        nodeid: str = None,
        database_key: bytes,
        hypothesis_database: ExampleDatabase,
    ):
        """Construct a FuzzProcess from specific arguments."""
        # The actual fuzzer implementation
        self.random = Random(random_seed)
        self.fuzz_generator = fuzz_in_generator(
            test_fn,
            strategy=strategy,
            collector=CollectionContext(),
            random=Random(random_seed),
        )
        self.nodeid = nodeid or test_fn.__qualname__

        # Database pointers and keys, so that we can resume fuzzing runs without
        # losing all our progress, and to insert failing examples into the
        # *hypothesis* database so they'll be replayed when running e.g. pytest.
        #
        # Only smallest-known failing examples go into the database under the
        # main key to be replayed by running the tests; everything else goes under
        # a specialised fuzz key - this is at minimum enough to replay coverage
        # for every discovered arc or other source of interesting behaviour.
        self._hy_database_key: bytes = database_key or function_digest(test_fn)
        self._fuzz_database_key = b"fuzz_" + self._hy_database_key

        # Each time we find a failing example (result.status is Status.INTERESTING)
        # we insert it into the Hypothesis database *if* it's smaller than our
        # previously minimal example of that interesting_origin, to avoid overfilling
        # the database with mostly-redundant examples.
        #
        # If the test has database=None but there is a non-None default database,
        # use that - otherwise we have no way to report or replay any failures we
        # discover at all!
        self._database = hypothesis_database or settings.default.database
        self._interesting_examples: Dict[Any, bytes] = {}

        # Set up the basic data that we'll track while fuzzing
        self.seen_arcs: Counter[Arc] = Counter()
        self.minimal_example_arcs: FrozenSet[Arc] = frozenset()
        self.ninputs = self.last_new_cov_at = 0

        # Maintain our pool of known examples as a set of ConjectureResult objects,
        # which include the corresponding buffer and track the covered arcs as
        # `item.extra_information.arcs` (added in fuzz_in_generator above).
        #
        # We will probably want a fancier data structure later for performance,
        # but at this stage I'm going to stick with the simple-if-slow approach
        # of calculating everything I need directly from the set data.
        # We track a SortedList instead of a set of stability of random sampling.
        self.pool: SortedKeyList[ConjectureResult] = SortedKeyList(key=sort_key)
        self._replay_buffer: List[bytes] = []

    def startup(self) -> None:
        """Set up initial state and replay the saved behaviour."""
        assert not self.pool, "already started this FuzzProcess"
        # The first thing we need to do is get the covered arcs for the minimal
        # example.  Missing any of these later is taken to be new behaviour.
        self._report_change(self._json_description)
        next(self.fuzz_generator)
        self.ninputs += 1
        firstresult = self.fuzz_generator.send(b"\x00" * BUFFER_SIZE)
        self.minimal_example_arcs = firstresult.extra_information.arcs
        self.pool.add(firstresult)
        self._update(firstresult)

        # Next, restore progress made in previous runs by replaying our saved examples.
        # This is meant to be the minimal set of inputs that exhibits all distinct
        # behaviours we've observed to date.  Replaying takes longer than restoring
        # our data structures directly, but copes much better with changed behaviour.
        if self._database is not None:
            self._replay_buffer = sorted(
                {
                    *self._database.fetch(self._hy_database_key),
                    *self._database.fetch(self._fuzz_database_key),
                },
                key=sort_key,
                reverse=True,
            )

    def generate_prefix(self) -> bytes:
        """Generate a test prefix by mutating previous examples.

        This is going to be the method to override when experimenting with
        alternative fuzzing techniques.

            - for unguided fuzzing, return an empty b'' and the random postfix
              generation in ConjectureData will do the rest.
            - for coverage-guided fuzzing, mutate or splice together known inputs.

        This version is terrible, but any coverage guidance at all is enough to help...
        """
        # Start by replaying any previous failures which we've retrieved from the
        # database.  This is useful to recover state at startup, or to share
        # progress made in other processes.
        if self._replay_buffer:
            return self._replay_buffer.pop()

        # This is a dead-simple implemenation, with no validation of the approach
        # beyond "plausibly works".  The first thousand examples we generate are
        # truly random, and 1% of them after that.
        if self.ninputs < 100 or self.random.random() <= 0.01:
            return b""

        # TODO: weight choice of buffers from the pool inversely to the hit-count
        #       of the arcs they cover.  (least-covered arc they cover?)

        # This ensures that we exploit newly-discovered behaviour ASAP, and that
        # after reloading in a fresh session we converge back to the same roughly
        # even distribution of effort by inverting the stationary distribution.

        # Choose two previously-seen buffers to form a prefix and postfix,
        # plus some random bytes in the middle to pad it out a bit.
        # TODO: exploit the .examples tracking for structured mutation.
        prefix, postfix = self.random.choices(self.pool, weights=None, k=2)
        buffer = (
            prefix.buffer[: self.random.randint(0, len(prefix.buffer))]
            + self._gen_bytes(self.random.randint(0, 9))
            + postfix.buffer[: self.random.randint(0, len(postfix.buffer))]
        )
        assert isinstance(buffer, bytes)
        return buffer

    def _gen_bytes(self, n: int) -> bytes:
        return bytes(self.random.randint(0, 255) for _ in range(n))

    def run_one(self) -> None:
        """Run a single input through the fuzz target."""
        assert self.pool, "not started yet"
        self.ninputs += 1

        # Run the input
        next(self.fuzz_generator)
        prefix = self.generate_prefix()
        result = self.fuzz_generator.send(prefix)
        assert result  # a ConjectureResult
        if result.status > Status.OVERRUN:
            self._update(result)

    def _report_change(self, data: dict) -> object:
        """Replace this method to send data to the dashboard."""

    def _update(self, result: ConjectureResult) -> None:
        assert isinstance(result, ConjectureResult)
        if result.status == Status.OVERRUN:
            return

        # Save and use the coverage information we just collected.
        arcs = self.minimal_example_arcs.symmetric_difference(
            result.extra_information.arcs
        )
        if arcs.difference(self.seen_arcs):
            self._database.save(self._fuzz_database_key, result.buffer)
            self.last_new_cov_at = self.ninputs
            self.pool.add(result)

        # If the last example was "interesting" - i.e. raised an exception which
        # indicates test failure, make sure we know about it and insert the failing
        # example into the Hypothesis database to be replayed in standard tests.
        if result.status == Status.INTERESTING:
            x = self._interesting_examples.setdefault(
                result.interesting_origin, result.buffer
            )
            if self._database and sort_key(result) <= sort_key(x):
                # To avoid over-filling the hypothesis database, we only add a failure
                # if it is a smaller example than the best such failure to date.
                self._database.save(self._hy_database_key, result.buffer)

        # Update the live chart immediately on new progress, and regularly even
        # if we haven't discovered anything (to extend the line horizontally)
        if self.ninputs % 100 == 0 or not arcs.issubset(self.seen_arcs):
            self._report_change(self._json_description)

        # Note: seen_arcs is a Counter, not a set
        self.seen_arcs.update(arcs)

    @property
    def _json_description(self) -> Dict[str, Union[str, int, float]]:
        """Summarise current state to send to dashboard."""
        note = ""
        if self.has_found_failure:
            note = "found failing example"
        elif self._replay_buffer:
            note = "replaying saved examples"
        elif self.ninputs == 0:
            note = "starting up..."
        return {
            "nodeid": self.nodeid,
            "ninputs": self.ninputs,
            "arcs": len(self.seen_arcs),
            "estimated value": self.estimated_value_of_next_run,
            "since new cov": self.ninputs - self.last_new_cov_at,
            "note": note,
        }

    @property
    def estimated_value_of_next_run(self) -> float:
        """Estimate the value of scheduling this fuzz target for another run."""
        # TODO: improve this method.  It should draw on (at least!) MBoehme's
        #       papers, and runtime information so that tests get roughly even
        #       runtime rather than number of runs.
        return 1 / (1 + self.ninputs - self.last_new_cov_at)

    @property
    def has_found_failure(self) -> bool:
        """If we've already found a failing example we might reprioritize."""
        return bool(self._interesting_examples)


def fuzz_several(
    *targets_: FuzzProcess, numprocesses: int = 1, random_seed: int = None
) -> NoReturn:
    """Take N fuzz targets and run them all."""
    # TODO: this isn't actually multi-process yet, and that's bad.
    rand = Random(random_seed)
    targets = SortedKeyList(targets_, lambda t: -t.estimated_value_of_next_run)

    # Loop forever: at each timestep, we choose a target using an epsilon-greedy
    # strategy for simplicity (TODO: improve this later) and run it once.
    # TODO: make this aware of test runtime, so it adapts for arcs-per-second
    #       rather than arcs-per-input.
    for t in targets:
        t.startup()
    for i in itertools.count():
        if i % 20 == 0:
            t = targets.pop(rand.randrange(len(targets)))
            t.run_one()
            targets.add(t)
        else:
            targets[0].run_one()
            if len(targets) > 1 and targets.key(targets[0]) > targets.key(targets[1]):
                # pay our log-n cost to keep the list sorted
                targets.add(targets.pop(0))
            elif targets[0]._interesting_examples:
                print(f"found failing example for {targets[0].nodeid}")  # noqa
                targets.pop(0)
            if not targets:
                raise Exception("Found failures for all tests!")
    raise NotImplementedError("unreachable")
