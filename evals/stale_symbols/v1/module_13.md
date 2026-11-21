# Module 13

## Overview

`legacyAuditHook` is part of the runtime pipeline. Call it before the request is dispatched and keep the returned handle for later checks.

## Example

```python
handle = legacyAuditHook(request)
```
