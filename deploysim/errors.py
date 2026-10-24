"""Problems that stop deploysim before it can act safely; the CLI maps them to exit code 5."""


class EnvironmentProblem(Exception):
    """Base class: nothing was changed."""


class ToolError(EnvironmentProblem):
    """A required tool or file is missing or misbehaved."""


class UnsafeTarget(EnvironmentProblem):
    """The kubeconfig or namespace is not the local simulator."""
