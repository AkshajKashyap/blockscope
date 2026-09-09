import typer

app = typer.Typer(no_args_is_help=True)


@app.command()
def analyze(block: int) -> None:
    """Analyze an Ethereum block."""
    typer.echo(f"Analyzing Ethereum block {block}")


if __name__ == "__main__":
    app()
