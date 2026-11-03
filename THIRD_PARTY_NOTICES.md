# Third-Party Notices

ASIN Product Watch's own source code is licensed under the MIT License in
[`LICENSE`](LICENSE). The container image also includes third-party software
under its respective licenses.

## Python

- Component: CPython 3.12 runtime and standard library
- Upstream: <https://www.python.org/>
- License: Python Software Foundation License Version 2 and licenses of
  incorporated software
- License information: <https://docs.python.org/3/license.html>

The Python license and copyright notices shipped with the runtime are retained
inside the container image.

## tzdata

- Component: Python `tzdata` package
- Upstream: <https://github.com/python/tzdata>
- License: Apache License 2.0
- License file: <https://github.com/python/tzdata/blob/master/LICENSE>

The package's license metadata is retained in its installed distribution inside
the container image.

## Debian base-image components

The image is built from the official `python:3.12.15-slim-trixie` image. It
contains Debian packages and supporting components under their individual
licenses. Copyright and license information supplied by Debian is retained in
`/usr/share/doc/*/copyright` inside the image.

- Python Official Image: <https://hub.docker.com/_/python>
- Debian copyright information: <https://www.debian.org/doc/debian-policy/ch-docs.html#copyright-information>

The published OCI image includes a BuildKit SBOM attestation. Consumers should
review that inventory when they need a complete component-by-component license
assessment for a particular image digest.
