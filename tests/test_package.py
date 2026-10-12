"""Packaging smoke check; runnable against an installed wheel as well as editable."""

from importlib.metadata import metadata
from importlib.resources import files

from xpushstream import __version__


def test_installed_package_metadata_and_typing_marker() -> None:
    package = metadata("xpushstream")
    assert package["Version"] == __version__
    assert package["License-Expression"] == "MIT"
    assert files("xpushstream").joinpath("py.typed").is_file()
