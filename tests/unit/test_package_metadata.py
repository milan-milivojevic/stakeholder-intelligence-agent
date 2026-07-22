from importlib.metadata import version

from stakeholder_intelligence_agent import __version__


def test_distribution_and_package_versions_match() -> None:
    assert version("stakeholder-intelligence-agent") == __version__
