"""Command-line interface (the outermost adapter in the ports-and-adapters layout).

Only argument parsing and output live here; business logic belongs in ``costguard.core``.
"""

from typing import Annotated

import typer

from costguard import __version__

app = typer.Typer(
    name="costguard",
    help="AWS FinOps and cost-risk toolkit.",
    no_args_is_help=True,
    add_completion=False,
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"costguard {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """AWS FinOps and cost-risk toolkit."""


@app.command()
def info() -> None:
    """Show build and environment information."""
    import platform

    typer.echo(f"costguard {__version__}")
    typer.echo(f"python    {platform.python_version()} ({platform.system()})")
