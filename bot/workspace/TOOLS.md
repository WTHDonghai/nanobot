# Available Tools

**IMPORTANT: Use the internal document repository as the only evidence source for user-facing answers.**

## Internal Knowledge Tools

### Search Resources
```
openviking_search(query: str, target_uri: str = None) -> str
```
Search for relevant documents and resources in OpenViking. Use this as the first step for information queries.

### Read Content
```
openviking_read(uri: str, level: str = "abstract") -> str
```
Read resource content from OpenViking. Levels: `abstract`, `overview`, `read`.

- `openviking_search` is retrieval only. After finding a relevant resource, call `openviking_read` before answering.
- When `openviking_read(level="read", include_images=true)` returns Markdown image lines, keep those lines unchanged in the final reply.
- Never invent Markdown image syntax from raw `viking://...` image URIs yourself.

### List Resources
```
openviking_list(uri: str, recursive: bool = False) -> str
```
List resources at a specified knowledge-base path.

### Exact Match Search
```
openviking_grep(pattern: str, uri: str = "viking://resources/") -> str
```
Search for exact text matches inside the knowledge base when keyword lookup is more suitable than semantic search.

### Path Pattern Search
```
openviking_glob(pattern: str, uri: str = "viking://resources/") -> str
```
Locate documents by filename or path pattern inside the knowledge base.
