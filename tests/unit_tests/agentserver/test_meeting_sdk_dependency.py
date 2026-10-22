"""Verify the installed SDK provides the Team meeting API."""

import subprocess
import sys


def test_installed_sdk_exposes_meeting_api_without_pythonpath():
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "from openjiuwen.agent_teams.meeting import "
            "MeetingCoordinator, MeetingScope, MeetingState",
        ],
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )

    assert result.returncode == 0, result.stderr
