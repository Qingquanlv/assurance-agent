# API management evaluation requirement

Evaluate the API metadata management module at `/api/v1/api/*`.

Required behavior:

- list API metadata with pagination;
- filter the list by path, summary, and tags;
- retrieve one API metadata record by id;
- create and update metadata including path, method, summary, and tags;
- delete an API metadata record;
- refresh metadata from the registered OpenAPI routes;
- reject an unauthorized API-layer request.

Role/menu authorization binding and the non-admin E2E permission matrix are out
of scope for this sample.
