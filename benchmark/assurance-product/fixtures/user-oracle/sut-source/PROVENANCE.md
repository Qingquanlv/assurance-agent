# User oracle benchmark source

This is the minimal Python backend snapshot used by the User oracle benchmark.
It was copied on 2026-09-06 from the repository's local
`benchmark/vue-fastapi-admin` fixture, whose README identifies the upstream as
<https://github.com/mizhexiaoxiao/vue-fastapi-admin>. The source is licensed
under the adjacent MIT `LICENSE` file (Copyright 2023 mizhexiaoxiao).

Benchmark-only changes remove embedded development credentials, require
runtime secrets through environment handles, bind the server to loopback, and
disable reload. No database, authentication material, frontend assets, or
dependency directories are included.
