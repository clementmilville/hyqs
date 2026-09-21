"""hyqs-jobs: an OAuth-authenticated CLI over the hyqs MCP job-operations tools.

This is intentionally separate from the ``hyqs`` console script
(``hyqs.__main__``), which boots the entire web+pipeline+scheduler service.
Running ``hyqs`` to "just check job status" is a documented footgun; this
package never touches that entrypoint.
"""

from __future__ import annotations
