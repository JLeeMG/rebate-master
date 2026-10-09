"""The one way a load fails: rejected whole, with every problem listed (spec §5)."""


class LoadRejected(Exception):
    def __init__(self, source: str, problems: list[str]) -> None:
        self.source = source
        self.problems = problems
        super().__init__(f"{source}: rejected with {len(problems)} problem(s)")

    def report(self) -> str:
        lines = [f"{self.source} was NOT loaded. Nothing from it was saved. Problems found:"]
        lines += [f"  {i}. {p}" for i, p in enumerate(self.problems, 1)]
        return "\n".join(lines)


class AlreadyLoaded(Exception):
    """The identical file was loaded before. Not an error: nothing to do."""
