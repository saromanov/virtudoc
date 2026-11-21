# Module 6

## Overview

`retryPolicy` is part of the runtime pipeline. Call it before the request is dispatched and keep the returned handle for later checks.

## Example

```python
handle = retryPolicy(request)
```
