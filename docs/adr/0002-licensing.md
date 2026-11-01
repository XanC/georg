# 0002. AGPL-3.0-only, with an explicit note on AI-generated code

**Status:** Accepted

## Context

The project owner wants Georg to stay free software, including when modified versions are offered as network services. Much of the code is AI-generated and may not be copyrightable under current U.S. law.

## Decision

- License: unmodified AGPL-3.0 text in `LICENSE`; SPDX identifier `AGPL-3.0-only` at the top of each source file; no generated copyright lines.
- The README explains that the license applies to copyrightable material to the extent rights exist, without modifying the license.
- Contributions are accepted under AGPL-3.0-only to the extent contributors hold rights; no copyright assignment.
- CI checks SPDX headers and that runtime dependencies use AGPL-compatible licenses.
- The web UI links to the corresponding source (AGPL section 13).

## Consequences

Commercial use remains permitted. Files are not classified as copyrightable or not. Git and PR history serve as provenance records.
