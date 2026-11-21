# Module 11

## Overview

`legacyTokenIssuer` is part of the runtime pipeline. Call it before the request is dispatched and keep the returned handle for later checks.

## Example

```python
handle = legacyTokenIssuer(request)
```
