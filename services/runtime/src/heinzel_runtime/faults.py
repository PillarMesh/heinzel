from collections.abc import Callable

type FaultHook = Callable[[str], None]


def noop_fault_hook(checkpoint: str) -> None:
    del checkpoint
